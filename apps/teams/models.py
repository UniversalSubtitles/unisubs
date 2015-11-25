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
from collections import defaultdict
from itertools import groupby
from math import ceil
import csv
import datetime
import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.core.files import File
from django.db import connection
from django.db import models
from django.db.models import query, Q, Count, Sum
from django.db.models.signals import post_save, post_delete, pre_delete
from django.http import Http404
from django.template.loader import render_to_string
from django.utils.translation import ugettext_lazy as _, ugettext

import teams.moderation_const as MODERATION
from caching import ModelCacheManager
from comments.models import Comment
from auth.models import UserLanguage, CustomUser as User
from auth.providers import get_authentication_provider
from messages import tasks as notifier
from subtitles import shims
from subtitles.signals import language_deleted
from teams.moderation_const import WAITING_MODERATION, UNMODERATED, APPROVED
from teams.permissions_const import (
    TEAM_PERMISSIONS, PROJECT_PERMISSIONS, ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER,
    ROLE_CONTRIBUTOR
)
from teams import tasks
from teams import workflows
from teams.exceptions import ApplicationInvalidException
from teams.notifications import BaseNotification
from teams.signals import api_subtitles_approved, api_subtitles_rejected
from utils import DEFAULT_PROTOCOL
from utils import translation
from utils.amazon import S3EnabledImageField, S3EnabledFileField
from utils.panslugify import pan_slugify
from utils.text import fmt
from videos.models import (Video, VideoUrl, SubtitleVersion, SubtitleLanguage,
                           Action)
from videos.tasks import video_changed_tasks
from subtitles.models import (
    SubtitleVersion as NewSubtitleVersion,
    SubtitleLanguage as NewSubtitleLanguage,
    SubtitleNoteBase,
    ORIGIN_IMPORTED
)
from subtitles import pipeline

from functools import partial

logger = logging.getLogger(__name__)
celery_logger = logging.getLogger('celery.task')

BILLING_CUTOFF = getattr(settings, 'BILLING_CUTOFF', None)

# Teams
class TeamQuerySet(query.QuerySet):
    def add_members_count(self):
        """Add _members_count field to this query

        This can be used to order/filter the query and also avoids a query in
        when Team.members_count() is called.
        """
        select = {
            '_members_count': (
                'SELECT COUNT(1) '
                'FROM teams_teammember tm '
                'WHERE tm.team_id=teams_team.id'
            )
        }
        return self.extra(select=select)

    def add_videos_count(self):
        """Add _videos_count field to this query

        This can be used to order/filter the query and also avoids a query in
        when Team.video_count() is called.
        """
        select = {
            '_videos_count':  (
                'SELECT COUNT(1) '
                'FROM teams_teamvideo tv '
                'WHERE tv.team_id=teams_team.id'
            )
        }
        return self.extra(select=select)

    def add_user_is_member(self, user):
        """Add user_is_member field to this query """
        if not user.is_authenticated():
            return self.extra(select={'user_is_member': 0})
        select = {
            'user_is_member':  (
                'EXISTS (SELECT 1 '
                'FROM teams_teammember tm '
                'WHERE tm.team_id=teams_team.id '
                'AND tm.user_id=%s)'
            )
        }
        return self.extra(select=select, select_params=[user.id])

class TeamManager(models.Manager):
    def get_query_set(self):
        """Return a QS of all non-deleted teams."""
        return TeamQuerySet(Team).filter(deleted=False)

    def for_user(self, user, exclude_private=False):
        """Return the teams visible for the given user.

        If exclude_private is True, then we will exclude private teams, even
        if the user can apply to them.
        """
        # policies where we should show the team, even if they're not visible
        visible_policies = [Team.OPEN, Team.APPLICATION]
        q = models.Q(is_visible=True)
        if not exclude_private:
            q |= models.Q(membership_policy__in=visible_policies)
        if user.is_authenticated():
            user_teams = TeamMember.objects.filter(user=user)
            q |= models.Q(id__in=user_teams.values('team_id'))
        return self.get_query_set().filter(q)

    def with_recent_billing_record(self, day_range):
        """Find teams that have had a new video recently"""
        start_date = (datetime.datetime.now() -
                      datetime.timedelta(days=day_range))
        team_ids = list(BillingRecord.objects
                        .order_by()
                        .filter(created__gt=start_date)
                        .values_list('team_id', flat=True)
                        .distinct())
        return Team.objects.filter(id__in=team_ids)

    def needs_new_video_notification(self, notify_interval):
        return (self.filter(
            notify_interval=notify_interval,
            teamvideo__created__gt=models.F('last_notification_time'))
            .distinct())

