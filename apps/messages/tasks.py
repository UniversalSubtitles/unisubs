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
"""
Centralizes notification sending throught the website.
Currently we support:
    - email messages
    - site inbox (messages.models.Message)
    - activity feed (videos.models.Action)

Messages models will trigger an email to be sent if
the user has allowed email notifications
"""
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _, ugettext
from django.template.loader import render_to_string
from django.contrib.contenttypes.models import ContentType

from raven.contrib.django.models import client

from celery.task import task

from auth.models import CustomUser as User, UserLanguage
from localeurl.utils import universal_url

from teams.moderation_const import REVIEWED_AND_PUBLISHED, \
     REVIEWED_AND_PENDING_APPROVAL, REVIEWED_AND_SENT_BACK

from messages.models import Message
from utils import send_templated_email
from utils.text import fmt
from utils.translation import get_language_label
BATCH_SIZE = 1000

def get_url_base():
    return "http://" + Site.objects.get_current().domain

def team_sends_notification(team, notification_setting_name):
    from teams.models import Setting
    return not team.settings.filter( key=Setting.KEY_IDS[notification_setting_name]).exists()

@task()
def cleanup():
    # These numbers have to be chosen. Do not cleanup for now
    # Message.objects.cleanup(364, message_type='S')
    # Message.objects.cleanup(2*365, message_type='M')
    # For now we just convert old messages, by small bulks.
    # We have to do this way because we can not update on
    # a sliced query
    for i in range(10):
        messages = []
        for message in Message.objects.all().filter(message_type='O', author__isnull=True)[:BATCH_SIZE]:
            messages.append(message.id)
        Message.objects.all().filter(id__in=messages).update(message_type='S')
        messages = []
        for message in Message.objects.all().filter(message_type='O', author__isnull=False)[:BATCH_SIZE]:
            messages.append(message.id)
        Message.objects.all().filter(id__in=messages).update(message_type='M')

@task()
def send_new_messages_notifications(message_ids):
    for message_id in message_ids:
        send_new_message_notification(message_id)

@task()
def send_new_message_notification(message_id):
    from messages.models import Message
    try:
        message = Message.objects.get(pk=message_id)
    except Message.DoesNotExist:
        msg = '**send_new_message_notification**. Message does not exist. ID: %s' % message_id
        client.create_from_text(msg, logger='celery')
        return

    user = message.user

    if not user.email or not user.is_active or not user.notify_by_email:
        return

    if message.author:
        subject = _(u"New message from %(author)s on Amara: %(subject)s")
    else:
        subject = _("New message on Amara: %(subject)s")
    subject = fmt(subject, author=message.author, subject=message.subject)

    context = {
        "message": message,
        "domain":  Site.objects.get_current().domain,
        "STATIC_URL": settings.STATIC_URL,
    }
    send_templated_email(user, subject, "messages/email/message_received.html", context)

@task()
def team_invitation_sent(invite_pk):
    from messages.models import Message
    from teams.models import Invite, Setting, TeamMember
    invite = Invite.objects.get(pk=invite_pk)
    if not team_sends_notification(invite.team,'block_invitation_sent_message'):
        return False
    # does this team have a custom message for this?
    team_default_message = None
    messages = Setting.objects.messages().filter(team=invite.team)
    if messages.exists():
        data = {}
        for m in messages:
            data[m.get_key_display()] = m.data
        mapping = {
            TeamMember.ROLE_ADMIN: data['messages_admin'],
            TeamMember.ROLE_MANAGER: data['messages_manager'],
            TeamMember.ROLE_CONTRIBUTOR: data['messages_invite'],
        }
        team_default_message = mapping.get(invite.role, None)
    context = {
        'invite': invite,
        'role': invite.role,
        "user":invite.user,
        "inviter":invite.author,
        "team": invite.team,
        "invite_pk": invite_pk,
        'note': invite.note,
        'custom_message': team_default_message,
        'url_base': get_url_base(),
    }
    title = fmt(
        ugettext(u"You've been invited to team %(team)s on Amara"),
        team=invite.team.name)

    if invite.user.notify_by_message:
        body = render_to_string("messages/team-you-have-been-invited.txt", context)
        msg = Message()
        msg.message_type = 'S'
        msg.subject = title
        msg.user = invite.user
        msg.object = invite
        msg.author = invite.author
        msg.content = body
        msg.save()
    template_name = 'messages/email/team-you-have-been-invited.html'
    return send_templated_email(invite.user, title, template_name, context)

