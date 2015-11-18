from datetime import datetime
import logging

logger = logging.getLogger('teams.tasks')

from celery.schedules import crontab, timedelta
from celery.task import task
from django.conf import settings
from django.contrib.sites.models import Site
from django.db.models import F
from django.utils.translation import ugettext_lazy as _

from utils import send_templated_email
from widget.video_cache import (
    invalidate_cache as invalidate_video_cache,
    invalidate_video_moderation,
    invalidate_video_visibility
)

from utils.text import fmt
from videos.tasks import video_changed_tasks

@task()
def invalidate_video_caches(team_id):
    """Invalidate all TeamVideo caches for all the given team's videos."""
    from teams.models import Team
    team = Team.objects.get(pk=team_id)
    for video_id in team.teamvideo_set.values_list('video__video_id', flat=True):
        invalidate_video_cache(video_id)

@task()
def invalidate_video_moderation_caches(team):
    """Invalidate the moderation status caches for all the given team's videos."""
    for video_id in team.teamvideo_set.values_list('video__video_id', flat=True):
        invalidate_video_moderation(video_id)

@task()
def update_video_moderation(team):
    """Set the moderated_by field for all the given team's videos."""
    from videos.models import Video

    moderated_by = team if team.moderates_videos() else None
    Video.objects.filter(teamvideo__team=team).update(moderated_by=moderated_by)

@task()
def invalidate_video_visibility_caches(team):
    for video_id in team.teamvideo_set.values_list("video__video_id", flat=True):
        invalidate_video_visibility(video_id)

@task()
def update_video_public_field(team_id):
    from teams.models import Team

    team = Team.objects.get(pk=team_id)

    for team_video in team.teamvideo_set.all():
        video = team_video.video
        video.is_public = team.is_visible
        video.save()
        video_changed_tasks(video.id)

@task
def expire_tasks():
    """Find any tasks that are past their expiration date and unassign them.

    We currently run this once per day (at 7 AM server time).

    """
    from teams.models import Task

    expired_tasks = Task.objects.incomplete().filter(
        expiration_date__isnull=False,
        expiration_date__lt=datetime.now(),
    )
    for task in expired_tasks:
        task.assignee = task.expiration_date = None
        # run each inside a try/except so that one
        # rotten apple doesn't make a huge mess
        try:
            task.save()
        except Exception as e:
            logger.error('Error on expiring tasks', extra={
            'task': task,
            'exception': e,
        })



@task
def add_videos_notification_daily(*args, **kwargs):
    from teams.models import Team
    team_qs = Team.objects.needs_new_video_notification(Team.NOTIFY_DAILY)
    _notify_teams_of_new_videos(team_qs)

@task
def add_videos_notification_hourly(*args, **kwargs):
    from teams.models import Team
    team_qs = Team.objects.needs_new_video_notification(Team.NOTIFY_HOURLY)
    _notify_teams_of_new_videos(team_qs)

def _notify_teams_of_new_videos(team_qs):
    from messages.tasks import _team_sends_notification
    from teams.models import TeamVideo
    domain = Site.objects.get_current().domain

    for team in team_qs:
        if not _team_sends_notification(team, 'block_new_video_message'):
            continue
        team_videos = TeamVideo.objects.filter(team=team, created__gt=team.last_notification_time)

        team.last_notification_time = datetime.now()
        team.save()
        members = team.users.filter( notify_by_email=True, is_active=True) \
            .distinct()

        subject = fmt(_(u'New %(team)s videos ready for subtitling!'),
                      team=team)

        for user in members:
            if not user.email:
                continue

            context = {
                'domain': domain,
                'user': user,
                'team': team,
                'team_videos': team_videos,
                "STATIC_URL": settings.STATIC_URL,
            }

            send_templated_email(user, subject,
                                 'teams/email_new_videos.html',
                                 context, fail_silently=not settings.DEBUG)


@task()
def api_notify_on_subtitles_activity(team_pk, event_name, version_pk):
    from teams.models import TeamNotificationSetting
    from subtitles.models import SubtitleVersion
    version = SubtitleVersion.objects.select_related("subtitle_language", "video").get(pk=version_pk)
    TeamNotificationSetting.objects.notify_team(team_pk, event_name,
            video_id=version.video.video_id,
            language_pk=version.subtitle_language.pk, version_pk=version_pk)

@task()
def api_notify_on_language_activity(team_pk, event_name, language_pk):
    from teams.models import TeamNotificationSetting
    from subtitles.models import SubtitleLanguage
    language = SubtitleLanguage.objects.select_related("video").get(pk=language_pk)
    TeamNotificationSetting.objects.notify_team(
        team_pk, event_name, language_pk=language_pk, video_id=language.video.video_id)

@task()
def api_notify_on_video_activity(team_pk, event_name, video_id):
    from teams.models import TeamNotificationSetting
    TeamNotificationSetting.objects.notify_team(team_pk, event_name, video_id=video_id)

@task()
def api_notify_on_application_activity(team_pk, event_name, application_pk):
    from teams.models import TeamNotificationSetting
    TeamNotificationSetting.objects.notify_team(
        team_pk, event_name, application_pk=application_pk)


@task()
def process_billing_report(billing_report_pk):
    from teams.models import BillingReport
    report = BillingReport.objects.get(pk=billing_report_pk)
    report.process()