class Team(models.Model):
    APPLICATION = 1
    INVITATION_BY_MANAGER = 2
    INVITATION_BY_ALL = 3
    OPEN = 4
    INVITATION_BY_ADMIN = 5
    MEMBERSHIP_POLICY_CHOICES = (
        (OPEN, _(u'Open')),
        (APPLICATION, _(u'Application')),
        (INVITATION_BY_ALL, _(u'Invitation by any team member')),
        (INVITATION_BY_MANAGER, _(u'Invitation by manager')),
        (INVITATION_BY_ADMIN, _(u'Invitation by admin')),
    )

    VP_MEMBER = 1
    VP_MANAGER = 2
    VP_ADMIN = 3
    VIDEO_POLICY_CHOICES = (
        (VP_MEMBER, _(u'Any team member')),
        (VP_MANAGER, _(u'Managers and admins')),
        (VP_ADMIN, _(u'Admins only'))
    )

    TASK_ASSIGN_CHOICES = (
        (10, 'Any team member'),
        (20, 'Managers and admins'),
        (30, 'Admins only'),
    )
    TASK_ASSIGN_NAMES = dict(TASK_ASSIGN_CHOICES)
    TASK_ASSIGN_IDS = dict([choice[::-1] for choice in TASK_ASSIGN_CHOICES])

    SUBTITLE_CHOICES = (
        (10, 'Anyone'),
        (20, 'Any team member'),
        (30, 'Only managers and admins'),
        (40, 'Only admins'),
    )
    SUBTITLE_NAMES = dict(SUBTITLE_CHOICES)
    SUBTITLE_IDS = dict([choice[::-1] for choice in SUBTITLE_CHOICES])

    NOTIFY_DAILY = 'D'
    NOTIFY_HOURLY = 'H'
    NOTIFY_INTERVAL_CHOICES = (
        (NOTIFY_DAILY, _('Daily')),
        (NOTIFY_HOURLY, _('Hourly')),
    )

    name = models.CharField(_(u'name'), max_length=250, unique=True)
    slug = models.SlugField(_(u'slug'), unique=True)
    description = models.TextField(_(u'description'), blank=True, help_text=_('All urls will be converted to links. Line breaks and HTML not supported.'))

    logo = S3EnabledImageField(verbose_name=_(u'logo'), blank=True,
                               upload_to='teams/logo/',
                               default='',
                               thumb_sizes=[(280, 100), (100, 100)])
    square_logo = S3EnabledImageField(verbose_name=_(u'square logo'),
                                      blank=True,
                                      default='',
                                      upload_to='teams/square-logo/',
                                      thumb_sizes=[(100, 100), (48, 48)])
    is_visible = models.BooleanField(_(u'videos public?'), default=True)
    videos = models.ManyToManyField(Video, through='TeamVideo',  verbose_name=_('videos'))
    users = models.ManyToManyField(User, through='TeamMember', related_name='teams', verbose_name=_('users'))

    points = models.IntegerField(default=0, editable=False)
    applicants = models.ManyToManyField(User, through='Application', related_name='applicated_teams', verbose_name=_('applicants'))
    created = models.DateTimeField(auto_now_add=True)
    highlight = models.BooleanField(default=False)
    video = models.ForeignKey(Video, null=True, blank=True, related_name='intro_for_teams', verbose_name=_(u'Intro Video'))
    application_text = models.TextField(blank=True)
    page_content = models.TextField(_(u'Page content'), blank=True, help_text=_(u'You can use markdown. This will replace Description.'))
    is_moderated = models.BooleanField(default=False)
    header_html_text = models.TextField(blank=True, default='', help_text=_(u"HTML that appears at the top of the teams page."))
    last_notification_time = models.DateTimeField(editable=False, default=datetime.datetime.now)
    notify_interval = models.CharField(max_length=1,
                                       choices=NOTIFY_INTERVAL_CHOICES,
                                       default=NOTIFY_DAILY)

    auth_provider_code = models.CharField(_(u'authentication provider code'),
                                          max_length=24, blank=True, default="")

    # code value from one the TeamWorkflow subclasses
    # Since other apps can add workflow types, let's use this system to avoid
    # conflicts:
    #   - Core types are defined in the teams app and 1 char long
    #   - Extention types are defined on other apps.  They are 2 chars long,
    #     with the first one being unique to the app.
    workflow_type = models.CharField(max_length=2, default='O')

    # Enabling Features
    projects_enabled = models.BooleanField(default=False)
    # Deprecated field that enables the tasks workflow
    workflow_enabled = models.BooleanField(default=False)

    # Policies and Permissions
    membership_policy = models.IntegerField(_(u'membership policy'),
                                            choices=MEMBERSHIP_POLICY_CHOICES,
                                            default=OPEN)
    video_policy = models.IntegerField(_(u'video policy'),
                                       choices=VIDEO_POLICY_CHOICES,
                                       default=VP_MEMBER)

    # The values below here are mostly specific to the tasks workflow and will
    # probably be deleted.
    task_assign_policy = models.IntegerField(_(u'task assignment policy'),
                                             choices=TASK_ASSIGN_CHOICES,
                                             default=TASK_ASSIGN_IDS['Any team member'])
    subtitle_policy = models.IntegerField(_(u'subtitling policy'),
                                          choices=SUBTITLE_CHOICES,
                                          default=SUBTITLE_IDS['Anyone'])
    translate_policy = models.IntegerField(_(u'translation policy'),
                                           choices=SUBTITLE_CHOICES,
                                           default=SUBTITLE_IDS['Anyone'])
    max_tasks_per_member = models.PositiveIntegerField(_(u'maximum tasks per member'),
                                                       default=None, null=True, blank=True)
    task_expiration = models.PositiveIntegerField(_(u'task expiration (days)'),
                                                  default=None, null=True, blank=True)

    deleted = models.BooleanField(default=False)
    partner = models.ForeignKey('Partner', null=True, blank=True,
                                related_name='teams')

    objects = TeamManager()
    all_objects = models.Manager() # For accessing deleted teams, if necessary.

    cache = ModelCacheManager()

    class Meta:
        ordering = ['name']
        verbose_name = _(u'Team')
        verbose_name_plural = _(u'Teams')

    def __init__(self, *args, **kwargs):
        models.Model.__init__(self, *args, **kwargs)
        self._member_cache = {}

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super(Team, self).save(*args, **kwargs)
        self.cache.invalidate()
        if creating:
            # create a default project
            self.default_project
            # setup our workflow
            self.new_workflow.setup_team()

    def __unicode__(self):
        return self.name or self.slug

    def is_tasks_team(self):
        return self.workflow_enabled

    @property
    def new_workflow(self):
        if not hasattr(self, '_new_workflow'):
            self._new_workflow = workflows.TeamWorkflow.get_workflow(self)
        return self._new_workflow

    def is_old_style(self):
        return self.workflow_type == "O"

    def get_tasks_page_url(self):
        return reverse('teams:team_tasks', kwargs={
            'slug': self.slug,
        })

    def languages(self, members_joined_since=None):
        """Returns the languages spoken by the member of the team
        """
        if members_joined_since:
            users = self.members_since(members_joined_since)
        else:
            users = self.users.all()
        return UserLanguage.objects.filter(user__in=users).values_list('language', flat=True)

    def active_users(self, since=None, published=True):
        sv = NewSubtitleVersion.objects.filter(video__in=self.videos.all())
        if published:
            sv = sv.filter(Q(visibility_override='public') | Q(visibility='public'))
        if since:
            sv = sv.filter(created__gt=datetime.datetime.now() - datetime.timedelta(days=since))
        return sv.exclude(author__username="anonymous").values_list('author', 'subtitle_language')

    def get_default_message(self, name):
        return fmt(Setting.MESSAGE_DEFAULTS.get(name, ''), team=self)

    def get_messages(self, names):
        """Fetch messages from the settings objects

        This method fetches the messages assocated with names and interpolates
        them to replace %(team)s with the team name.

        Returns:
            dict mapping names to message text
        """
        messages = {
            name: self.get_default_message(name)
            for name in names
        }
        for setting in self.settings.with_names(names):
            if setting.data:
                messages[setting.key_name] = setting.data
        return messages

    def render_message(self, msg):
        """Return a string of HTML represention a team header for a notification.

        TODO: Get this out of the model and into a templatetag or something.

        """
        author_page = msg.author.get_absolute_url() if msg.author else ''
        context = {
            'team': self,
            'msg': msg,
            'author': msg.author,
            'author_page': author_page,
            'team_page': self.get_absolute_url(),
            "STATIC_URL": settings.STATIC_URL,
        }
        return render_to_string('teams/_team_message.html', context)

    def is_open(self):
        """Return whether this team's membership is open to the public."""
        return self.membership_policy == Team.OPEN

    def is_by_application(self):
        """Return whether this team's membership is by application only."""
        return self.membership_policy == Team.APPLICATION

    def is_by_invitation(self):
        """Return whether this team's membership is by application only."""
        return self.membership_policy in (
            Team.INVITATION_BY_MANAGER,
            Team.INVITATION_BY_ALL,
            Team.INVITATION_BY_ADMIN,
        )

    def get_workflow(self):
        """Return the workflow for the given team.

        A workflow will always be returned.  If one isn't specified for the team
        a default (unsaved) one will be populated with default values and
        returned.

        TODO: Refactor this behaviour into something less confusing.

        """
        return Workflow.get_for_target(self.id, 'team')

    @property
    def auth_provider(self):
        """Return the authentication provider class for this Team, or None.

        No DB queries are used, so this is safe to call many times.

        """
        if not self.auth_provider_code:
            return None
        else:
            return get_authentication_provider(self.auth_provider_code)

    # Thumbnails
    def logo_thumbnail(self):
        """URL for a kind-of small version of this team's logo, or None."""
        if self.logo:
            return self.logo.thumb_url(100, 100)

    def logo_thumbnail_medium(self):
        """URL for a medium version of this team's logo, or None."""
        if self.logo:
            return self.logo.thumb_url(280, 100)

    def square_logo_thumbnail(self):
        """URL for this team's square logo, or None."""
        if self.square_logo:
            return self.square_logo.thumb_url(100, 100)

    def square_logo_thumbnail_small(self):
        """URL for a small version of this team's square logo, or None."""
        if self.square_logo:
            return self.square_logo.thumb_url(48, 48)

    # URLs
    @models.permalink
    def get_absolute_url(self):
        return ('teams:dashboard', [self.slug])

    def get_site_url(self):
        """Return the full, absolute URL for this team, including http:// and the domain."""
        return '%s://%s%s' % (DEFAULT_PROTOCOL,
                              Site.objects.get_current().domain,
                              self.get_absolute_url())


    # Membership and roles
    def get_member(self, user):
        """Get a TeamMember object for a user or None."""
        if not user.is_authenticated():
            return None

        if user.id in self._member_cache:
            return self._member_cache[user.id]
        try:
            member = self.members.get(user_id=user.id)
            member.team = self
            member.user = user
        except TeamMember.DoesNotExist:
            member = None
        self._member_cache[user.id] = member
        return member

    def get_join_mode(self, user):
        """Figure out how the user can join the team.

        Returns:
            - "open" -- user can join the team without any approval
            - "application" -- user can apply to join the team
            - "pending-application" -- user has a pending application to join
              the team
            - "invitation" -- user must be invited to join
            - "already-joined" -- user has already joined the team
            - "login" -- user needs to login first
            - None -- user can't join the team
        """
        if not user.is_authenticated():
            return 'login'
        elif self.user_is_member(user):
            return 'already-joined'
        elif self.is_open():
            return 'open'
        elif self.is_by_invitation():
            return 'invitation'
        elif self.is_by_application():
            try:
                application = self.applications.get(user=user)
            except Application.DoesNotExist:
                return 'application'
            else:
                if application.status == Application.STATUS_PENDING:
                    return 'pending-application'
        return None

    def user_is_member(self, user):
        members = self.cache.get('members')
        if members is None:
            members = list(self.members.values_list('user_id', flat=True))
            self.cache.set('members', members)
        return user.id in members

    def uncache_member(self, user):
        try:
            del self._member_cache[user.id]
        except KeyError:
            pass

    def invitable_users(self):
        pending_invites = (Invite.objects
                           .pending_for(team=self)
                           .values_list('user_id'))
        return (User.objects
                .exclude(team_members__team=self)
                .exclude(id__in=pending_invites))

    def potential_language_managers(self, language_code):
        member_qs = (TeamMember.objects
                     .filter(team=self)
                     .exclude(languages_managed__code=language_code))
        return User.objects.filter(team_members__in=member_qs)

    def user_can_view_videos(self, user):
        return self.is_visible or self.user_is_member(user)

    def _is_role(self, user, role=None):
        """Return whether the given user has the given role in this team.

        Safe to use with null or unauthenticated users.

        If no role is given, simply return whether the user is a member of this team at all.

        TODO: Change this to use the stuff in teams.permissions.

        """
        if not user or not user.is_authenticated():
            return False
        qs = self.members.filter(user=user)
        if role:
            qs = qs.filter(role=role)
        return qs.exists()

    def can_bulk_approve(self, user):
        return self.is_owner(user) or self.is_admin(user)

    def is_owner(self, user):
        """
        Return whether the given user is an owner of this team.
        """
        return self._is_role(user, TeamMember.ROLE_OWNER)

    def is_admin(self, user):
        """Return whether the given user is an admin of this team."""
        return self._is_role(user, TeamMember.ROLE_ADMIN)

    def is_manager(self, user):
        """Return whether the given user is a manager of this team."""
        return self._is_role(user, TeamMember.ROLE_MANAGER)

    def is_member(self, user):
        """Return whether the given user is a member of this team."""
        return self._is_role(user)

    def is_contributor(self, user, authenticated=True):
        """Return whether the given user is a contributor of this team, False otherwise."""
        return self._is_role(user, TeamMember.ROLE_CONTRIBUTOR)

    def can_see_video(self, user, team_video=None):
        """I have no idea.

        TODO: Figure out what this thing is, and if it's still necessary.

        """
        if not user.is_authenticated():
            return False
        return self.is_member(user)

    def fetch_video_actions(self, video_language=None):
        """Fetch the Action objects for this team's videos

        Args:
            video_language: only actions for videos with this
                            primary_audio_language_code
        """
        video_q = TeamVideo.objects.filter(team=self).values_list('video_id')
        if video_language is not None:
            video_q = video_q.filter(
                video__primary_audio_language_code=video_language)
        return Action.objects.filter(video_id__in=video_q)

    def projects_with_video_stats(self):
        """Fetch all projects for this team and stats about their videos

        This method returns a list of projects, where each project has these
        attributes:

        - video_count: total number of videos in the project
        - videos_with_duration: number of videos with durations set
        - videos_without_duration: number of videos with NULL durations
        - total_duration: sum of all video durations (in seconds)
        """

        # We should be able to do with with an annotate() call, but for some
        # reason it doesn't work.  I think it has to be a django bug because
        # when you print out the query and run it you get the correct results,
        # but when you fetch objects from the queryset, then you get the wrong
        # results.
        stats_sql = (
            'SELECT p.id, COUNT(tv.id), COUNT(v.duration), SUM(v.duration) '
            'FROM teams_project p '
            'LEFT JOIN teams_teamvideo tv ON p.id = tv.project_id '
            'LEFT JOIN videos_video v ON tv.video_id = v.id '
            'WHERE p.team_id=%s '
            'GROUP BY p.id')
        cursor = connection.cursor()
        cursor.execute(stats_sql, (self.id,))
        stats_map = { r[0]: r[1:] for r in cursor }
        projects = list(self.project_set.all())
        for p in projects:
            stats = stats_map[p.id]
            p.video_count = stats[0]
            p.videos_with_duration = stats[1]
            p.videos_without_duration = stats[0] - stats[1]
            p.total_duration = int(stats[2]) if stats[2] is not None else 0
        return projects

    # Moderation
    def moderates_videos(self):
        """Return whether this team moderates videos in some way, False otherwise.

        Moderation means the team restricts who can create subtitles and/or
        translations.

        """
        if self.subtitle_policy != Team.SUBTITLE_IDS['Anyone']:
            return True

        if self.translate_policy != Team.SUBTITLE_IDS['Anyone']:
            return True

        return False

    def video_is_moderated_by_team(self, video):
        """Return whether this team moderates the given video."""
        return video.moderated_by == self


    # Item counts
    @property
    def members_count(self):
        """Return the number of members of this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_members_count'):
            setattr(self, '_members_count', self.users.count())
        return self._members_count

    def members_count_since(self, joined_since):
        """Return the number of members of this team who joined the last n days.
        """
        return self.users.filter(date_joined__gt=datetime.datetime.now() - datetime.timedelta(days=joined_since)).count()

    def members_since(self, joined_since):
        """ Returns the members who joined the team the last n days
        """
        return self.users.filter(date_joined__gt=datetime.datetime.now() - datetime.timedelta(days=joined_since))

    @property
    def videos_count(self):
        """Return the number of videos of this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_videos_count'):
            setattr(self, '_videos_count', self.teamvideo_set.count())
        return self._videos_count

    def videos_count_since(self, added_since = None):
        """Return the number of videos of this team added the last n days.
        """
        return self.teamvideo_set.filter(created__gt=datetime.datetime.now() - datetime.timedelta(days=added_since)).count()

    def videos_since(self, added_since):
        """Returns the videos of this team added the last n days.
        """
        return self.videos.filter(created__gt=datetime.datetime.now() - datetime.timedelta(days=added_since))

    def unassigned_tasks(self, sort=None):
        qs = Task.objects.filter(team=self, deleted=False, completed=None, assignee=None, type=Task.TYPE_IDS['Approve'])
        if sort is not None:
            qs = qs.order_by(sort)
        return qs

    def get_task(self, task_pk):
        return Task.objects.get(pk=task_pk)

    def get_tasks(self, task_pks):
        return Task.objects.filter(pk__in=task_pks).select_related('new_subtitle_version', 'new_subtitle_version__subtitle_language', 'team_video', 'team_video__video', 'team_video__video__teamvideo', 'workflow')

    def _count_tasks(self):
        qs = Task.objects.filter(team=self, deleted=False, completed=None)
        # quick, check, are there more than 1000 tasks, if so return 1001, and
        # let the UI display > 1000
        if qs[1000:1001].exists():
            return 1001
        else:
            return qs.count()

    @property
    def tasks_count(self):
        """Return the number of incomplete, undeleted tasks of this team.

        Caches the result in-object for performance.

        Note: the count is capped at 1001 tasks.  If a team has more than
        that, we generally just want to display "> 1000".  Use
        get_tasks_count_display() to do that.

        """
        if not hasattr(self, '_tasks_count'):
            setattr(self, '_tasks_count', self._count_tasks())
        return self._tasks_count

    def get_tasks_count_display(self):
        """Get a string to display for our tasks count."""
        if self.tasks_count <= 1000:
            return unicode(self.tasks_count)
        else:
            return ugettext('> 1000')

    # Applications (people applying to join)
    def application_message(self):
        """Return the membership application message for this team, or '' if none exists."""
        try:
            return self.settings.get(key=Setting.KEY_IDS['messages_application']).data
        except Setting.DoesNotExist:
            return ''

    @property
    def applications_count(self):
        """Return the number of open membership applications to this team.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_applications_count'):
            setattr(self, '_applications_count', self.applications.count())
        return self._applications_count

    # Projects
    @property
    def default_project(self):
        """Return the default project for this team.

        If it doesn't already exist it will be created.

        TODO: Move the creation into a signal on the team to avoid creating
        multiple default projects here?

        """
        try:
            return Project.objects.get(team=self, slug=Project.DEFAULT_NAME)
        except Project.DoesNotExist:
            p = Project(team=self,name=Project.DEFAULT_NAME)
            p.save()
            return p

    @property
    def has_projects(self):
        """Return whether this team has projects other than the default one."""
        return self.project_set.count() > 1


    # Readable/writeable language codes
    def get_writable_langs(self):
        """Return a list of language code strings that are writable for this team.

        This value may come from memcache.

        """
        return TeamLanguagePreference.objects.get_writable(self)

    def get_readable_langs(self):
        """Return a list of language code strings that are readable for this team.

        This value may come from memcache.

        """
        return TeamLanguagePreference.objects.get_readable(self)

    def get_team_languages(self, since=None):
        query_sl = NewSubtitleLanguage.objects.filter(video__in=self.videos.all())
        new_languages = []
        if since:
            query_sl = query_sl.filter(id__in=NewSubtitleVersion.objects.filter(video__in=self.videos.all(),
                                                                             created__gt=datetime.datetime.now() - datetime.timedelta(days=since)).order_by('subtitle_language').values_list('subtitle_language', flat=True).distinct())
            new_languages = list(NewSubtitleLanguage.objects.filter(video__in=self.videos_since(since)).values_list('language_code', 'subtitles_complete'))
        query_sl = query_sl.values_list('language_code', 'subtitles_complete')
        languages = list(query_sl)

        def first_member(x):
            return x[0]
        complete_languages = map(first_member, filter(lambda x: x[1], languages))
        incomplete_languages = map(first_member, filter(lambda x: not x[1], languages))
        new_languages = map(first_member, new_languages)
        if since:
            return (complete_languages, incomplete_languages, new_languages)
        else:
            return (complete_languages, incomplete_languages)

    def get_video_language_counts(self):
        """Count team videos for each langugage

        Returns: list of (language_code, count) tuples
        """
        return list(self.videos
                    .values_list('primary_audio_language_code')
                    .annotate(Count("id"))
                    .order_by())

    def get_completed_language_counts(self):
        from subtitles.models import SubtitleLanguage
        qs = (SubtitleLanguage.objects
              .filter(video__in=self.videos.all())
              .values_list('language_code')
              .annotate(Sum('subtitles_complete')))
        return [(lc, int(count)) for lc, count in qs]

# This needs to be constructed after the model definition since we need a
# reference to the class itself.
Team._meta.permissions = TEAM_PERMISSIONS

# Project
class ProjectManager(models.Manager):
    def for_team(self, team_identifier):
        """Return all non-default projects for the given team with the given identifier.

        The team_identifier passed may be an actual Team object, or a string
        containing a team slug, or the primary key of a team as an integer.

        """
        if hasattr(team_identifier, "pk"):
            team = team_identifier
        elif isinstance(team_identifier, int):
            team = Team.objects.get(pk=team_identifier)
        elif isinstance(team_identifier, str):
            team = Team.objects.get(slug=team_identifier)
        return Project.objects.filter(team=team).exclude(name=Project.DEFAULT_NAME)

class Project(models.Model):
    # All tvs belong to a project, wheather the team has enabled them or not
    # the default project is just a convenience UI that pretends to be part of
    # the team . If this ever gets changed, you need to change migrations/0044
    DEFAULT_NAME = "_root"

    team = models.ForeignKey(Team)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(blank=True)

    name = models.CharField(max_length=255, null=False)
    description = models.TextField(blank=True, null=True, max_length=2048)
    guidelines = models.TextField(blank=True, null=True, max_length=2048)

    slug = models.SlugField(blank=True)
    order = models.PositiveIntegerField(default=0)

    workflow_enabled = models.BooleanField(default=False)

    objects = ProjectManager()

    def __unicode__(self):
        if self.is_default_project:
            return u"---------"
        return u"%s" % (self.name)

    def display(self, default_project_label=None):
        if self.is_default_project and default_project_label is not None:
            return default_project_label
        return self.__unicode__()

    def save(self, slug=None,*args, **kwargs):
        self.modified = datetime.datetime.now()
        if slug is not None:
            self.slug = pan_slugify(slug)
        elif not self.slug:
            self.slug = pan_slugify(self.name)
        super(Project, self).save(*args, **kwargs)

    @property
    def is_default_project(self):
        """Return whether this project is a default project for a team."""
        return self.name == Project.DEFAULT_NAME


    def get_site_url(self):
        """Return the full, absolute URL for this project, including http:// and the domain."""
        return '%s://%s%s' % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, self.get_absolute_url())

    def get_absolute_url(self):
        if self.team.is_old_style():
            return reverse('teams:project_video_list',
                           args=(self.team.slug, self.slug))
        else:
            # TODO implement project landing page for new-style teams
            return reverse('teams:project', args=(self.team.slug, self.slug))

    def potential_managers(self):
        member_qs = (TeamMember.objects
                     .filter(team_id=self.team_id)
                     .exclude(projects_managed=self))
        return User.objects.filter(team_members__in=member_qs)

    @property
    def videos_count(self):
        """Return the number of videos in this project.

        Caches the result in-object for performance.

        """
        if not hasattr(self, '_videos_count'):
            setattr(self, '_videos_count', TeamVideo.objects.filter(project=self).count())
        return self._videos_count

    def _count_tasks(self):
        qs = tasks.filter(team_video__project = self)
        # quick, check, are there more than 1000 tasks, if so return 1001, and
        # let the UI display > 1000
        if qs[1000:1001].exists():
            return 1001
        else:
            return qs.count()

    @property
    def tasks_count(self):
        """Return the number of incomplete, undeleted tasks in this project.

        Caches the result in-object for performance.

        """
        tasks = Task.objects.filter(team=self.team, deleted=False, completed=None)

        if not hasattr(self, '_tasks_count'):
            setattr(self, '_tasks_count', self._count_tasks())
        return self._tasks_count

    class Meta:
        unique_together = (
                ("team", "name",),
                ("team", "slug",),
        )
        permissions = PROJECT_PERMISSIONS