@task()
def application_sent(application_pk):
    if getattr(settings, "MESSAGES_DISABLED", False):
        return
    from messages.models import Message
    from teams.models import Application, TeamMember
    application = Application.objects.get(pk=application_pk)
    if not team_sends_notification(application.team,'block_application_sent_message'):
        return False
    notifiable = TeamMember.objects.filter( team=application.team,
       role__in=[TeamMember.ROLE_ADMIN, TeamMember.ROLE_OWNER])
    for m in notifiable:

        template_name = "messages/application-sent.txt"
        context = {
            "application": application,
            "applicant": application.user,
            "url_base": get_url_base(),
            "team":application.team,
            "note":application.note,
            "user":m.user,
        }
        body = render_to_string(template_name,context)
        subject  = fmt(
            ugettext(u'%(user)s is applying for team %(team)s'),
            user=application.user, team=application.team.name)
        if m.user.notify_by_message:
            msg = Message()
            msg.message_type = 'S'
            msg.subject = subject
            msg.content = body
            msg.user = m.user
            msg.object = application.team
            msg.author = application.user
            msg.save()
        send_templated_email(m.user, subject, "messages/email/application-sent-email.html", context)
    return True


@task()
def team_application_denied(application_pk):

    if getattr(settings, "MESSAGES_DISABLED", False):
        return
    from messages.models import Message
    from teams.models import Application
    application = Application.objects.get(pk=application_pk)
    if not team_sends_notification(application.team,'block_application_denided_message'):
        return False
    template_name = "messages/email/team-application-denied.html"
    context = {
        "team": application.team,
        "user": application.user,
        "url_base": get_url_base(),
        "note": application.note,
    }
    subject = fmt(
        ugettext(u'Your application to join the %(team)s '
                 u'team has been declined'),
        team=application.team.name)
    if application.user.notify_by_message:
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string("messages/team-application-denied.txt", context)
        msg.user = application.user
        msg.object = application.team
        msg.save()
    send_templated_email(application.user, subject, template_name, context)

@task()
def team_member_new(member_pk):
    if getattr(settings, "MESSAGES_DISABLED", False):
        return
    from messages.models import Message
    from teams.models import TeamMember, Setting
    member = TeamMember.objects.get(pk=member_pk)
    if not team_sends_notification(member.team,'block_team_member_new_message'):
        return False
    from videos.models import Action
    from teams.models import TeamMember
    # the feed item should appear on the timeline for all team members
    # as a team might have thousands of members, this one item has
    # to show up on all of them
    Action.create_new_member_handler(member)
    # notify  admins and owners through messages
    notifiable = TeamMember.objects.filter( team=member.team,
       role__in=[TeamMember.ROLE_ADMIN, TeamMember.ROLE_OWNER]).exclude(pk=member.pk)
    for m in notifiable:
        context = {
            "new_member": member.user,
            "team":member.team,
            "user":m.user,
            "role":member.role,
            "url_base":get_url_base(),
        }
        body = render_to_string("messages/team-new-member.txt",context)
        subject = fmt(
            ugettext("%(team)s team has a new member"),
            team=member.team)
        if m.user.notify_by_message:
            msg = Message()
            msg.message_type = 'S'
            msg.subject = subject
            msg.content = body
            msg.user = m.user
            msg.object = m.team
            msg.save()
        template_name = "messages/email/team-new-member.html"
        send_templated_email(m.user, subject, template_name, context)

    # does this team have a custom message for this?
    team_default_message = None
    messages = Setting.objects.messages().filter(team=member.team)
    if messages.exists():
        for m in messages:
            if m.get_key_display() == 'messages_joins':
                team_default_message = m.data
                break
    for ul in UserLanguage.objects.filter(user=member.user).order_by("priority"):
        localized_message = Setting.objects.messages().filter(team=member.team, language_code=ul.language)
        if len(localized_message) == 1:
            if team_default_message:
                team_default_message += u'\n\n----------------\n\n' + localized_message[0].data
            else:
                team_default_message = localized_message[0].data
            break
    # now send welcome mail to the new member
    template_name = "messages/team-welcome.txt"
    context = {
       "team":member.team,
       "url_base":get_url_base(),
       "role":member.role,
       "user":member.user,
       "custom_message": team_default_message,
    }
    body = render_to_string(template_name,context)

    msg = Message()
    msg.message_type = 'S'
    msg.subject = fmt(
        ugettext("You've joined the %(team)s team!"),
        team=member.team)
    msg.content = body
    msg.user = member.user
    msg.object = member.team
    msg.save()
    template_name = "messages/email/team-welcome.html"
    send_templated_email(msg.user, msg.subject, template_name, context)

