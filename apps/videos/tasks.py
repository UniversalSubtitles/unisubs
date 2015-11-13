# Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.
from datetime import datetime, timedelta
import logging

from celery.schedules import crontab, timedelta
from celery.signals import task_failure
from celery.task import task
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.files.base import ContentFile
from django.db.models import ObjectDoesNotExist
from haystack import site
from raven.contrib.django.models import client
import requests

from babelsubs.storage import diff as diff_subtitles
from messages.models import Message
from messages import tasks
from utils import send_templated_email, DEFAULT_PROTOCOL
from videos.models import (VideoFeed, Video, VIDEO_TYPE_YOUTUBE, VideoUrl,
                           VideoIndex)
from subtitles.models import (
    SubtitleLanguage, SubtitleVersion
)
from auth.models import CustomUser as User
from videos.types import video_type_registrar
from videos.types import UPDATE_VERSION_ACTION, DELETE_LANGUAGE_ACTION
from videos.types import VideoTypeError

celery_logger = logging.getLogger('celery.task')

def process_failure_signal(exception, traceback, sender, task_id,
                           signal, args, kwargs, einfo, **kw):
    exc_info = (type(exception), exception, traceback)
    try:
        celery_logger.error(
            'Celery job exception: %s(%s)' % (exception.__class__.__name__, exception),
            exc_info=exc_info,
            extra={
                'data': {
                    'task_id': task_id,
                    'sender': sender,
                    'args': args,
                    'kwargs': kwargs,
                }
            }
        )
    except:
        pass
task_failure.connect(process_failure_signal)

@task
def cleanup():
    import datetime
    from django.db import transaction
    from django.contrib.sessions.models import Session
    from djcelery.models import TaskState
    from auth.models import EmailConfirmation

    EmailConfirmation.objects.delete_expired_confirmations()

    now = datetime.datetime.now()
    Session.objects.filter(expire_date__lt=now).delete()

    d = now - datetime.timedelta(days=31)
    TaskState.objects.filter(tstamp__lt=d).delete()
    transaction.commit_unless_managed()

@task(time_limit=20)
def save_thumbnail_in_s3(video_id):
    try:
        video = Video.objects.get(pk=video_id)
    except Video.DoesNotExist:
        return

    if video.thumbnail and not video.s3_thumbnail:
        response = requests.get(video.thumbnail, timeout=15)
        content = ContentFile(response.content)
        video.s3_thumbnail.save(video.thumbnail.split('/')[-1], content)

@task
def update_from_feed():
    qs = VideoFeed.objects.exclude(last_update__gt=datetime.now()-
                                   timedelta(hours=1))
    for feed in qs:
        update_video_feed_with_rate_limit.apply_async(
            args=(feed.pk,), queue='feeds')

@task
def update_video_feed(video_feed_id):
    try:
        video_feed = VideoFeed.objects.get(pk=video_feed_id)
        video_feed.update()
    except VideoFeed:
        msg = '**update_video_feed**. VideoFeed does not exist. ID: %s' % video_feed_id
        client.captureMessage(msg)

@task(rate_limit='500/m')
def update_video_feed_with_rate_limit(video_feed_id):
    return update_video_feed(video_feed_id)

@task(ignore_result=False)
def add(a, b):
    print "TEST TASK FOR CELERY. EXECUTED WITH ARGUMENTS: %s %s" % (a, b)
    return (a, b, a+b)

@task
def test_task(n):
    if not n:
        print '.'

    from time import sleep
    for i in xrange(n):
        print '.',
        sleep(0.5)

@task
def raise_exception(msg, **kwargs):
    print "TEST TASK FOR CELERY. RAISE EXCEPTION WITH MESSAGE: %s" % msg
    logger = raise_exception.get_logger()
    logger.error('Test error logging to Sentry from Celery')
    raise TypeError(msg)

@task()
def video_changed_tasks(video_pk, new_version_id=None):
    from videos import metadata_manager
    from videos.models import Video

    from teams.models import TeamVideo, BillingRecord
    metadata_manager.update_metadata(video_pk)
    if new_version_id is not None:
        send_new_version_notification(new_version_id)
        try:
            BillingRecord.objects.insert_record(
                SubtitleVersion.objects.get(pk=new_version_id))
        except Exception, e:
            celery_logger.error("Could not add billing record", extra={
                "version_pk": new_version_id,
                "exception": str(e)})

    video = Video.objects.get(pk=video_pk)

    tv = video.get_team_video()

    if tv:
        tv_search_index = site.get_index(TeamVideo)
        tv_search_index.backend.update(tv_search_index, [tv])

    video.update_search_index()

@task
def subtitles_complete_changed(language_pk):
    """
    On the editor, if you don't actually change the subs, but still change
    it to completed, then there's a bunch of things we want to do, namelly
    check if billing records should be created and if we should push the subtitle
    to youtube
    """
    from teams.models import TeamVideo, BillingRecord
    language = SubtitleLanguage.objects.get(pk=language_pk)
    version = language.get_tip()
    try:
        BillingRecord.objects.insert_record(version)
    except Exception, e:
        celery_logger.error("Could not add billing record", extra={
            "version_pk": version.pk,
            "exception": str(e)})