# TeamVideo
class TeamVideo(models.Model):
    THUMBNAIL_SIZE = (288, 162)

    team = models.ForeignKey(Team)
    video = models.OneToOneField(Video)
    description = models.TextField(blank=True,
        help_text=_(u'Use this space to explain why you or your team need to '
                    u'caption or subtitle this video. Adding a note makes '
                    u'volunteers more likely to help out!'))
    thumbnail = S3EnabledImageField(upload_to='teams/video_thumbnails/', null=True, blank=True,
        help_text=_(u'We automatically grab thumbnails for certain sites, e.g. Youtube'),
                                    thumb_sizes=(THUMBNAIL_SIZE, (120,90),))
    all_languages = models.BooleanField(_('Need help with all languages'), default=False,
        help_text=_(u'If you check this, other languages will not be displayed.'))
    added_by = models.ForeignKey(User, null=True)
    # this is an auto_add like field, but done on the model save so the
    # admin doesn't throw a fit
    created = models.DateTimeField(blank=True)
    partner_id = models.CharField(max_length=100, blank=True, default="")

    project = models.ForeignKey(Project)

    class Meta:
        unique_together = (('team', 'video'),)

    def __unicode__(self):
        return unicode(self.video)

    @models.permalink
    def get_absolute_url(self):
        return ('teams:team_video', [self.pk])

    def get_tasks_page_url(self):
        return "%s?team_video=%s" % (self.team.get_tasks_page_url(), self.pk)

    def get_thumbnail(self):
        if self.thumbnail:
            return self.thumbnail.thumb_url(*TeamVideo.THUMBNAIL_SIZE)

        video_thumb = self.video.get_thumbnail(fallback=False)
        if video_thumb:
            return video_thumb

        return "%simages/video-no-thumbnail-medium.png" % settings.STATIC_URL

    def _original_language(self):
        if not hasattr(self, 'original_language_code'):
            sub_lang = self.video.subtitle_language()
            setattr(self, 'original_language_code', None if not sub_lang else sub_lang.language)
        return getattr(self, 'original_language_code')

    def save(self, *args, **kwargs):
        if not hasattr(self, "project"):
            self.project = self.team.default_project

        assert self.project.team == self.team, \
                    "%s: Team (%s) is not equal to project's (%s) team (%s)"\
                         % (self, self.team, self.project, self.project.team)

        if not self.pk:
            self.created = datetime.datetime.now()
        self.video.cache.invalidate()
        self.video.clear_team_video_cache()
        super(TeamVideo, self).save(*args, **kwargs)

    def is_checked_out(self, ignore_user=None):
        '''Return whether this video is checked out in a task.

        If a user is given, checkouts by that user will be ignored.  This
        provides a way to ask "can user X check out or work on this task?".

        This is similar to the writelocking done on Videos and
        SubtitleLanguages.

        '''
        tasks = self.task_set.filter(
                # Find all tasks for this video which:
                deleted=False,           # - Aren't deleted
                assignee__isnull=False,  # - Are assigned to someone
                language="",             # - Aren't specific to a language
                completed__isnull=True,  # - Are unfinished
        )
        if ignore_user:
            tasks = tasks.exclude(assignee=ignore_user)

        return tasks.exists()


    # Convenience functions
    def subtitles_started(self):
        """Return whether subtitles have been started for this video."""
        from subtitles.models import SubtitleLanguage
        return (SubtitleLanguage.objects.having_nonempty_versions()
                                        .filter(video=self.video)
                                        .exists())

    def subtitles_finished(self):
        """Return whether at least one set of subtitles has been finished for this video."""
        qs = (self.video.newsubtitlelanguage_set.having_public_versions()
              .filter(subtitles_complete=True))
        for lang in qs:
            if lang.is_synced():
                return True
        return False

    def get_workflow(self):
        """Return the appropriate Workflow for this TeamVideo."""
        return Workflow.get_for_team_video(self)

    def move_to(self, new_team, project=None):
        """
        Moves this TeamVideo to a new team.
        This method expects you to have run the correct permissions checks.
        """
        old_team = self.team
        if old_team == new_team and project == self.project:
            return
        within_team = (old_team == new_team)
        # these imports are here to avoid circular imports, hacky
        from teams.signals import api_teamvideo_new
        from teams.signals import video_moved_from_team_to_team
        from videos import metadata_manager
        # For now, we'll just delete any tasks associated with the moved video.
        if not within_team:
            self.task_set.update(deleted=True)

            # We move the video by just switching the team, instead of deleting and
            # recreating it.
            self.team = new_team

        # projects are always team dependent:
        if project:
            self.project = project
        else:
            self.project = new_team.default_project

        self.save()

        if not within_team:
            # We need to make any as-yet-unmoderated versions public.
            # TODO: Dedupe this and the team video delete signal.
            video = self.video

            video.newsubtitleversion_set.extant().update(visibility='public')
            video.is_public = new_team.is_visible
            video.moderated_by = new_team if new_team.moderates_videos() else None
            video.save()

            TeamVideoMigration.objects.create(from_team=old_team,
                                              to_team=new_team,
                                              to_project=self.project)

            # Create any necessary tasks.
            autocreate_tasks(self)

            # fire a http notification that a new video has hit this team:
            api_teamvideo_new.send(self)
            video_moved_from_team_to_team.send(sender=self,
                                               destination_team=new_team, video=self.video)
        # Update search data and other things
        video_changed_tasks.delay(self.video_id)

    def get_task_for_editor(self, language_code):
        if not hasattr(self, '_editor_task'):
            self._editor_task = self._get_task_for_editor(language_code)
        return self._editor_task

    def _get_task_for_editor(self, language_code):
        task_set = self.task_set.incomplete().filter(language=language_code)
        # 2533: We can get 2 review tasks if we include translate/transcribe
        # tasks in the results.  This is because when we have a task id and
        # the user clicks endorse, we do the following:
        #    - save the subtitles
        #    - save the task, setting subtitle_version to the version that we
        #      just saved
        #
        # However, the task code creates a task on both of those steps.  I'm not
        # sure exactly what the old editor does to make this not happen, but
        # it's safest to just not send task_id in that case
        task_set = task_set.filter(type__in=(Task.TYPE_IDS['Review'],
                                             Task.TYPE_IDS['Approve']))
        # This assumes there is only 1 incomplete tasks at once, hopefully
        # that's a good enough assumption to hold until we dump tasks for the
        # collab model.
        tasks = list(task_set[:1])
        if tasks:
            return tasks[0]
        else:
            return None

class TeamVideoMigration(models.Model):
    from_team = models.ForeignKey(Team, related_name='+')
    to_team = models.ForeignKey(Team, related_name='+')
    to_project = models.ForeignKey(Project, related_name='+')
    datetime = models.DateTimeField()

    def __init__(self, *args, **kwargs):
        if 'datetime' not in kwargs:
            kwargs['datetime'] = self.now()
        models.Model.__init__(self, *args, **kwargs)

    @staticmethod
    def now():
        # Make now a function so we can patch it in the unittests
        return datetime.datetime.now()

def _create_translation_tasks(team_video, subtitle_version=None):
    """Create any translation tasks that should be autocreated for this video.

    subtitle_version should be the original SubtitleVersion that these tasks
    will probably be translating from.

    """
    preferred_langs = TeamLanguagePreference.objects.get_preferred(team_video.team)

    for lang in preferred_langs:
        # Don't create tasks for languages that are already complete.
        sl = team_video.video.subtitle_language(lang)
        if sl and sl.is_complete_and_synced():
            continue

        # Don't create tasks for languages that already have one.  This includes
        # review/approve tasks and such.
        # Doesn't matter if it's complete or not.
        task_exists = Task.objects.not_deleted().filter(
            team=team_video.team, team_video=team_video, language=lang
        ).exists()
        if task_exists:
            continue

        # Otherwise, go ahead and create it.
        task = Task(team=team_video.team, team_video=team_video,
                    language=lang, type=Task.TYPE_IDS['Translate'])

        # we should only update the team video after all tasks for
        # this video are saved, else we end up with a lot of
        # wasted tasks
        task.save()

def autocreate_tasks(team_video):
    workflow = Workflow.get_for_team_video(team_video)
    existing_subtitles = team_video.video.completed_subtitle_languages(public_only=True)

    # We may need to create a transcribe task, if there are no existing subs.
    if workflow.autocreate_subtitle and not existing_subtitles:
        if not team_video.task_set.not_deleted().exists():
            original_language = team_video.video.primary_audio_language_code
            Task(team=team_video.team,
                 team_video=team_video,
                 subtitle_version=None,
                 language= original_language or '',
                 type=Task.TYPE_IDS['Subtitle']
            ).save()

    # If there are existing subtitles, we may need to create translate tasks.
    #
    # TODO: This sets the "source version" for the translations to an arbitrary
    #       language's version.  In practice this probably won't be a problem
    #       because most teams will transcribe one language and then send to a
    #       new team for translation, but we can probably be smarter about this
    #       if we spend some time.
    if workflow.autocreate_translate and existing_subtitles:
        _create_translation_tasks(team_video)


def team_video_delete(sender, instance, **kwargs):
    """Perform necessary actions for when a TeamVideo is deleted.

    TODO: Split this up into separate signals.

    """
    from videos import metadata_manager
    try:
        video = instance.video

        # we need to publish all unpublished subs for this video:
        NewSubtitleVersion.objects.filter(video=video,
                visibility='private').update(visibility='public')

        video.is_public = True
        video.moderated_by = None
        video.save()

        metadata_manager.update_metadata(video.pk)
    except Video.DoesNotExist:
        pass
    if instance.video_id is not None:
        Video.cache.invalidate_by_pk(instance.video_id)

def on_language_deleted(sender, **kwargs):
    """When a language is deleted, delete all tasks associated with it."""
    team_video = sender.video.get_team_video()
    if not team_video:
        return
    Task.objects.filter(team_video=team_video,
                        language=sender.language_code).delete()
    # check if there are no more source languages for the video, and in that
    # case delete all transcribe tasks.  Don't delete:
    #     - transcribe tasks that have already been started
    #     - review tasks
    #     - approve tasks
    if not sender.video.has_public_version():
        # filtering on new_subtitle_version=None excludes all 3 cases where we
        # don't want to delete tasks
        Task.objects.filter(team_video=team_video,
                            new_subtitle_version=None).delete()

def team_video_autocreate_task(sender, instance, created, raw, **kwargs):
    """Create subtitle/translation tasks for a newly added TeamVideo, if necessary."""
    if created and not raw:
        autocreate_tasks(instance)