@task()
def team_member_leave(team_pk, user_pk):
    if getattr(settings, "MESSAGES_DISABLED", False):
        return
    from messages.models import Message
    from teams.models import TeamMember, Team
    user = User.objects.get(pk=user_pk)
    team = Team.objects.get(pk=team_pk)
    if not team_sends_notification(team,'block_team_member_leave_message'):
        return False
    from videos.models import Action
    # the feed item should appear on the timeline for all team members
    # as a team might have thousands of members, this one item has
    # to show up on all of them
    Action.create_member_left_handler(team, user)
    # notify  admins and owners through messages
    notifiable = TeamMember.objects.filter( team=team,
       role__in=[TeamMember.ROLE_ADMIN, TeamMember.ROLE_OWNER])
    subject = fmt(
        ugettext(u"%(user)s has left the %(team)s team"),
        user=user, team=team)
    for m in notifiable:
        context = {
            "parting_member": user,
            "team":team,
            "user":m.user,
            "url_base":get_url_base(),
        }
        body = render_to_string("messages/team-member-left.txt",context)
        if m.user.notify_by_message:
            msg = Message()
            msg.message_type = 'S'
            msg.subject = subject
            msg.content = body
            msg.user = m.user
            msg.object = team
            msg.save()
        send_templated_email(m.user, subject, "messages/email/team-member-left.html", context)


    context = {
        "team":team,
        "user":user,
        "url_base":get_url_base(),
    }
    subject = fmt(ugettext("You've left the %(team)s team!"), team=team)
    if user.notify_by_message:
        template_name = "messages/team-member-you-have-left.txt"
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string(template_name,context)
        msg.user = user
        msg.object = team
        msg.save()
    template_name = "messages/email/team-member-you-have-left.html"
    send_templated_email(user, subject, template_name, context)

@task()
def email_confirmed(user_pk):
    from messages.models import Message
    user = User.objects.get(pk=user_pk)
    subject = "Welcome aboard!"
    context = {"user":user}
    if user.notify_by_message:
        body = render_to_string("messages/email-confirmed.txt", context)
        message  = Message(
            user=user,
            message_type='S',
            subject=subject,
            content=body
        )
        message.save()
    template_name = "messages/email/email-confirmed.html"
    send_templated_email(user, subject, template_name, context )
    return True

@task()
def videos_imported_message(user_pk, imported_videos):
    from messages.models import Message
    user = User.objects.get(pk=user_pk)
    subject = u"Your videos were imported!"
    url = "%s%s" % (get_url_base(),
                    reverse("profiles:videos", kwargs={'user_id': user_pk}))
    context = {"user": user,
               "imported_videos": imported_videos,
               "my_videos_url": url}

    if user.notify_by_message:
        body = render_to_string("messages/videos-imported.txt", context)
        message  = Message(
            message_type='S',
            user=user,
            subject=subject,
            content=body
        )
        message.save()
    template_name = "messages/email/videos-imported.html"
    send_templated_email(user, subject, template_name, context)