@task()
def send_change_title_email(video_id, user_id, old_title, new_title):
    from videos.models import Video

    domain = Site.objects.get_current().domain

    try:
        video = Video.objects.get(id=video_id)
        user = user_id and User.objects.get(id=user_id)
    except ObjectDoesNotExist:
        return

    users = video.notification_list(user)

    for obj in users:
        subject = u'Video\'s title changed on Amara'
        context = {
            'user': obj,
            'domain': domain,
            'video': video,
            'editor': user,
            'old_title': old_title,
            'hash': obj.hash_for_video(video.video_id),
            'new_title': new_title,
            "STATIC_URL": settings.STATIC_URL,
        }
        send_templated_email(obj, subject,
                             'videos/email_title_changed.html',
                             context, fail_silently=not settings.DEBUG)

@task()
def import_videos_from_feed(feed_id):
    feed = VideoFeed.objects.get(id=feed_id)
    new_videos = feed.update()
    if feed.user is not None:
        tasks.videos_imported_message.delay(feed.user.id, len(new_videos))

def send_new_version_notification(version_id):
    try:
        version = SubtitleVersion.objects.get(id=version_id)
    except SubtitleVersion.DoesNotExist:
        return False

    # if version.result_of_rollback or not version.is_public:
    if version.is_private():
        return False

    if version.version_number == 0 and not version.language.is_primary_audio_language():
        return send_new_translation_notification(version)
    else:
        time_change, text_change = version.get_changes()
        if text_change or time_change:
            return notify_for_version(version)
    return None

def send_new_translation_notification(translation_version):
    domain = Site.objects.get_current().domain
    video = translation_version.language.video
    language = translation_version.language

    for user in video.notification_list(translation_version.user):
        context = {
            'version': translation_version,
            'domain': domain,
            'video_url': '%s://%s%s' % (DEFAULT_PROTOCOL, domain, video.get_absolute_url()),
            'user': user,
            'language': language,
            'video': video,
            'hash': user.hash_for_video(video.video_id),
            "STATIC_URL": settings.STATIC_URL,
        }
        subject = 'New %s translation by %s of "%s"' % \
            (language.language_display(), translation_version.user.__unicode__(), video.__unicode__())
        send_templated_email(user, subject,
                             'videos/email_start_notification.html',
                             context, fail_silently=not settings.DEBUG)
    return True

def _make_caption_data(new_version, old_version):
    raise Exception("This function is deprecated. "
            "babelsubs.storage.diff")


def notify_for_version(version):
    domain = Site.objects.get_current().domain

    language = version.subtitle_language
    video = language.video

    qs = SubtitleVersion.objects.filter(
            subtitle_language=language).filter(
            version_number__lt=version.version_number).order_by(
                    '-version_number')

    if qs.count() == 0:
        return

    most_recent_version = qs[0]
    diff_data = diff_subtitles(version.get_subtitles(), most_recent_version.get_subtitles())

    title = {
        'new_title': version.title,
        'old_title': most_recent_version.title,
        'has_changed': version.title != most_recent_version.title
    }

    description = {
        'new_description': version.description,
        'old_description': most_recent_version.description,
        'has_changed': version.description !=
            most_recent_version.description
    }

    context = {
        'title': title,
        'description': description,
        'version': version,
        'domain': domain,
        'translation': not language.is_primary_audio_language(),
        'video': version.video,
        'language': language,
        'last_version': most_recent_version,
        'diff_data': diff_data,
        'video_url': video.get_absolute_url(),
        'language_url': language.get_absolute_url(),
        'user_url': version.author and version.author.get_absolute_url(),
        "STATIC_URL": settings.STATIC_URL,
    }

    subject = u'New edits to "%s" by %s on Amara' % (language.video,
            version.author)

    followers = set(video.notification_list(version.author))
    followers.update(language.notification_list(version.author))

    for item in qs:
        if item.author and item.author in followers:
            if item.author.notify_by_email:
                context['your_version'] = item
                context['user'] = item.author
                context['hash'] = item.author.hash_for_video(context['video'].video_id)
                context['user_is_rtl'] = item.author.guess_is_rtl()
                send_templated_email(item.author, subject,
                                 'videos/email_notification.html',
                                 context, fail_silently=not settings.DEBUG)
            if item.author.notify_by_message:
                # TODO: Add body
                Message.objects.create(user=item.author, subject=subject,
                                       content='', message_type='S')
            followers.discard(item.author)

    for user in followers:
        context['user'] = user
        context['hash'] = user.hash_for_video(context['video'].video_id)
        context['user_is_rtl'] = user.guess_is_rtl()
        send_templated_email(user, subject,
                             'videos/email_notification_non_editors.html',
                             context, fail_silently=not settings.DEBUG)
    return True

def _save_video_feed(feed_url, user):
    """ Creates or updates a videofeed given some url """
    try:
        return VideoFeed.objects.get(url=feed_url, user=user)
    except VideoFeed.DoesNotExist:
        return VideoFeed.objects.create(url=feed_url, user=user)

@task
def index_videos():
    VideoIndex.objects.index_videos(limit=100)

@task
def create_missing_video_index_objects():
    VideoIndex.objects.create_missing()