def team_video_add_video_moderation(sender, instance, created, raw, **kwargs):
    """Set the .moderated_by attribute on a newly created TeamVideo's Video, if necessary."""
    if created and not raw and instance.team.moderates_videos():
        instance.video.moderated_by = instance.team
        instance.video.save()

def team_video_rm_video_moderation(sender, instance, **kwargs):
    """Clear the .moderated_by attribute on a newly deleted TeamVideo's Video, if necessary."""
    try:
        # when removing a video, this will be triggered by the fk constraing
        # and will be already removed
        instance.video.moderated_by = None
        instance.video.save()
    except Video.DoesNotExist:
        pass


post_save.connect(team_video_autocreate_task, TeamVideo, dispatch_uid='teams.teamvideo.team_video_autocreate_task')
post_save.connect(team_video_add_video_moderation, TeamVideo, dispatch_uid='teams.teamvideo.team_video_add_video_moderation')
post_delete.connect(team_video_delete, TeamVideo, dispatch_uid="teams.teamvideo.team_video_delete")
post_delete.connect(team_video_rm_video_moderation, TeamVideo, dispatch_uid="teams.teamvideo.team_video_rm_video_moderation")
language_deleted.connect(on_language_deleted, dispatch_uid="teams.subtitlelanguage.language_deleted")

# TeamMember
class TeamMemberManager(models.Manager):
    use_for_related_fields = True

    def create_first_member(self, team, user):
        """Make sure that new teams always have an 'owner' member."""

        tm = TeamMember(team=team, user=user, role=ROLE_OWNER)
        tm.save()
        return tm

    def admins(self):
        return self.filter(role__in=(ROLE_OWNER, ROLE_ADMIN))

class TeamMember(models.Model):
    ROLE_OWNER = ROLE_OWNER
    ROLE_ADMIN = ROLE_ADMIN
    ROLE_MANAGER = ROLE_MANAGER
    ROLE_CONTRIBUTOR = ROLE_CONTRIBUTOR

    ROLES = (
        (ROLE_OWNER, _("Owner")),
        (ROLE_MANAGER, _("Manager")),
        (ROLE_ADMIN, _("Admin")),
        (ROLE_CONTRIBUTOR, _("Contributor")),
    )

    team = models.ForeignKey(Team, related_name='members')
    user = models.ForeignKey(User, related_name='team_members')
    role = models.CharField(max_length=16, default=ROLE_CONTRIBUTOR, choices=ROLES, db_index=True)
    created = models.DateTimeField(default=datetime.datetime.now, null=True,
            blank=True)

    # A project manager is a user who manages a project.  They have slightly
    # elavated permisions for that project and also new users can look to them
    # for help.
    projects_managed = models.ManyToManyField(Project,
                                              related_name='managers')

    objects = TeamMemberManager()

    def __unicode__(self):
        return u'%s' % self.user

    def save(self, *args, **kwargs):
        super(TeamMember, self).save(*args, **kwargs)
        Team.cache.invalidate_by_pk(self.team_id)

    def delete(self):
        super(TeamMember, self).delete()
        Team.cache.invalidate_by_pk(self.team_id)

    def project_narrowings(self):
        """Return any project narrowings applied to this member."""
        return self.narrowings.filter(project__isnull=False)

    def language_narrowings(self):
        """Return any language narrowings applied to this member."""
        return self.narrowings.filter(project__isnull=True)


    def project_narrowings_fast(self):
        """Return any project narrowings applied to this member.

        Caches the result in-object for speed.

        """
        return [n for n in  self.narrowings_fast() if n.project]

    def language_narrowings_fast(self):
        """Return any language narrowings applied to this member.

        Caches the result in-object for speed.

        """
        return [n for n in self.narrowings_fast() if n.language]

    def narrowings_fast(self):
        """Return any narrowings (both project and language) applied to this member.

        Caches the result in-object for speed.

        """
        if hasattr(self, '_cached_narrowings'):
            if self._cached_narrowings is not None:
                return self._cached_narrowings

        self._cached_narrowings = self.narrowings.all()
        return self._cached_narrowings


    def has_max_tasks(self):
        """Return whether this member has the maximum number of tasks."""
        max_tasks = self.team.max_tasks_per_member
        if max_tasks:
            if self.user.task_set.incomplete().filter(team=self.team).count() >= max_tasks:
                return True
        return False

    def is_manager(self):
        """Test if the user is a manager or above."""
        return self.role in (ROLE_OWNER, ROLE_ADMIN, ROLE_MANAGER)

    def is_admin(self):
        """Test if the user is an admin or owner."""
        return self.role in (ROLE_OWNER, ROLE_ADMIN)

    def get_projects_managed(self):
        if not hasattr(self, '_projects_managed_cache'):
            self._projects_managed_cache = list(self.projects_managed.all())
        return self._projects_managed_cache

    def get_languages_managed(self):
        if not hasattr(self, '_languages_managed_cache'):
            self._languages_managed_cache = list(self.languages_managed.all())
        return self._languages_managed_cache

    def is_project_manager(self, project):
        if isinstance(project, Project):
            project_id = project.id
        else:
            project_id = project
        return project_id in (p.id for p in self.get_projects_managed())

    def is_language_manager(self, language_code):
        return (language_code in
                (l.code for l in self.get_languages_managed()))

    def make_project_manager(self, project):
        self.projects_managed.add(project)

    def remove_project_manager(self, project):
        self.projects_managed.remove(project)

    def make_language_manager(self, language_code):
        self.languages_managed.create(code=language_code)

    def remove_language_manager(self, language_code):
        self.languages_managed.filter(code=language_code).delete()

    class Meta:
        unique_together = (('team', 'user'),)

def clear_tasks(sender, instance, *args, **kwargs):
    """Unassign all tasks assigned to a user.

    Used when deleting a user from a team.

    """
    tasks = instance.team.task_set.incomplete().filter(assignee=instance.user)
    tasks.update(assignee=None)

pre_delete.connect(clear_tasks, TeamMember, dispatch_uid='teams.members.clear-tasks-on-delete')

class LanguageManager(models.Model):
    member = models.ForeignKey(TeamMember, related_name='languages_managed')
    code = models.CharField(max_length=16,
                            choices=translation.ALL_LANGUAGE_CHOICES)

# MembershipNarrowing
class MembershipNarrowing(models.Model):
    """Represent narrowings that can be made on memberships.

    A single MembershipNarrowing can apply to a project or a language, but not both.

    This model is deprecated and we're planning on replacing it with the
    projects_managed and languages_managed fields
    """
    member = models.ForeignKey(TeamMember, related_name="narrowings")
    project = models.ForeignKey(Project, null=True, blank=True)
    language = models.CharField(max_length=24, blank=True,
                                choices=translation.ALL_LANGUAGE_CHOICES)

    added_by = models.ForeignKey(TeamMember, related_name="narrowing_includer", null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, blank=None)
    modified = models.DateTimeField(auto_now=True, blank=None)

    def __unicode__(self):
        if self.project:
            return u"Permission restriction for %s to project %s " % (self.member, self.project)
        else:
            return u"Permission restriction for %s to language %s " % (self.member, self.language)


    def save(self, *args, **kwargs):
        # Cannot have duplicate narrowings for a language.
        if self.language:
            duplicate_exists = MembershipNarrowing.objects.filter(
                member=self.member, language=self.language
            ).exclude(id=self.id).exists()

            assert not duplicate_exists, "Duplicate language narrowing detected!"

        # Cannot have duplicate narrowings for a project.
        if self.project:
            duplicate_exists = MembershipNarrowing.objects.filter(
                member=self.member, project=self.project
            ).exclude(id=self.id).exists()

            assert not duplicate_exists, "Duplicate project narrowing detected!"

        super(MembershipNarrowing, self).save(*args, **kwargs)
        Team.cache.invalidate_by_pk(self.member.team_id)

    def delete(self):
        super(MembershipNarrowing, self).delete()
        Team.cache.invalidate_by_pk(self.member.team_id)

class TeamSubtitleNote(SubtitleNoteBase):
    team = models.ForeignKey(Team, related_name='+')

class ApplicationManager(models.Manager):

    def can_apply(self, team, user):
        """
        A user can apply either if he is not a member of the team yet, the
        team hasn't said no to the user (either application denied or removed the user'
        and if no applications are pending.
        """
        sour_application_exists =  self.filter(team=team, user=user, status__in=[
            Application.STATUS_MEMBER_REMOVED, Application.STATUS_DENIED,
            Application.STATUS_PENDING]).exists()
        if sour_application_exists:
            return False
        return  not team.is_member(user)

    def open(self, team=None, user=None):
        if user and not user.is_authenticated():
            return self.none()
        qs =  self.filter(status=Application.STATUS_PENDING)
        if team:
            qs = qs.filter(team=team)
        if user:
            qs = qs.filter(user=user)
        return qs


# Application
class Application(models.Model):
    team = models.ForeignKey(Team, related_name='applications')
    user = models.ForeignKey(User, related_name='team_applications')
    note = models.TextField(blank=True)
    # None -> not acted upon
    # True -> Approved
    # False -> Rejected
    STATUS_PENDING,STATUS_APPROVED, STATUS_DENIED, STATUS_MEMBER_REMOVED,\
        STATUS_MEMBER_LEFT = xrange(0, 5)
    STATUSES = (
        (STATUS_PENDING, u"Pending"),
        (STATUS_APPROVED, u"Approved"),
        (STATUS_DENIED, u"Denied"),
        (STATUS_MEMBER_REMOVED, u"Member Removed"),
        (STATUS_MEMBER_LEFT, u"Member Left"),
    )
    STATUSES_IDS = dict([choice[::-1] for choice in STATUSES])

    status = models.PositiveIntegerField(default=STATUS_PENDING, choices=STATUSES)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(blank=True, null=True)

    # free text keeping a log of changes to this application
    history = models.TextField(blank=True, null=True)

    objects = ApplicationManager()
    class Meta:
        unique_together = (('team', 'user', 'status'),)

    def check_can_submit(self):
        """Check if a user can submit this application

        Raises: ApplicationInvalidException if the user can't apply.  The
        message will be set explaining why.
        """
        if self.status == Application.STATUS_PENDING and self.pk is not None:
            raise ApplicationInvalidException(
                fmt(_(u'You already have a pending application to %(team)s.'),
                team=self.team)
            )
        elif self.status == Application.STATUS_DENIED:
            raise ApplicationInvalidException(
                _(u'Your application has been denied.')
            )
        elif self.status == Application.STATUS_MEMBER_REMOVED:
            raise ApplicationInvalidException(
                fmt(_(u'You have been removed from %(team)s.'),
                    team=self.team)
            )

    def approve(self, author, interface):
        """Approve the application.

        This will create an appropriate TeamMember if this application has
        not been already acted upon
        """
        if self.status not in (Application.STATUS_PENDING, Application.STATUS_MEMBER_LEFT):
            raise ApplicationInvalidException("")
        member, created = TeamMember.objects.get_or_create(team=self.team, user=self.user)
        if created:
            notifier.team_member_new.delay(member.pk)
        self.modified = datetime.datetime.now()
        self.status = Application.STATUS_APPROVED
        self.save(author=author, interface=interface)
        return self

    def deny(self, author, interface):
        """
        Marks the application as not approved, then
        Queue a Celery task that will handle properly denying this
        application.
        """
        if self.status != Application.STATUS_PENDING:
            raise ApplicationInvalidException("")
        self.modified = datetime.datetime.now()
        self.status = Application.STATUS_DENIED
        self.save(author=author, interface=interface)
        notifier.team_application_denied.delay(self.pk)
        return self

    def on_member_leave(self, author, interface):
        """
        Marks the appropriate status, but users can still
        reapply to a team if they so desire later.
        """
        self.status = Application.STATUS_MEMBER_LEFT
        self.save(author=author, interface=interface)

    def on_member_removed(self, author, interface):
        """
        Marks the appropriate status so that user's cannot reapply
        to a team after being removed.
        """
        self.status = Application.STATUS_MEMBER_REMOVED
        self.save(author=author, interface=interface)

    def _generate_history_line(self, new_status, author=None, interface=None):
        author = author or "?"
        interface = interface or "web UI"
        new_status = new_status if new_status != None else Application.STATUS_PENDING
        for value,name in Application.STATUSES:
            if value == new_status:
                status = name
        assert status
        return u"%s by %s from %s (%s)\n" % (status, author, interface, datetime.datetime.now())

    def save(self, dispatches_http_callback=True, author=None, interface=None, *args, **kwargs):
        """
        Saves the model, but also appends a line on the history for that
        model, like these:
           - CoolGuy Approved through the web UI.
           - Arthur Left team through the web UI.
        This way,we can keep one application per user per team, never
        delete them (so the messages stay current) and we still can
        track history
        """
        self.history = (self.history or "") + self._generate_history_line(self.status, author, interface)
        super(Application, self).save(*args, **kwargs)
        if dispatches_http_callback:
            from teams.signals import api_application_new
            api_application_new.send(self)

    def __unicode__(self):
        return "Application: %s - %s - %s" % (self.team.slug, self.user.username, self.get_status_display())


# Invites
class InviteExpiredException(Exception):
    pass

class InviteManager(models.Manager):
    def pending_for(self, team, user=None):
        if user is not None:
            return self.filter(team=team, user=user, approved=None)
        else:
            return self.filter(team=team, approved=None)

    def acted_on(self, team, user):
        return self.filter(team=team, user=user, approved__notnull=True)