@task()
def team_task_assigned(task_pk):
    from teams.models import Task
    from messages.models import Message
    try:
        task = Task.objects.select_related("team_video__video", "team_video", "assignee").get(pk=task_pk, assignee__isnull=False)
        if not team_sends_notification(task.team,'block_task_assigned_message'):
            return False
    except Task.DoesNotExist:
        return False
    task_type = Task.TYPE_NAMES[task.type]
    subject = ugettext(u"You have a new task assignment on Amara!")
    user = task.assignee
    task_language = None
    if task.language:
        task_language = get_language_label(task.language)
    context = {
        "team":task.team,
        "user":user,
        "task_type": task_type,
        "task_language": task_language,
        "url_base":get_url_base(),
        "task":task,
    }
    msg = None
    if user.notify_by_message:
        template_name = "messages/team-task-assigned.txt"
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string(template_name,context)
        msg.user = user
        msg.object = task.team
        msg.save()

    template_name = "messages/email/team-task-assigned.html"
    email_res = send_templated_email(user, subject, template_name, context)
    return msg, email_res


def _reviewed_notification(task_pk, status):
    from teams.models import Task
    from videos.models import Action
    from messages.models import Message
    try:
        task = Task.objects.select_related(
            "team_video__video", "team_video", "assignee").get(
                pk=task_pk)
    except Task.DoesNotExist:
        return False

    notification_setting_name = {

        REVIEWED_AND_PUBLISHED: 'block_reviewed_and_published_message',
        REVIEWED_AND_PENDING_APPROVAL: 'block_reviewed_and_pending_approval_message',
        REVIEWED_AND_SENT_BACK: 'block_reviewed_and_sent_back_message',
    }[status]
    if not team_sends_notification(task.team, notification_setting_name):
        return False

    subject = ugettext(u"Your subtitles have been reviewed")
    if status == REVIEWED_AND_PUBLISHED:
        subject += ugettext(" and published")

    version = task.get_subtitle_version()

    if task.new_review_base_version:
        user = task.new_review_base_version.author
    else:
        user = version.author

    task_language = get_language_label(task.language)
    reviewer = task.assignee
    video = task.team_video.video
    subs_url = "%s%s" % (get_url_base(), reverse("videos:translation_history", kwargs={
        'video_id': video.video_id,
        'lang': task.language,
        'lang_id': version.subtitle_language.pk,

    }))
    reviewer_message_url = "%s%s?user=%s" % (
        get_url_base(), reverse("messages:new"), reviewer.username)

    reviewer_profile_url = "%s%s" % (get_url_base(), reverse("profiles:profile", kwargs={'user_id': reviewer.id}))
    perform_task_url = "%s%s" % (get_url_base(), reverse("teams:perform_task", kwargs={
        'slug': task.team.slug,
        'task_pk': task_pk
    }))

    context = {
        "team":task.team,
        "title": version.subtitle_language.get_title(),
        "user":user,
        "task_language": task_language,
        "url_base":get_url_base(),
        "task":task,
        "reviewer":reviewer,
        "note":task.body,
        "reviewed_and_pending_approval": status == REVIEWED_AND_PENDING_APPROVAL,
        "sent_back": status == REVIEWED_AND_SENT_BACK,
        "reviewed_and_published": status == REVIEWED_AND_PUBLISHED,
        "subs_url": subs_url,
        "reviewer_message_url": reviewer_message_url,
        "reviewer_profile_url": reviewer_profile_url,
        "perform_task_url": perform_task_url,
    }
    msg = None
    if user.notify_by_message:
        template_name = "messages/team-task-reviewed.txt"
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string(template_name,context)
        msg.user = user
        msg.object = task.team
        msg.save()

    template_name = "messages/email/team-task-reviewed.html"
    email_res =  send_templated_email(user, subject, template_name, context)

    if status == REVIEWED_AND_SENT_BACK:
        if task.type == Task.TYPE_IDS['Review']:
            Action.create_declined_video_handler(version, reviewer)
        else:
            Action.create_rejected_video_handler(version, reviewer)
    elif status == REVIEWED_AND_PUBLISHED:
        Action.create_approved_video_handler(version, reviewer)
    elif status == REVIEWED_AND_PENDING_APPROVAL:
        Action.create_accepted_video_handler(version, reviewer)

    return msg, email_res

@task
def reviewed_and_published(task_pk):
    return _reviewed_notification(task_pk, REVIEWED_AND_PUBLISHED)