class Invite(models.Model):
    team = models.ForeignKey(Team, related_name='invitations')
    user = models.ForeignKey(User, related_name='team_invitations')
    note = models.TextField(blank=True, max_length=200)
    author = models.ForeignKey(User)
    role = models.CharField(max_length=16, choices=TeamMember.ROLES,
                            default=TeamMember.ROLE_CONTRIBUTOR)
    # None -> not acted upon
    # True -> Approved
    # False -> Rejected
    approved = models.NullBooleanField(default=None)

    objects = InviteManager()

    def accept(self):
        """Accept this invitation.

        Creates an appropriate TeamMember record, sends a notification and
        deletes itself.

        """
        if self.approved is not None:
            raise InviteExpiredException("")
        self.approved = True
        member, created = TeamMember.objects.get_or_create(
            team=self.team, user=self.user, role=self.role)
        if created:
            notifier.team_member_new.delay(member.pk)
        self.save()
        return True

    def deny(self):
        """Deny this invitation.

        Could be useful to send a notification here in the future.

        """
        if self.approved is not None:
            raise InviteExpiredException("")
        self.approved = False
        self.save()


    def message_json_data(self, data, msg):
        data['can-reply'] = False
        return data


# Workflows
class Workflow(models.Model):
    REVIEW_CHOICES = (
        (00, "Don't require review"),
        (10, 'Peer must review'),
        (20, 'Manager must review'),
        (30, 'Admin must review'),
    )
    REVIEW_NAMES = dict(REVIEW_CHOICES)
    REVIEW_IDS = dict([choice[::-1] for choice in REVIEW_CHOICES])

    APPROVE_CHOICES = (
        (00, "Don't require approval"),
        (10, 'Manager must approve'),
        (20, 'Admin must approve'),
    )
    APPROVE_NAMES = dict(APPROVE_CHOICES)
    APPROVE_IDS = dict([choice[::-1] for choice in APPROVE_CHOICES])

    team = models.ForeignKey(Team)

    project = models.ForeignKey(Project, blank=True, null=True)
    team_video = models.ForeignKey(TeamVideo, blank=True, null=True)

    autocreate_subtitle = models.BooleanField(default=False)
    autocreate_translate = models.BooleanField(default=False)

    review_allowed = models.PositiveIntegerField(
            choices=REVIEW_CHOICES, verbose_name='reviewers', default=0)

    approve_allowed = models.PositiveIntegerField(
            choices=APPROVE_CHOICES, verbose_name='approvers', default=0)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        unique_together = ('team', 'project', 'team_video')


    @classmethod
    def _get_target_team(cls, id, type):
        """Return the team for the given target.

        The target is identified by id (its PK as an integer) and type (a string
        of 'team_video', 'project', or 'team').

        """
        if type == 'team_video':
            return TeamVideo.objects.select_related('team').get(pk=id).team
        elif type == 'project':
            return Project.objects.select_related('team').get(pk=id).team
        else:
            return Team.objects.get(pk=id)

    @classmethod
    def get_for_target(cls, id, type, workflows=None):
        '''Return the most specific Workflow for the given target.

        If target object does not exist, None is returned.

        If workflows is given, it should be a QS or List of all Workflows for
        the TeamVideo's team.  This will let you look it up yourself once and
        use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        '''
        if not workflows:
            team = Workflow._get_target_team(id, type)
            workflows = list(Workflow.objects.filter(team=team.id)
                                             .select_related('project', 'team',
                                                             'team_video'))
        else:
            team = workflows[0].team

        default_workflow = Workflow(team=team)

        if not workflows:
            return default_workflow

        if type == 'team_video':
            try:
                return [w for w in workflows
                        if w.team_video and w.team_video.id == id][0]
            except IndexError:
                # If there's no video-specific workflow for this video, there
                # might be a workflow for its project, so we'll start looking
                # for that instead.
                team_video = TeamVideo.objects.get(pk=id)
                id, type = team_video.project_id, 'project'

        if type == 'project':
            try:
                return [w for w in workflows
                        if w.project and w.project.workflow_enabled
                        and w.project.id == id and not w.team_video][0]
            except IndexError:
                # If there's no project-specific workflow for this project,
                # there might be one for its team, so we'll fall through.
                pass

        if not team.workflow_enabled:
            return default_workflow

        return [w for w in workflows
                if (not w.project) and (not w.team_video)][0]


    @classmethod
    def get_for_team_video(cls, team_video, workflows=None):
        '''Return the most specific Workflow for the given team_video.

        If workflows is given, it should be a QuerySet or List of all Workflows
        for the TeamVideo's team.  This will let you look it up yourself once
        and use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        NOTE: This function caches the workflow for performance reasons.  If the
        workflow changes within the space of a single request that
        _cached_workflow should be cleared.

        '''
        if not hasattr(team_video, '_cached_workflow'):
            team_video._cached_workflow = Workflow.get_for_target(
                    team_video.id, 'team_video', workflows)
        return team_video._cached_workflow

    @classmethod
    def get_for_project(cls, project, workflows=None):
        '''Return the most specific Workflow for the given project.

        If workflows is given, it should be a QuerySet or List of all Workflows
        for the Project's team.  This will let you look it up yourself once
        and use it in many of these calls to avoid hitting the DB each time.

        If workflows is not given it will be looked up with one DB query.

        '''
        return Workflow.get_for_target(project.id, 'project', workflows)

    @classmethod
    def add_to_team_videos(cls, team_videos):
        '''Add the appropriate Workflow objects to each TeamVideo as .workflow.

        This will only perform one DB query, and it will add the most specific
        workflow possible to each TeamVideo.

        This only exists for performance reasons.

        '''
        if not team_videos:
            return []

        workflows = list(Workflow.objects.filter(team=team_videos[0].team))

        for tv in team_videos:
            tv.workflow = Workflow.get_for_team_video(tv, workflows)


    def get_specific_target(self):
        """Return the most specific target that this workflow applies to."""
        return self.team_video or self.project or self.team


    def __unicode__(self):
        target = self.get_specific_target()
        return u'Workflow %s for %s (%s %d)' % (
                self.pk, target, target.__class__.__name__, target.pk)


    # Convenience functions for checking if a step of the workflow is enabled.
    @property
    def review_enabled(self):
        """Return whether any form of review is enabled for this workflow."""
        return True if self.review_allowed else False

    @property
    def approve_enabled(self):
        """Return whether any form of approval is enabled for this workflow."""
        return True if self.approve_allowed else False

    @property
    def requires_review_or_approval(self):
        """Return whether a given workflow requires review or approval."""
        return self.approve_enabled or self.review_enabled

    @property
    def requires_tasks(self):
        """Return whether a given workflow requires the use of tasks."""
        return (self.requires_review_or_approval or self.autocreate_subtitle
                or self.autocreate_translate)


# Tasks
class TaskManager(models.Manager):
    def not_deleted(self):
        """Return a QS of tasks that are not deleted."""
        return self.get_query_set().filter(deleted=False)


    def incomplete(self):
        """Return a QS of tasks that are not deleted or completed."""
        return self.not_deleted().filter(completed=None)

    def complete(self):
        """Return a QS of tasks that are not deleted, but are completed."""
        return self.not_deleted().filter(completed__isnull=False)

    def _type(self, types, completed=None, approved=None):
        """Return a QS of tasks that are not deleted and are one of the given types.

        types should be a list of strings matching a label in Task.TYPE_CHOICES.

        completed should be one of:

        * True (only show completed tasks)
        * False (only show incomplete tasks)
        * None (don't filter on completion status)

        approved should be either None or a string matching a label in
        Task.APPROVED_CHOICES.

        """
        type_ids = [Task.TYPE_IDS[type] for type in types]
        qs = self.not_deleted().filter(type__in=type_ids)

        if completed == False:
            qs = qs.filter(completed=None)
        elif completed == True:
            qs = qs.filter(completed__isnull=False)

        if approved:
            qs = qs.filter(approved=Task.APPROVED_IDS[approved])

        return qs


    def incomplete_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted or completed."""
        return self._type(['Subtitle'], False)

    def incomplete_translate(self):
        """Return a QS of translate tasks that are not deleted or completed."""
        return self._type(['Translate'], False)

    def incomplete_review(self):
        """Return a QS of review tasks that are not deleted or completed."""
        return self._type(['Review'], False)

    def incomplete_approve(self):
        """Return a QS of approve tasks that are not deleted or completed."""
        return self._type(['Approve'], False)

    def incomplete_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted or completed."""
        return self._type(['Subtitle', 'Translate'], False)

    def incomplete_review_or_approve(self):
        """Return a QS of review or approve tasks that are not deleted or completed."""
        return self._type(['Review', 'Approve'], False)


    def complete_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted, but are completed."""
        return self._type(['Subtitle'], True)

    def complete_translate(self):
        """Return a QS of translate tasks that are not deleted, but are completed."""
        return self._type(['Translate'], True)

    def complete_review(self, approved=None):
        """Return a QS of review tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Review'], True, approved)

    def complete_approve(self, approved=None):
        """Return a QS of approve tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Approve'], True, approved)

    def complete_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted, but are completed."""
        return self._type(['Subtitle', 'Translate'], True)

    def complete_review_or_approve(self, approved=None):
        """Return a QS of review or approve tasks that are not deleted, but are completed.

        If approved is given the tasks are further filtered on their .approved
        attribute.  It must be a string matching one of the labels in
        Task.APPROVED_CHOICES, like 'Rejected'.

        """
        return self._type(['Review', 'Approve'], True, approved)


    def all_subtitle(self):
        """Return a QS of subtitle tasks that are not deleted."""
        return self._type(['Subtitle'])

    def all_translate(self):
        """Return a QS of translate tasks that are not deleted."""
        return self._type(['Translate'])

    def all_review(self):
        """Return a QS of review tasks that are not deleted."""
        return self._type(['Review'])

    def all_approve(self):
        """Return a QS of tasks that are not deleted."""
        return self._type(['Approve'])

    def all_subtitle_or_translate(self):
        """Return a QS of subtitle or translate tasks that are not deleted."""
        return self._type(['Subtitle', 'Translate'])

    def all_review_or_approve(self):
        """Return a QS of review or approve tasks that are not deleted."""
        return self._type(['Review', 'Approve'])

class Task(models.Model):
    TYPE_CHOICES = (
        (10, 'Subtitle'),
        (20, 'Translate'),
        (30, 'Review'),
        (40, 'Approve'),
    )
    TYPE_NAMES = dict(TYPE_CHOICES)
    TYPE_IDS = dict([choice[::-1] for choice in TYPE_CHOICES])

    APPROVED_CHOICES = (
        (10, 'In Progress'),
        (20, 'Approved'),
        (30, 'Rejected'),
    )
    APPROVED_NAMES = dict(APPROVED_CHOICES)
    APPROVED_IDS = dict([choice[::-1] for choice in APPROVED_CHOICES])
    APPROVED_FINISHED_IDS = (20, 30)

    type = models.PositiveIntegerField(choices=TYPE_CHOICES)

    team = models.ForeignKey(Team)
    team_video = models.ForeignKey(TeamVideo)
    language = models.CharField(max_length=16,
                                choices=translation.ALL_LANGUAGE_CHOICES,
                                blank=True, db_index=True)
    assignee = models.ForeignKey(User, blank=True, null=True)
    subtitle_version = models.ForeignKey(SubtitleVersion, blank=True, null=True)
    new_subtitle_version = models.ForeignKey(NewSubtitleVersion,
                                             blank=True, null=True)

    # The original source version being reviewed or approved.
    #
    # For example, if person A creates two versions while working on a subtitle
    # task:
    #
    #  v1  v2
    # --o---o
    #   s   s
    #
    # and then the reviewer and approver make some edits
    #
    #  v1  v2  v3  v4  v5
    # --o---o---o---o---o
    #   s   s   r   r   a
    #       *
    #
    # the review_base_version will be v2.  Once approved, if an edit is made it
    # needs to be approved as well, and the same thing happens:
    #
    #  v1  v2  v3  v4  v5  v6  v7
    # --o---o---o---o---o---o---o
    #   s   s   r   r   a   e   a
    #                       *
    #
    # This is used when rejecting versions, and may be used elsewhere in the
    # future as well.
    review_base_version = models.ForeignKey(SubtitleVersion, blank=True,
                                            null=True,
                                            related_name='tasks_based_on')
    new_review_base_version = models.ForeignKey(NewSubtitleVersion, blank=True,
                                                null=True,
                                                related_name='tasks_based_on_new')

    deleted = models.BooleanField(default=False)

    # TODO: Remove this field.
    public = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)
    completed = models.DateTimeField(blank=True, null=True)
    expiration_date = models.DateTimeField(blank=True, null=True)

    # Arbitrary priority for tasks. Some teams might calculate this
    # on complex criteria and expect us to be able to sort tasks on it.
    # Higher numbers mean higher priority
    priority = models.PositiveIntegerField(blank=True, default=0, db_index=True)
    # Review and Approval -specific fields
    approved = models.PositiveIntegerField(choices=APPROVED_CHOICES,
                                           null=True, blank=True)
    body = models.TextField(blank=True, default="")

    objects = TaskManager()

    def __unicode__(self):
        return u'Task %s (%s) for %s' % (self.id or "unsaved",
                                         self.get_type_display(),
                                         self.team_video)
    @property
    def summary(self):
        """
        Return a brief summary of the task
        """
        output = unicode(self.team_video)
        if self.body:
            output += unicode(self.body.split('\n',1)[0].strip()[:20])
        return output

    @staticmethod
    def now():
        """datetime.datetime.now as a method

        This lets us patch it in the unittests.
        """
        return datetime.datetime.now()

    def is_subtitle_task(self):
        return self.type == Task.TYPE_IDS['Subtitle']

    def is_translate_task(self):
        return self.type == Task.TYPE_IDS['Translate']

    def is_review_task(self):
        return self.type == Task.TYPE_IDS['Review']

    def is_approve_task(self):
        return self.type == Task.TYPE_IDS['Approve']

    @property
    def workflow(self):
        '''Return the most specific workflow for this task's TeamVideo.'''
        return Workflow.get_for_team_video(self.team_video)

    @staticmethod
    def add_cached_video_urls(tasks):
        """Add the cached_video_url attribute to a list of atkss

        cached_video_url is the URL as a string for the video.
        """
        team_video_pks = [t.team_video_id for t in tasks]
        video_urls = (VideoUrl.objects
                      .filter(video__teamvideo__id__in=team_video_pks)
                      .filter(primary=True))
        video_url_map = dict((vu.video_id, vu.effective_url)
                             for vu in video_urls)
        for t in tasks:
            t.cached_video_url = video_url_map.get(t.team_video.video_id)


    def _add_comment(self, lang_ct=None):
        """Add a comment on the SubtitleLanguage for this task with the body as content."""
        if self.body.strip():
            if lang_ct is None:
                lang_ct = ContentType.objects.get_for_model(NewSubtitleLanguage)
            comment = Comment(
                content=self.body,
                object_pk=self.new_subtitle_version.subtitle_language.pk,
                content_type=lang_ct,
                submit_date=self.completed,
                user=self.assignee,
            )
            comment.save()
            notifier.send_video_comment_notification.delay(
                comment.pk, version_pk=self.new_subtitle_version.pk)

    def future(self):
        """Return whether this task expires in the future."""
        return self.expiration_date > self.now()

    # Functions related to task completion.
    def _send_back(self, sends_notification=True):
        """Handle "rejection" of this task.

        This will:

        * Create a new task with the appropriate type (translate or subtitle).
        * Try to reassign it to the previous assignee, leaving it unassigned
          if that's not possible.
        * Send a notification unless sends_notification is given as False.

        NOTE: This function does not modify the *current* task in any way.

        """
        # when sending back, instead of always sending back
        # to the first step (translate/subtitle) go to the
        # step before this one:
        # Translate/Subtitle -> Review -> Approve
        # also, you can just send back approve and review tasks.
        if self.type == Task.TYPE_IDS['Approve'] and self.workflow.review_enabled:
            type = Task.TYPE_IDS['Review']
        else:
            is_primary = (self.new_subtitle_version
                              .subtitle_language
                              .is_primary_audio_language())
            if is_primary:
                type = Task.TYPE_IDS['Subtitle']
            else:
                type = Task.TYPE_IDS['Translate']

        # let's guess which assignee should we use
        # by finding the last user that did this task type
        previous_task = Task.objects.complete().filter(
            team_video=self.team_video, language=self.language, team=self.team, type=type
        ).order_by('-completed')[:1]

        if previous_task:
            assignee = previous_task[0].assignee
        else:
            assignee = None

        # The target assignee may have left the team in the mean time.
        if not self.team.members.filter(user=assignee).exists():
            assignee = None

        task = Task(team=self.team, team_video=self.team_video,
                    language=self.language, type=type,
                    assignee=assignee)

        task.new_subtitle_version = self.new_subtitle_version

        task.set_expiration()

        task.save()

        if sends_notification:
            # notify original submiter (assignee of self)
            notifier.reviewed_and_sent_back.delay(self.pk)
        return task

    def complete_approved(self, user):
        """Mark a review/approve task as Approved and complete it.

        :param user: user who is approving he task
        :returns: next task in the workflow.
        """
        self.assignee = user
        self.approved = Task.APPROVED_IDS['Approved']
        return self.complete()

    def complete_rejected(self, user):
        """Mark a review/approve task as Rejected and complete it.

        :param user: user who is approving he task
        :returns: next task in the workflow.
        """
        self.assignee = user
        self.approved = Task.APPROVED_IDS['Rejected']
        return self.complete()

    def complete(self):
        '''Mark as complete and return the next task in the process if applicable.'''

        self.completed = self.now()
        self.save()

        return { 'Subtitle': self._complete_subtitle,
                 'Translate': self._complete_translate,
                 'Review': self._complete_review,
                 'Approve': self._complete_approve,
        }[Task.TYPE_NAMES[self.type]]()

    def _can_publish_directly(self, subtitle_version):
        from teams.permissions import can_publish_edits_immediately

        type = {10: 'Review',
                20: 'Review',
                30: 'Approve'}.get(self.type)

        tasks = (Task.objects._type([type], True, 'Approved')
                             .filter(language=self.language))

        return (can_publish_edits_immediately(self.team_video,
                                                    self.assignee,
                                                    self.language) and
                subtitle_version and
                subtitle_version.previous_version() and
                subtitle_version.previous_version().is_public() and
                subtitle_version.subtitle_language.is_complete_and_synced() and
                tasks.exists())

    def _find_previous_assignee(self, type):
        """Find the previous assignee for a new review/approve task for this video.

        NOTE: This is different than finding out the person to send a task back
              to!  This is for saying "who reviewed this task last time?".

        For now, we'll assign the review/approval task to whomever did it last
        time (if it was indeed done), but only if they're still eligible to
        perform it now.

        """
        from teams.permissions import can_review, can_approve

        if type == 'Approve':
            # Check if this is a post-publish edit.
            # According to #1039 we don't wanna auto-assign the assignee
            version = self.get_subtitle_version()
            if (version and 
                version.is_public() and
                version.subtitle_language.is_complete_and_synced()):
                return None

            type = Task.TYPE_IDS['Approve']
            can_do = can_approve
        elif type == 'Review':
            type = Task.TYPE_IDS['Review']
            can_do = partial(can_review, allow_own=True)
        else:
            return None

        last_task = self.team_video.task_set.complete().filter(
            language=self.language, type=type
        ).order_by('-completed')[:1]

        if last_task:
            candidate = last_task[0].assignee
            if candidate and can_do(self.team_video, candidate, self.language):
                return candidate

    def _complete_subtitle(self):
        """Handle the messy details of completing a subtitle task."""
        sv = self.get_subtitle_version()

        # TL;DR take a look at #1206 to know why i did this
        if self.workflow.requires_review_or_approval and not self._can_publish_directly(sv):

            if self.workflow.review_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            new_subtitle_version=sv,
                            new_review_base_version=sv,
                            language=self.language, type=Task.TYPE_IDS['Review'],
                            assignee=self._find_previous_assignee('Review'))
                task.set_expiration()
                task.save()
            elif self.workflow.approve_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            new_subtitle_version=sv,
                            new_review_base_version=sv,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()
        else:
            # Subtitle task is done, and there is no approval or review
            # required, so we mark the version as approved.
            sv.publish()

            # We need to make sure this is updated correctly here.
            from videos import metadata_manager
            metadata_manager.update_metadata(self.team_video.video.pk)

            if self.workflow.autocreate_translate:
                # TODO: Switch to autocreate_task?
                _create_translation_tasks(self.team_video, sv)

            task = None
        return task

    def _complete_translate(self):
        """Handle the messy details of completing a translate task."""
        sv = self.get_subtitle_version()

        # TL;DR take a look at #1206 to know why i did this
        if self.workflow.requires_review_or_approval and not self._can_publish_directly(sv):

            if self.workflow.review_enabled:
                task = Task(team=self.team, team_video=self.team_video,
                            new_subtitle_version=sv,
                            new_review_base_version=sv,
                            language=self.language, type=Task.TYPE_IDS['Review'],
                            assignee=self._find_previous_assignee('Review'))
                task.set_expiration()
                task.save()
            elif self.workflow.approve_enabled:
                # The review step may be disabled.  If so, we check the approve step.
                task = Task(team=self.team, team_video=self.team_video,
                            new_subtitle_version=sv,
                            new_review_base_version=sv,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()
        else:
            sv.publish()

            # We need to make sure this is updated correctly here.
            from videos import metadata_manager
            metadata_manager.update_metadata(self.team_video.video.pk)

            task = None

        return task

    def _complete_review(self):
        """Handle the messy details of completing a review task."""
        approval = self.approved == Task.APPROVED_IDS['Approved']
        sv = self.get_subtitle_version()
        if approval:
            self._ensure_language_complete(sv.subtitle_language)

        self._add_comment()

        task = None
        if self.workflow.approve_enabled:
            # Approval is enabled, so...
            if approval:
                # If the reviewer thought these subtitles were good we create
                # the next task.
                task = Task(team=self.team, team_video=self.team_video,
                            new_subtitle_version=sv,
                            new_review_base_version=sv,
                            language=self.language, type=Task.TYPE_IDS['Approve'],
                            assignee=self._find_previous_assignee('Approve'))
                task.set_expiration()
                task.save()

                # Notify the appropriate users.
                notifier.reviewed_and_pending_approval.delay(self.pk)
            else:
                # Otherwise we send the subtitles back for improvement.
                task = self._send_back()
        else:
            # Approval isn't enabled, so the ruling of this Review task
            # determines whether the subtitles go public.
            if approval:
                # Make these subtitles public!
                self.new_subtitle_version.publish()

                # If the subtitles are okay, go ahead and autocreate translation
                # tasks if necessary.
                if self.workflow.autocreate_translate:
                    _create_translation_tasks(self.team_video, sv)

                # Notify the appropriate users and external services.
                notifier.reviewed_and_published.delay(self.pk)
            else:
                # Send the subtitles back for improvement.
                task = self._send_back()

        # Before we go, we need to record who reviewed these subtitles, so if
        # necessary we can "send back" to them later.
        if self.assignee:
            sv.set_reviewed_by(self.assignee)

        return task

    def do_complete_approve(self, lang_ct=None):
        return self._complete_approve(lang_ct=lang_ct)

    def _complete_approve(self, lang_ct=None):
        """Handle the messy details of completing an approve task."""
        approval = self.approved == Task.APPROVED_IDS['Approved']
        sv = self.get_subtitle_version()
        if approval:
            self._ensure_language_complete(sv.subtitle_language)

        self._add_comment(lang_ct=lang_ct)

        if approval:
            # The subtitles are acceptable, so make them public!
            self.new_subtitle_version.publish()

            # Create translation tasks if necessary.
            if self.workflow.autocreate_translate:
                _create_translation_tasks(self.team_video, sv)

            task = None
            # Notify the appropriate users.
            notifier.approved_notification.delay(self.pk, approval)
        else:
            # Send the subtitles back for improvement.
            task = self._send_back()

        # Before we go, we need to record who approved these subtitles, so if
        # necessary we can "send back" to them later.
        if self.assignee:
            sv.set_approved_by(self.assignee)

        if approval:
            api_subtitles_approved.send(sv)
        else:
            api_subtitles_rejected.send(sv)

        return task

    def _ensure_language_complete(self, subtitle_language):
        if not subtitle_language.subtitles_complete:
            subtitle_language.subtitles_complete = True
            subtitle_language.save()

    def get_perform_url(self):
        """Return a URL for whatever dialog is used to perform this task."""
        return reverse('teams:perform_task', args=(self.team.slug, self.id))

    def tasks_page_perform_link_text(self):
        """Get the link text for perform link on the tasks page."""
        if self.assignee:
            return _('Resume')
        else:
            return _('Start now')

    def get_widget_url(self):
        """Get the URL to edit the video for this task.  """
        return reverse("subtitles:subtitle-editor", kwargs={
                        "video_id": self.team_video.video.video_id,
                        "language_code": self.language
                    })

    def needs_start_dialog(self):
        """Check if this task needs the start dialog.

        The only time we need it is when a user is starting a
        transcribe/translate task.  We don't need it for review/approval, or
        if the task is being resumed.
        """
        # We use the start dialog for select two things:
        #   - primary audio language
        #   - language of the subtitles
        return (self.language == '' or
                self.team_video.video.primary_audio_language_code == '')

    def get_reviewer(self):
        """For Approve tasks, return the last user to Review these subtitles.

        May be None if this task is not an Approve task, or if we can't figure
        out the last reviewer for any reason.

        """
        if self.get_type_display() == 'Approve':
            previous = Task.objects.complete().filter(
                team_video=self.team_video,
                language=self.language,
                team=self.team,
                type=Task.TYPE_IDS['Review']).order_by('-completed')[:1]

            if previous:
                return previous[0].assignee

    def set_expiration(self):
        """Set the expiration_date of this task.  Does not save().

        Requires that self.team and self.assignee be set correctly.

        """
        if not self.assignee or not self.team.task_expiration:
            self.expiration_date = None
        else:
            limit = datetime.timedelta(days=self.team.task_expiration)
            self.expiration_date = self.now() + limit

    def get_subtitle_version(self):
        """ Gets the subtitle version related to this task.
        If the task has a subtitle_version attached, return it and
        if not, try to find it throught the subtitle language of the video.

        Note: we need this since we don't attach incomplete subtitle_version
        to the task (and if we do we need to set the status to unmoderated and
        that causes the version to get published).
        """

        # autocreate sets the subtitle_version to another
        # language's subtitle_version and that was breaking
        # not only the interface but the new upload method.
        if (self.new_subtitle_version and
            self.new_subtitle_version.language_code == self.language):
            return self.new_subtitle_version

        if not hasattr(self, "_subtitle_version"):
            language = self.team_video.video.subtitle_language(self.language)
            self._subtitle_version = (language.get_tip(public=False)
                                      if language else None)
        return self._subtitle_version

    def is_blocked(self):
        """Return whether this task is "blocked".
        "Blocked" means that it's a translation task but the source language
        isn't ready to be translated yet.
        """
        subtitle_version = self.get_subtitle_version()
        if not subtitle_version:
            return False
        source_language = subtitle_version.subtitle_language.get_translation_source_language()
        if not source_language:
            return False
        can_perform = (source_language and
                       source_language.is_complete_and_synced())

        if self.get_type_display() != 'Translate':
            if self.get_type_display() in ('Review', 'Approve'):
                # review and approve tasks will be blocked if they're
                # a translation and they have a draft and the source
                # language no longer  has published version
                if not can_perform or source_language.language_code == self.language:
                    return True
        return not can_perform

    def save(self, *args, **kwargs):
        is_review_or_approve = self.get_type_display() in ('Review', 'Approve')

        if self.language:
            if not self.language in translation.ALL_LANGUAGE_CODES:
                raise ValidationError(
                    "Subtitle Language should be a valid code.")

        result = super(Task, self).save(*args, **kwargs)

        Video.cache.invalidate_by_pk(self.team_video.video_id)

        return result


# Settings
class SettingManager(models.Manager):
    use_for_related_fields = True

    def guidelines(self):
        """Return a QS of settings related to team guidelines."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.startswith('guidelines_')]
        return self.get_query_set().filter(key__in=keys)

    def messages(self):
        """Return a QS of settings related to team messages."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.startswith('messages_')]
        return self.get_query_set().filter(key__in=keys)

    def messages_guidelines(self):
        """Return a QS of settings related to team messages or guidelines."""
        return self.get_query_set().filter(key__in=Setting.MESSAGE_KEYS)

    def with_names(self, names):
        return self.filter(key__in=[Setting.KEY_IDS[name] for name in names])

    def all_messages(self):
        messages = {}
        for key in Setting.MESSAGE_KEYS:
            name = Setting.KEY_NAMES[key]
            messages[name] = self.instance.get_default_message(name)
        messages.update({
            s.key_name: s.data
            for s in self.messages_guidelines()
            if s.data
        })
        return messages

    def localized_messages(self):
        """Return a QS of settings related to team messages."""
        keys = [key for key, name in Setting.KEY_CHOICES
                if name.endswith('localized')]
        return self.get_query_set().filter(key__in=keys)

class Setting(models.Model):
    KEY_CHOICES = (
        (100, 'messages_invite'),
        (101, 'messages_manager'),
        (102, 'messages_admin'),
        (103, 'messages_application'),
        (104, 'messages_joins'),
        (105, 'messages_joins_localized'),
        (200, 'guidelines_subtitle'),
        (201, 'guidelines_translate'),
        (202, 'guidelines_review'),
        # 300s means if this team will block those notifications
        (300, 'block_invitation_sent_message'),
        (301, 'block_application_sent_message'),
        (302, 'block_application_denided_message'),
        (303, 'block_team_member_new_message'),
        (304, 'block_team_member_leave_message'),
        (305, 'block_task_assigned_message'),
        (306, 'block_reviewed_and_published_message'),
        (307, 'block_reviewed_and_pending_approval_message'),
        (308, 'block_reviewed_and_sent_back_message'),
        (309, 'block_approved_message'),
        (310, 'block_new_video_message'),
        (311, 'block_new_collab_assignments_message'),
        # 400 is for text displayed on web pages
        (401, 'pagetext_welcome_heading'),
    )
    KEY_NAMES = dict(KEY_CHOICES)
    KEY_IDS = dict([choice[::-1] for choice in KEY_CHOICES])

    MESSAGE_KEYS = [
        key for key, name in KEY_CHOICES
        if name.startswith('messages_') or name.startswith('guidelines_')
        or name.startswith('pagetext_') and not name.endswith('localized')
    ]
    MESSAGE_KEYS_LOCALIZED = [
        key for key, name in KEY_CHOICES
        if name.endswith('localized')
    ]
    MESSAGE_DEFAULTS = {
        'pagetext_welcome_heading': _("Help %(team)s reach a world audience"),
    }
    key = models.PositiveIntegerField(choices=KEY_CHOICES)
    data = models.TextField(blank=True)
    team = models.ForeignKey(Team, related_name='settings')
    language_code = models.CharField(
        max_length=16, blank=True, default='',
        choices=translation.ALL_LANGUAGE_CHOICES)
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, editable=False)

    objects = SettingManager()

    class Meta:
        unique_together = (('key', 'team', 'language_code'),)

    def __unicode__(self):
        return u'%s - %s' % (self.team, self.key_name)

    @property
    def key_name(self):
        """Return the key name for this setting.

        TODO: Remove this and replace with get_key_display()?

        """
        return Setting.KEY_NAMES[self.key]