@task
def reviewed_and_pending_approval(task_pk):
    return _reviewed_notification(task_pk, REVIEWED_AND_PENDING_APPROVAL)

@task
def reviewed_and_sent_back(task_pk):
    return _reviewed_notification(task_pk, REVIEWED_AND_SENT_BACK)

@task
def approved_notification(task_pk, published=False):
    """
    On approval, it can be sent back (published=False) or
    approved and published
    """
    from teams.models import Task
    from videos.models import Action
    from messages.models import Message
    from teams.models import TeamNotificationSetting
    try:
        task = Task.objects.select_related(
            "team_video__video", "team_video", "assignee", "subtitle_version").get(
                pk=task_pk)
        if not team_sends_notification(task.team, 'block_approved_message'):
            return False
    except Task.DoesNotExist:
        return False
    # some tasks are being created without subtitles version, see
    # https://unisubs.sifterapp.com/projects/12298/issues/552092/comments

    if published:
        subject = ugettext(u"Your subtitles have been approved and published!")
        template_txt = "messages/team-task-approved-published.txt"
        template_html ="messages/email/team-task-approved-published.html"
        # Not sure whether it is the right place to send notification
        # but should work around the approval when there is no new sub version
        version = task.get_subtitle_version()
        TeamNotificationSetting.objects.notify_team(task.team.pk, TeamNotificationSetting.EVENT_SUBTITLE_APPROVED,
                                                    video_id=version.video.video_id,
                                                    language_pk=version.subtitle_language.pk, version_pk=version.pk)
    else:
        template_txt = "messages/team-task-approved-sentback.txt"
        template_html ="messages/email/team-task-approved-sentback.html"
        subject = ugettext(u"Your subtitles have been returned for further editing")
    version = task.get_subtitle_version()
    if task.new_review_base_version:
        user = task.new_review_base_version.author
    else:
        user = version.author
    task_language = get_language_label(task.language)
    reviewer = task.assignee
    video = task.team_video.video
    subs_url = "%s%s" % (get_url_base(), reverse("videos:translation_history", kwargs={
        'video_id': video.video_id,
        'lang': task.language,
        'lang_id': version.subtitle_language.pk,

    }))
    reviewer_message_url = "%s%s?user=%s" % (
        get_url_base(), reverse("messages:new"), reviewer.username)

    context = {
        "team":task.team,
        "title": version.subtitle_language.get_title(),
        "user":user,
        "task_language": task_language,
        "url_base":get_url_base(),
        "task":task,
        "reviewer":reviewer,
        "note":task.body,
        "subs_url": subs_url,
        "reviewer_message_url": reviewer_message_url,
    }
    msg = None
    if user.notify_by_message:
        template_name = template_txt
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string(template_name,context)
        msg.user = user
        msg.object = task.team
        msg.save()

    template_name = template_html
    email_res =  send_templated_email(user, subject, template_name, context)
    Action.create_approved_video_handler(version, reviewer)
    return msg, email_res

@task
def send_reject_notification(task_pk, sent_back):
    raise NotImplementedError()
    from teams.models import Task
    from videos.models import Action
    from messages.models import Message
    try:
        task = Task.objects.select_related(
            "team_video__video", "team_video", "assignee", "subtitle_version").get(
                pk=task_pk)
    except Task.DoesNotExist:
        return False

    version = task.get_subtitle_version()
    subject = ugettext(u"Your subtitles were not accepted")
    if task.new_review_base_version:
        user = task.new_review_base_version.author
    else:
        user = version.author
    task_language = get_language_label(task.language)
    reviewer = task.assignee
    video = task.team_video.video
    subs_url = "%s%s" % (get_url_base(), reverse("videos:translation_history", kwargs={
        'video_id': video.video_id,
        'lang': task.language,
        'lang_id': version.subtitle_language.pk,

    }))
    reviewer_message_url = "%s%s?user=%s" % (
        get_url_base(), reverse("messages:new"), reviewer.username)

    context = {
        "team":task.team,
        "title": version.subtitle_language.get_title(),
        "user":user,
        "task_language": task_language,
        "url_base":get_url_base(),
        "task":task,
        "reviewer":reviewer,
        "note":task.body,
        "sent_back": sent_back,
        "subs_url": subs_url,
        "reviewer_message_url": reviewer_message_url,
    }
    msg = None
    if user.notify_by_message:
        template_name = "messages/team-task-rejected.txt"
        msg = Message()
        msg.message_type = 'S'
        msg.subject = subject
        msg.content = render_to_string(template_name,context)
        msg.user = user
        msg.object = task.team
        msg.save()

    template_name = "messages/email/team-task-rejected.html"
    email_res =  send_templated_email(user, subject, template_name, context)
    Action.create_rejected_video_handler(version, reviewer)
    return msg, email_res