# TeamLanguagePreferences
class TeamLanguagePreferenceManager(models.Manager):
    def _generate_writable(self, team):
        """Return the set of language codes that are writeable for this team."""

        unwritable = self.for_team(team).filter(allow_writes=False, preferred=False).values("language_code")
        unwritable = set([x['language_code'] for x in unwritable])

        return translation.ALL_LANGUAGE_CODES - unwritable

    def _generate_readable(self, team):
        """Return the set of language codes that are readable for this team."""

        unreadable = self.for_team(team).filter(allow_reads=False, preferred=False).values("language_code")
        unreadable = set([x['language_code'] for x in unreadable])

        return translation.ALL_LANGUAGE_CODES - unreadable

    def _generate_preferred(self, team):
        """Return the set of language codes that are preferred for this team."""
        preferred = self.for_team(team).filter(preferred=True).values("language_code")
        return set([x['language_code'] for x in preferred])


    def for_team(self, team):
        """Return a QS of all language preferences for the given team."""
        return self.get_query_set().filter(team=team)

    def on_changed(cls, sender,  instance, *args, **kwargs):
        """Perform any necessary actions when a language preference changes.

        TODO: Refactor this out of the manager...

        """
        from teams.cache import invalidate_lang_preferences
        invalidate_lang_preferences(instance.team)


    def get_readable(self, team):
        """Return the set of language codes that are readable for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_readable_langs
        return get_readable_langs(team)

    def get_writable(self, team):
        """Return the set of language codes that are writeable for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_writable_langs
        return get_writable_langs(team)

    def get_preferred(self, team):
        """Return the set of language codes that are preferred for this team.

        This value may come from memcache if possible.

        """
        from teams.cache import get_preferred_langs
        return get_preferred_langs(team)

class TeamLanguagePreference(models.Model):
    """Represent language preferences for a given team.

    First, TLPs may mark a language as "preferred".  If that's the case then the
    other attributes of this model are irrelevant and can be ignored.
    "Preferred" languages will have translation tasks automatically created for
    them when subtitles are added.

    If preferred is False, the TLP describes a *restriction* on the language
    instead.  Writing in that language may be prevented, or both reading and
    writing may be prevented.

    (Note: "writing" means not only writing new subtitles but also creating
    tasks, etc)

    This is how the restriction settings should interact.  TLP means that we
    have created a TeamLanguagePreference for that team and language.

    | Action                                 | NO  | allow_read=True,  | allow_read=False, |
    |                                        | TLP | allow_write=False | allow_write=False |
    ========================================================================================
    | assignable as tasks                    | X   |                   |                   |
    | assignable as narrowing                | X   |                   |                   |
    | listed on the widget for viewing       | X   | X                 |                   |
    | listed on the widget for improving     | X   |                   |                   |
    | returned from the api read operations  | X   | X                 |                   |
    | upload / write operations from the api | X   |                   |                   |
    | show up on the start dialog            | X   |                   |                   |
    +----------------------------------------+-----+-------------------+-------------------+

    Remember, this table only applies if preferred=False.  If the language is
    preferred the "restriction" attributes are effectively garbage.  Maybe we
    should make the column nullable to make this more clear?

    allow_read=True, allow_write=True, preferred=False is invalid.  Just remove
    the row all together.

    """
    team = models.ForeignKey(Team, related_name="lang_preferences")
    language_code = models.CharField(max_length=16)

    allow_reads = models.BooleanField(default=False)
    allow_writes = models.BooleanField(default=False)
    preferred = models.BooleanField(default=False)

    objects = TeamLanguagePreferenceManager()

    class Meta:
        unique_together = ('team', 'language_code')


    def clean(self, *args, **kwargs):
        if self.allow_reads and self.allow_writes:
            raise ValidationError("No sense in having all allowed, just remove the preference for this language.")

        if self.preferred and (self.allow_reads or self.allow_writes):
            raise ValidationError("Cannot restrict a preferred language.")

        super(TeamLanguagePreference, self).clean(*args, **kwargs)

    def __unicode__(self):
        return u"%s preference for team %s" % (self.language_code, self.team)


post_save.connect(TeamLanguagePreference.objects.on_changed, TeamLanguagePreference)


# TeamNotificationSettings
class TeamNotificationSettingManager(models.Manager):
    def notify_team(self, team_pk, event_name, **kwargs):
        """Notify the given team of a given event.

        Finds the matching notification settings for this team, instantiates
        the notifier class, and sends the appropriate notification.

        If the notification settings has an email target, sends an email.

        If the http settings are filled, then sends the request.

        This can be ran as a Celery task, as it requires no objects to be passed.

        """
        try:
            team = Team.objects.get(pk=team_pk)
        except Team.DoesNotExist:
            logger.error("A pk for a non-existent team was passed in.",
                         extra={"team_pk": team_pk, "event_name": event_name})
            return

        try:
            if team.partner:
                notification_settings = self.get(partner=team.partner)
            else:
                notification_settings = self.get(team=team)
        except TeamNotificationSetting.DoesNotExist:
            return

        notification_settings.notify(event_name, **kwargs)


class TeamNotificationSetting(models.Model):
    """Info on how a team should be notified of changes to its videos.

    For now, a team can be notified by having a http request sent with the
    payload as the notification information.  This cannot be hardcoded since
    teams might have different urls for each environment.

    Some teams have strict requirements on mapping video ids to their internal
    values, and also their own language codes. Therefore we need to configure
    a class that can do the correct mapping.

    TODO: allow email notifications

    """
    EVENT_VIDEO_NEW = "video-new"
    EVENT_VIDEO_EDITED = "video-edited"
    EVENT_LANGUAGE_NEW = "language-new"
    EVENT_LANGUAGE_EDITED = "language-edit"
    EVENT_LANGUAGE_DELETED = "language-deleted"
    EVENT_SUBTITLE_NEW = "subs-new"
    EVENT_SUBTITLE_APPROVED = "subs-approved"
    EVENT_SUBTITLE_REJECTED = "subs-rejected"
    EVENT_APPLICATION_NEW = 'application-new'

    team = models.OneToOneField(Team, related_name="notification_settings",
            null=True, blank=True)
    partner = models.OneToOneField('Partner',
        related_name="notification_settings",  null=True, blank=True)

    # the url to post the callback notifing partners of new video activity
    request_url = models.URLField(blank=True, null=True)
    basic_auth_username = models.CharField(max_length=255, blank=True, null=True)
    basic_auth_password = models.CharField(max_length=255, blank=True, null=True)

    # not being used, here to avoid extra migrations in the future
    email = models.EmailField(blank=True, null=True)

    # integers mapping to classes, see unisubs-integration/notificationsclasses.py
    notification_class = models.IntegerField(default=1,)

    objects = TeamNotificationSettingManager()

    NOTIFICATION_CLASS_MAP = {
        1: BaseNotification,
    }

    @classmethod
    def register_notification_class(cls, index, notification_class):
        """Register a new notification class.

        This is used to allow other apps to extend the notification system.
        """
        if index in cls.NOTIFICATION_CLASS_MAP:
            raise ValueError("%s already registered", index)
        cls.NOTIFICATION_CLASS_MAP[index] = notification_class

    def get_notification_class(self):
        return self.NOTIFICATION_CLASS_MAP.get(self.notification_class)

    def notify(self, event_name,  **kwargs):
        """Resolve the notification class for this setting and fires notfications."""
        notification_class = self.get_notification_class()

        if not notification_class:
            logger.error("Could not find notification class %s" % self.notification_class)
            return

        logger.info(
            "Sending %s %s (team: %s) (partner: %s) (data: %s)",
            notification_class.__name__, event_name, self.team, self.partner,
            kwargs,
        )

        notification = notification_class(self.team, self.partner,
                event_name,  **kwargs)

        if self.request_url:
            success, content = notification.send_http_request(
                self.request_url,
                self.basic_auth_username,
                self.basic_auth_password
            )
            return success, content
        # FIXME: spec and test this, for now just return
        return

    def __unicode__(self):
        if self.partner:
            return u'NotificationSettings for partner %s' % self.partner
        return u'NotificationSettings for team %s' % self.team


class BillingReport(models.Model):
    # use BillingRecords to signify completed work
    TYPE_BILLING_RECORD = 2
    # use approval tasks to signify completed work
    TYPE_APPROVAL = 3
    # Like TYPE_APPROVAL, but centered on the users who subtitle/review the
    # work
    TYPE_APPROVAL_FOR_USERS = 4
    TYPE_CHOICES = (
        (TYPE_BILLING_RECORD, 'Crowd sourced'),
        (TYPE_APPROVAL, 'Professional services'),
        (TYPE_APPROVAL_FOR_USERS, 'On-demand translators'),
    )
    teams = models.ManyToManyField(Team, related_name='billing_reports')
    start_date = models.DateField()
    end_date = models.DateField()
    csv_file = S3EnabledFileField(blank=True, null=True,
            upload_to='teams/billing/')
    processed = models.DateTimeField(blank=True, null=True)
    type = models.IntegerField(choices=TYPE_CHOICES,
                               default=TYPE_BILLING_RECORD)

    def __unicode__(self):
        if hasattr(self, 'id') and self.id is not None:
            team_count = self.teams.all().count()
        else:
            team_count = 0
        return "%s teams (%s - %s)" % (team_count,
                self.start_date.strftime('%Y-%m-%d'),
                self.end_date.strftime('%Y-%m-%d'))

    def _get_approved_tasks(self):
        return Task.objects.complete_approve().filter(
            approved=Task.APPROVED_IDS['Approved'],
            team__in=self.teams.all(),
            completed__range=(self.start_date, self.end_date))

    def _report_date(self, datetime):
        return datetime.strftime('%Y-%m-%d %H:%M:%S')

    def generate_rows_type_approval(self):
        header = (
            'Team',
            'Video Title',
            'Video ID',
            'Project',
            'Language',
            'Minutes',
            'Original',
            'Translation?',
            'Approver',
            'Date',
        )
        rows = [header]
        for approve_task in self._get_approved_tasks():
            video = approve_task.team_video.video
            project = approve_task.team_video.project.name if approve_task.team_video.project else 'none'
            version = approve_task.new_subtitle_version
            language = version.subtitle_language
            subtitle_task = (Task.objects.complete_subtitle_or_translate()
                             .filter(team_video=approve_task.team_video,
                                     language=approve_task.language)
                             .order_by('-completed'))[0]
            rows.append((
                approve_task.team.name,
                video.title_display(),
                video.video_id,
                project,
                approve_task.language,
                get_minutes_for_version(version, False),
                language.is_primary_audio_language(),
                subtitle_task.type==Task.TYPE_IDS['Translate'],
                unicode(approve_task.assignee),
                self._report_date(approve_task.completed),
            ))
        return rows

    def generate_rows_type_approval_for_users(self):
        header = (
            'User',
            'Task Type',
            'Team',
            'Video Title',
            'Video ID',
            'Project',
            'Language',
            'Minutes',
            'Original',
            'Approver',
            'Note',
            'Date',
            'Pay Rate',
        )
        data_rows = []
        for approve_task in self._get_approved_tasks():
            video = approve_task.team_video.video
            project = approve_task.team_video.project.name if approve_task.team_video.project else 'none'
            version = approve_task.get_subtitle_version()
            language = version.subtitle_language

            all_tasks = [approve_task]
            try:
                all_tasks.append((Task.objects.complete_subtitle_or_translate()
                                  .filter(team_video=approve_task.team_video,
                                          language=approve_task.language)
                                  .order_by('-completed'))[0])
            except IndexError:
                # no subtitling task, probably the review task was manually
                # created.
                pass
            try:
                all_tasks.append((Task.objects.complete_review()
                                  .filter(team_video=approve_task.team_video,
                                          language=approve_task.language)
                                  .order_by('-completed'))[0])
            except IndexError:
                # review not enabled
                pass

            for task in all_tasks:
                data_rows.append((
                    unicode(task.assignee),
                    task.get_type_display(),
                    approve_task.team.name,
                    video.title_display(),
                    video.video_id,
                    project,
                    language.language_code,
                    get_minutes_for_version(version, False),
                    language.is_primary_audio_language(),
                    unicode(approve_task.assignee),
                    unicode(task.body),
                    self._report_date(task.completed),
                    task.assignee.pay_rate_code,
                ))

        data_rows.sort(key=lambda row: row[0])
        return [header] + data_rows

    def generate_rows_type_billing_record(self):
        rows = []
        for i,team in enumerate(self.teams.all()):
            rows = rows + BillingRecord.objects.csv_report_for_team(team,
                self.start_date, self.end_date, add_header=i == 0)
        return rows

    def generate_rows(self):
        if self.type == BillingReport.TYPE_BILLING_RECORD:
            rows = self.generate_rows_type_billing_record()
        elif self.type == BillingReport.TYPE_APPROVAL:
            rows = self.generate_rows_type_approval()
        elif self.type == BillingReport.TYPE_APPROVAL_FOR_USERS:
            rows = self.generate_rows_type_approval_for_users()
        else:
            raise ValueError("Unknown type: %s" % self.type)
        return rows

    def convert_unicode_to_utf8(self, rows):
        def _convert(value):
            if isinstance(value, unicode):
                return value.encode("utf-8")
            else:
                return value
        return [tuple(_convert(v) for v in row) for row in rows]

    def process(self):
        """
        Generate the correct rows (including headers), saves it to a tempo file,
        then set's that file to the csv_file property, which if , using the S3
        storage will take care of exporting it to s3.
        """
        try:
            rows = self.generate_rows()
        except StandardError:
            logger.error("Error generating billing report: (id: %s)", self.id)
            self.csv_file = None
        else:
            self.csv_file = self.make_csv_file(rows)
        self.processed = datetime.datetime.utcnow()
        self.save()

    def make_csv_file(self, rows):
        rows = self.convert_unicode_to_utf8(rows)
        fn = '/tmp/bill-%s-teams-%s-%s-%s-%s.csv' % (
            self.teams.all().count(),
            self.start_str, self.end_str,
            self.get_type_display(), self.pk)
        with open(fn, 'w') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        return File(open(fn, 'r'))

    @property
    def start_str(self):
        return self.start_date.strftime("%Y%m%d")

    @property
    def end_str(self):
        return self.end_date.strftime("%Y%m%d")

class BillingReportGenerator(object):
    def __init__(self, all_records, add_header=True):
        if add_header:
            self.rows = [self.header()]
        else:
            self.rows = []
        all_records = list(all_records)
        self.make_language_number_map(all_records)
        self.make_languages_without_records(all_records)
        for video, records in groupby(all_records, lambda r: r.video):
            records = list(records)
            if video:
                for lang in self.languages_without_records.get(video.id, []):
                    self.rows.append(
                        self.make_row_for_lang_without_record(video, lang))
            for r in records:
                self.rows.append(self.make_row(video, r))

    def header(self):
        return [
            'Video Title',
            'Video ID',
            'Project',
            'Language',
            'Minutes',
            'Original',
            'Language number',
            'Team',
            'Created',
            'Source',
            'User',
        ]

    def make_row(self, video, record):
        return [
            (video and video.title_display()) or "----",
            (video and video.video_id) or "deleted",
            (record.project.name if record.project else 'none'),
            (record.new_subtitle_language and record.new_subtitle_language.language_code) or "----",
            record.minutes,
            record.is_original,
            (self.language_number_map and (record.id in self.language_number_map) and self.language_number_map[record.id]) or "----",
            record.team.slug,
            record.created.strftime('%Y-%m-%d %H:%M:%S'),
            record.source,
            record.user.username,
        ]

    def make_language_number_map(self, records):
        self.language_number_map = {}
        videos = set(r.video for r in records)
        video_counts = dict((v and v.id, 0) for v in videos)
        qs = (BillingRecord.objects
              .filter(video__in=videos)
              .order_by('created'))
        for record in qs:
            vid = record.video and record.video.id
            video_counts[vid] += 1
            self.language_number_map[record.id] = video_counts[vid]

    def make_languages_without_records(self, records):
        self.languages_without_records = {}
        videos = [r.video for r in records]
        language_ids = [r.new_subtitle_language_id for r in records]
        no_billing_record_where = """\
NOT EXISTS (
    SELECT 1
    FROM teams_billingrecord br
    WHERE br.new_subtitle_language_id = subtitles_subtitlelanguage.id
)"""
        qs = (NewSubtitleLanguage.objects
              .filter(video__in=videos, subtitles_complete=True)
              .exclude(id__in=language_ids).
              extra(where=[no_billing_record_where]))
        for lang in qs:
            vid = lang.video_id
            if vid not in self.languages_without_records:
                self.languages_without_records[vid] = [lang]
            else:
                self.languages_without_records[vid].append(lang)

    def make_row_for_lang_without_record(self, video, language):
        return [
            video.title_display(),
            video.video_id,
            'none',
            language.language_code,
            0,
            language.is_primary_audio_language(),
            0,
            'unknown',
            language.created.strftime('%Y-%m-%d %H:%M:%S'),
            'unknown',
            'unknown',
        ]

class BillingRecordManager(models.Manager):

    def data_for_team(self, team, start, end):
        return self.filter(team=team, created__gte=start, created__lte=end)

    def csv_report_for_team(self, team, start, end, add_header=True):
        all_records = self.data_for_team(team, start, end)
        generator = BillingReportGenerator(all_records, add_header)
        return generator.rows

    def insert_records_for_translations(self, billing_record):
        """
        IF you've translated from an incomplete language, and later on that
        language is completed, we must check if any translations are now
        complete and therefore should have billing records with them
        """
        translations = billing_record.new_subtitle_language.get_dependent_subtitle_languages()
        inserted = []
        for translation in translations:
            version = translation.get_tip(public=False)
            if version:
               inserted.append(self.insert_record(version))
        return filter(bool, inserted)

    def insert_record(self, version):
        """
        Figures out if this version qualifies for a billing record, and
        if so creates one. This should be self contained, e.g. safe to call
        for any version. No records should be created if not needed, and it
        won't create multiples.

        If this language has translations it will check if any of those are now
        eligible for BillingRecords and create one accordingly.
        """
        from teams.models import BillingRecord

        celery_logger.debug('insert billing record')

        language = version.subtitle_language
        video = language.video
        tv = video.get_team_video()

        if not tv:
            celery_logger.debug('not a team video')
            return

        if not language.is_complete_and_synced(public=False):
            celery_logger.debug('language not complete')
            return


        try:
            # we already have a record
            previous_record = BillingRecord.objects.get(video=video,
                            new_subtitle_language=language)
            # make sure we update it
            celery_logger.debug('a billing record for this language exists')
            previous_record.is_original = \
                video.primary_audio_language_code == language.language_code
            previous_record.save()
            return
        except BillingRecord.DoesNotExist:
            pass


        if NewSubtitleVersion.objects.filter(
                subtitle_language=language,
                created__lt=BILLING_CUTOFF).exclude(
                pk=version.pk).exists():
            celery_logger.debug('an older version exists')
            return

        is_original = language.is_primary_audio_language()
        source = version.origin
        team = tv.team
        project = tv.project
        new_record = BillingRecord.objects.create(
            video=video,
            project = project,
            new_subtitle_version=version,
            new_subtitle_language=language,
            is_original=is_original, team=team,
            created=version.created,
            source=source,
            user=version.author)
        from_translations = self.insert_records_for_translations(new_record)
        return new_record, from_translations


def get_minutes_for_version(version, round_up_to_integer):
    """
    Return the number of minutes the subtitles specified in version
    """
    subs = version.get_subtitles()

    if len(subs) == 0:
        return 0

    for sub in subs:
        if sub.start_time is not None:
            start_time = sub.start_time
            break
        # we shouldn't have an end time set without a start time, but handle
        # it just in case
        if sub.end_time is not None:
            start_time = sub.end_time
            break
    else:
        return 0

    for sub in reversed(subs):
        if sub.end_time is not None:
            end_time = sub.end_time
            break
        # we shouldn't have an end time not set, but check for that just in
        # case
        if sub.start_time is not None:
            end_time = sub.start_time
            break
    else:
        return 0

    duration_seconds =  (end_time - start_time) / 1000.0
    minutes = duration_seconds/60.0
    if round_up_to_integer:
        minutes = int(ceil(minutes))
    return minutes

class BillingRecord(models.Model):
    # The billing record should still exist if the video is deleted
    video = models.ForeignKey(Video, blank=True, null=True, on_delete=models.SET_NULL)
    project = models.ForeignKey(Project, blank=True, null=True, on_delete=models.SET_NULL)
    subtitle_version = models.ForeignKey(SubtitleVersion, null=True,
            blank=True, on_delete=models.SET_NULL)
    new_subtitle_version = models.ForeignKey(NewSubtitleVersion, null=True,
            blank=True, on_delete=models.SET_NULL)

    subtitle_language = models.ForeignKey(SubtitleLanguage, null=True,
            blank=True, on_delete=models.SET_NULL)
    new_subtitle_language = models.ForeignKey(NewSubtitleLanguage, null=True,
            blank=True, on_delete=models.SET_NULL)

    minutes = models.FloatField(blank=True, null=True)
    is_original = models.BooleanField(default=False)
    team = models.ForeignKey(Team)
    created = models.DateTimeField()
    source = models.CharField(max_length=255)
    user = models.ForeignKey(User)

    objects = BillingRecordManager()

    class Meta:
        unique_together = ('video', 'new_subtitle_language')


    def __unicode__(self):
        return "%s - %s" % (self.video and self.video.video_id,
                self.new_subtitle_language and self.new_subtitle_language.language_code)

    def save(self, *args, **kwargs):
        if not self.minutes and self.minutes != 0.0:
            self.minutes = self.get_minutes()

        assert self.minutes is not None

        return super(BillingRecord, self).save(*args, **kwargs)

    def get_minutes(self):
        return get_minutes_for_version(self.new_subtitle_version, True)

class Partner(models.Model):
    name = models.CharField(_(u'name'), max_length=250, unique=True)
    slug = models.SlugField(_(u'slug'), unique=True)
    can_request_paid_captions = models.BooleanField(default=False)

    # The `admins` field specifies users who can do just about anything within
    # the partner realm.
    admins = models.ManyToManyField('auth.CustomUser',
            related_name='managed_partners', blank=True, null=True)

    def __unicode__(self):
        return self.name

    def is_admin(self, user):
        return user in self.admins.all()