COMMENT_MAX_LENGTH = getattr(settings,'COMMENT_MAX_LENGTH', 3000)
@task
def send_video_comment_notification(comment_pk_or_instance, version_pk=None):
    """
    Comments can be attached to a video (appear in the videos:video (info)) page) OR
                                  sublanguage (appear in the videos:translation_history  page)
    Approval / Reviews notes are also stored as comments.

    """
    from comments.models import Comment
    from videos.models import Video
    from subtitles.models import SubtitleLanguage, SubtitleVersion

    if not isinstance(comment_pk_or_instance, Comment):
        try:
            comment = Comment.objects.get(pk=comment_pk_or_instance)
        except Comment.DoesNotExist:
            return
    else:
        comment = comment_pk_or_instance

    version = None

    if version_pk:
        try:
            version = SubtitleVersion.objects.get(pk=version_pk)
        except SubtitleVersion.DoesNotExist:
            pass

    ct = comment.content_object

    if isinstance(ct, Video):
        video = ct
        version = None
        language = None
    elif isinstance(ct, SubtitleLanguage):
        video = ct.video
        language = ct

    domain = Site.objects.get_current().domain
    protocol = getattr(settings, 'DEFAULT_PROTOCOL', 'https')

    if language:
        language_url = universal_url("videos:translation_history", kwargs={
            "video_id": video.video_id,
            "lang": language.language_code,
            "lang_id": language.pk,
        })
    else:
        language_url = None

    if version:
        version_url = universal_url("videos:subtitleversion_detail", kwargs={
            'video_id': version.video.video_id,
            'lang': version.subtitle_language.language_code,
            'lang_id': version.subtitle_language.pk,
            'version_id': version.pk,
        })
    else:
        version_url = None

    subject = fmt(
        ugettext(u'%(user)s left a comment on the video %(title)s'),
        user=unicode(comment.user), title=video.title_display())

    followers = set(video.notification_list(comment.user))

    for user in followers:
        send_templated_email(
            user,
            subject,
            "messages/email/comment-notification.html",
            {
                "video": video,
                "user": user,
                "hash": user.hash_for_video(video.video_id),
                "commenter": unicode(comment.user),
                "commenter_url": comment.user.get_absolute_url(),
                "version_url":version_url,
                "language_url":language_url,
                "domain":domain,
                "version": version,
                "body": comment.content,
                "STATIC_URL": settings.STATIC_URL,
            },
            fail_silently=not settings.DEBUG)


    if language:
        obj = language
        object_pk = language.pk
        content_type = ContentType.objects.get_for_model(language)
        exclude = [u for u in language.followers.filter(notify_by_message=False)]
        exclude.append(comment.user)
        message_followers = language.notification_list(exclude)
    else:
        obj = video
        object_pk = video.pk
        content_type = ContentType.objects.get_for_model(video)
        exclude = list(video.followers.filter(notify_by_message=False))
        exclude.append(comment.user)
        message_followers = video.notification_list(exclude)

    for user in message_followers:
        Message.objects.create(user=user, subject=subject, object_pk=object_pk,
                               content_type=content_type, object=obj, message_type="S",
                content=render_to_string('messages/new-comment.html', {
                    "video": video,
                    "language": language,
                    "commenter": unicode(comment.user),
                    "commenter_url": comment.user.get_absolute_url(),
                    "version_url":version_url,
                    "language_url":language_url,
                    "domain":domain,
                    "protocol": protocol,
                    "version": version,
                    "body": comment.content
                }))
