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

from collections import namedtuple

from django import template
from datetime import timedelta
from teams.models import Team, TeamVideo, Project, TeamMember, Workflow, Task
from django.db.models import Count
from videos.models import Video
from widget import video_cache
from django.conf import settings
from django.utils.translation import ugettext_lazy as _, ngettext
from django.core.urlresolvers import reverse
from django.utils.http import urlencode, urlquote
from widget.views import base_widget_params

from templatetag_sugar.register import tag
from templatetag_sugar.parser import Name, Variable, Constant
from teams.models import Application
from utils.text import fmt

from teams.forms import TaskUploadForm
from teams.permissions import (
    can_view_settings_tab as _can_view_settings_tab,
    can_view_stats_tab as _can_view_stats_tab,
    can_view_approve_tab as _can_view_approve_tab,
    can_edit_video as _can_edit_video,
    can_rename_team as _can_rename_team,
    can_perform_task as _can_perform_task,
    can_assign_task as _can_assign_task,
    can_decline_task as _can_decline_task,
    can_delete_task as _can_delete_task,
    can_remove_video as _can_remove_video,
    can_delete_video as _can_delete_video,
    can_delete_video_in_team as _can_delete_video_in_team,
    can_approve as _can_approve,
    can_delete_language as _can_delete_language,
    can_resync as _can_resync,
)
from teams.permissions import (
    can_invite, can_add_video_somewhere,
    can_create_tasks, can_create_task_subtitle, can_create_task_translate,
    can_create_and_edit_subtitles, can_create_and_edit_translations
)

DEV_OR_STAGING = getattr(settings, 'DEV', False) or getattr(settings, 'STAGING', False)
ACTIONS_ON_PAGE = getattr(settings, 'ACTIONS_ON_PAGE', 10)

ALL_LANGUAGES_DICT = dict(settings.ALL_LANGUAGES)

register = template.Library()

import logging
logger = logging.getLogger(__name__)

@register.filter
def can_approve_application(team, user):
    return can_invite(team, user)

@register.filter
def can_invite_to_team(team, user):
    return can_invite(team, user)

@register.filter
def can_edit_video(team_video, user):
    return _can_edit_video(team_video, user)

@register.filter
def can_remove_video(tv, user):
    return _can_remove_video(tv, user)

@register.filter
def can_delete_video(tv, user):
    return _can_delete_video(tv, user)

@register.filter
def can_delete_video_in_team(user, team):
    return _can_delete_video_in_team(team, user)

@register.filter
def can_add_tasks(team, user):
    return can_create_tasks(team, user)

@register.filter
def is_team_manager(team, user):
    if not user.is_authenticated():
        return False
    return team.is_manager(user)

@register.filter
def is_team_member(team, user):
    if not user.is_authenticated():
        return False

    # We cache this here because we need to use it all over the place and
    # there's no point in making 3+ queries to the DB when one will do.
    if not hasattr(user, '_cached_teammember_status'):
        user._cached_teammember_status = {}

    if team.pk not in user._cached_teammember_status:
        user._cached_teammember_status[team.pk] = team.is_member(user)

    return user._cached_teammember_status[team.pk]

@register.filter
def user_role(team, user):
    member = TeamMember.objects.get(team=team,user=user)
    return member.role

@register.filter
def recent(date, now):
    if (now - date) <= timedelta(days=21):
        return "recent"
    return ""

@register.filter
def display_language(language_code):
    if language_code in ALL_LANGUAGES_DICT:
        return ALL_LANGUAGES_DICT[language_code]
    return language_code

@register.filter
def display_project(project, default_project_label):
    return project.display(_(default_project_label))

@register.filter
def user_tasks_count(team, user):
    tasks = Task.objects.filter(team=team,assignee=user,deleted=False,completed=None)
    return tasks.count()

@register.filter
def user_project_tasks_count(project, user):
    team = project.team
    tasks = Task.objects.filter(team=team,assignee=user,team_video__project=project,deleted=False,completed=None)
    return tasks.count()

@register.inclusion_tag('teams/_team_select.html', takes_context=True)
def team_select(context, team):
    user = context['user']
    qs = Team.objects.exclude(pk=team.pk).filter(users=user)
    return {
        'team': team,
        'objects': qs,
        'can_create_team': DEV_OR_STAGING or (user.is_superuser and user.is_active)
    }

TeamMetric = namedtuple('TeamMetric', 'url label count')

@register.inclusion_tag('teams/_metrics.html')
def team_metrics(team, member, projects):
    metrics = [
        TeamMetric(reverse('teams:videos', args=(team.slug,)),
                   ngettext('Video', 'Videos', team.videos_count),
                   team.videos_count),
        TeamMetric(reverse('teams:members', args=(team.slug,)),
                   ngettext('Member', 'Members', team.members_count),
                   team.members_count),
    ]
    if team.workflow_enabled:
        metrics.append(TeamMetric(
            reverse('teams:team_tasks', args=(team.slug,)),
            ngettext('Task', 'Tasks', team.tasks_count),
            team.get_tasks_count_display()))
    if projects:
        metrics.append(TeamMetric(
            reverse('teams:videos', args=(team.slug,)),
            ngettext('Project', 'Projects', len(projects)),
            len(projects)))

    return {
        'with_links': (team.is_visible or member),
        'metrics': metrics,
    }

@register.filter
def team_is_visible(team_slug):
    try:
        team = Team.objects.get(slug=team_slug)
    except Team.DoesNotExist:
        return False
    if team.is_visible:
        return True

@tag(register, [])
def share_panel_email_url(context):
    project = context.get('project')
    team = context.get('team')

    if not project:
        message = 'Check out the "%s" team on Amara: %s' % (team.name, team.get_site_url())
        share_panel_email_url = reverse('videos:email_friend')
        share_panel_email_url = "%s?%s" % (share_panel_email_url, urlencode({'text': message}))
    else:
        message = 'Check out the "%s" project on Amara: %s' % (project.name, project.get_site_url())
        share_panel_email_url = reverse('videos:email_friend')
        share_panel_email_url = "%s?%s" % (share_panel_email_url, urlencode({'text': message}))

    return share_panel_email_url

@register.inclusion_tag('teams/_team_move_video_select.html', takes_context=True)
def team_move_video_select(context):
    user = context['user']
    if user.is_authenticated():
        team_video = context['team_video']
        if team_video:
            qs = Team.objects.filter(users=user)
            context['teams'] = [team for team in qs
                                if can_add_video_somewhere(team, user)
                                and can_remove_video(team_video, user)
                                and team.pk != team_video.team.pk]
    return context

@register.inclusion_tag('teams/_team_video_summary.html', takes_context=True)
def team_video_summary(context, video):
    context['video'] = video
    context['team_video'] = video.get_team_video()
    context['video_url'] = video_url = video.get_video_url()
    context['team_video_widget_params'] = base_widget_params(context['request'], {
        'video_url': video_url,
        'base_state': {},
        'effectiveVideoURL': video_url
    })
    return context

@register.inclusion_tag('teams/_team_video_detail.html', takes_context=True)
def team_video_detail(context, video):
    context['video'] = video
    context['team_video'] = video.get_team_video()
    context['video_url'] = video_url = video.get_video_url()
    context['team_video_widget_params'] = base_widget_params(context['request'], {
        'video_url': video_url,
        'base_state': {},
        'effectiveVideoURL': video_url
    })
    return context

@register.inclusion_tag('teams/_team_video_lang_detail.html', takes_context=True)
def team_video_lang_detail(context, lang, team):
    context['team_video'] = team.teamvideo_set.select_related('video').get(video__id=lang.video_id)
    context['lang'] = lang
    return context

@register.inclusion_tag('teams/_invite_friends_to_team.html', takes_context=True)
def invite_friends_to_team(context, team):
    context['invite_message'] = fmt(_(u'Can somebody help me subtitle '
                                      'these videos? %(url)s'),
                                    url=team.get_site_url())
    return context

@register.inclusion_tag('teams/_task_language_list.html', takes_context=True)
def languages_to_translate(context, task):
    context['allowed_languages'] = video_cache.get_video_completed_languages(task.team_video_id)

    return context

@register.inclusion_tag('teams/_team_video_lang_list.html', takes_context=True)
def team_video_lang_list(context, model_or_search_record, max_items=6):
    """
    max_items: if there are more items than max_items, they will be truncated to X more.
    """

    if isinstance(model_or_search_record, TeamVideo):
        video_url = reverse("teams:team_video", kwargs={"team_video_pk":model_or_search_record.pk})
    elif isinstance(model_or_search_record, Video):
        video_url =  reverse("videos:video", kwargs={"video_id":model_or_search_record.video_id})
    else:
        video_url =  reverse("teams:team_video", kwargs={"team_video_pk":model_or_search_record.team_video_pk})
    return  {
        'sub_statuses': video_cache.get_video_languages_verbose(model_or_search_record.video_id, max_items),
        "video_url": video_url ,
        }

@register.inclusion_tag('teams/_team_video_in_progress_list.html')
def team_video_in_progress_list(team_video_search_record):
    langs_raw = video_cache.writelocked_langs(team_video_search_record.video_id)

    langs = [_(ALL_LANGUAGES_DICT[x]) for x in langs_raw]
    return  {
        'languages': langs
        }

@tag(register, [Variable(), Constant("as"), Name()])
def team_projects(context, team, varname):
    """
    Sets the project list on the context, but only the non default
    hidden projects.
    Usage:
    {%  team_projects team as projects %}
        {% for project in projects %}
            project
        {% endfor %}
    If you do want to loop through all project:

    {% for p in team.project_set.all %}
      {% if p.is_default_project %}
         blah
      {% else %}
    {%endif %}
    {% endfor %}

    """
    context[varname] = Project.objects.for_team(team)
    return ""

@tag(register, [Variable(), Constant("as"), Name()])
def member_projects(context, member, varname):
    narrowings = member.narrowings.filter(project__isnull=False)
    context[varname] = [n.project for n in narrowings]
    return ""


@register.filter
def can_view_settings_tab(team, user):
   return _can_view_settings_tab(team, user)

@register.filter
def can_view_stats_tab(team, user):
   return _can_view_stats_tab(team, user)

@register.filter
def can_view_approve_tab(team, user):
   return _can_view_approve_tab(team, user)

@register.filter
def can_rename_team(team, user):
    return _can_rename_team(team, user)

@register.filter
def can_resync(team, user):
    return _can_resync(team, user)

@register.filter
def can_apply(team, user):
    return Application.objects.can_apply(team=team, user=user)

@register.filter
def has_applicant(team, user):
    return team.applications.filter(user=user).exists()

def _team_members(team, role, countOnly):
    qs = team.members.filter(role=role)
    if countOnly:
        qs = qs.count()
    return qs

@register.filter
def contributors(team, countOnly=False):
    return _team_members(team, TeamMember.ROLE_CONTRIBUTOR, countOnly)

@register.filter
def managers(team, countOnly=False):
    return _team_members(team, TeamMember.ROLE_MANAGER, countOnly)


@register.filter
def admins(team, countOnly=False):
    return _team_members(team, TeamMember.ROLE_ADMIN, countOnly)


@register.filter
def owners(team, countOnly=False):
    return _team_members(team, TeamMember.ROLE_OWNER, countOnly)

@register.filter
def owners_and_admins(team, countOnly=False):
    qs = team.members.filter(role__in=[TeamMember.ROLE_ADMIN, TeamMember.ROLE_OWNER])
    if countOnly:
        qs = qs.count()
    return qs

@register.filter
def members(team, countOnly=False):
    qs = team.members.all()
    if countOnly:
        qs = qs.count()
    return qs

@register.filter
def can_leave_team(team, user):
    """Return True if the user can leave the team, else return False."""

    try:
        member = TeamMember.objects.get(team=team, user=user)
    except TeamMember.DoesNotExist:
        return False

    if not team.members.exclude(pk=member.pk).exists():
        False

    is_last_owner = (
        member.role == TeamMember.ROLE_OWNER
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exclude(pk=member.pk).exists()
    )
    if is_last_owner:
        return False

    is_last_admin = (
        member.role == TeamMember.ROLE_ADMIN
        and not team.members.filter(role=TeamMember.ROLE_ADMIN).exclude(pk=member.pk).exists()
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exists()
    )
    if is_last_admin:
        return False

    return True

@tag(register, [Variable(), Variable()])
def can_create_any_task_for_teamvideo(context, team_video, user):
    workflows = context.get('team_workflows')

    if can_create_task_subtitle(team_video, user, workflows):
        result = True
    elif can_create_task_translate(team_video, user, workflows):
        result = True
    else:
        result = False

    context['user_can_create_any_task'] = result

    return ''


@register.filter
def review_enabled(team):
    w = Workflow.get_for_target(team.id, 'team')

    if w.review_enabled:
        return True

    for p in team.project_set.all():
        if p.workflow_enabled:
            w = Workflow.get_for_project(p)
            if w.review_enabled:
                return True

    return False


@register.filter
def approve_enabled(team):
    w = Workflow.get_for_target(team.id, 'team')

    if w.approve_enabled:
        return True

    for p in team.project_set.all():
        if p.workflow_enabled:
            w = Workflow.get_for_project(p)
            if w.approve_enabled:
                return True

    return False

@register.filter
def can_perform_task(task, user):
    return _can_perform_task(user, task)

@register.filter
def can_assign_task(task, user):
    return _can_assign_task(task, user)

@register.filter
def can_decline_task(task, user):
    return _can_decline_task(task, user)

@register.filter
def can_delete_task(task, user):
    return _can_delete_task(task, user)


@register.filter
def can_create_subtitles_for(user, video):
    """Return True if the user can create original subtitles for this video.

    Safe to use with anonymous users as well as non-team videos.

    Usage:

        {% if request.user|can_create_subtitles_for:video %}
            ...
        {% endif %}

    """
    team_video = video.get_team_video()

    if not team_video:
        return True
    else:
        return can_create_and_edit_subtitles(user, team_video)
@register.filter
def can_create_translations_for(user, video):
    """Return True if the user can create translations for this video.

    Safe to use with anonymous users as well as non-team videos.

    Usage:

        {% if request.user|can_create_translations_for:video %}
            ...
        {% endif %}

    """
    team_video = video.get_team_video()

    if not team_video:
        return True
    else:
        return can_create_and_edit_translations(user, team_video)

@register.filter
def can_delete_language(user, language):
    team_video = language.video.get_team_video()
    return (team_video is not None and
            _can_delete_language(team_video.team, user))

@register.filter
def get_upload_form(task, user):
    """Return a TaskUploadForm for the given Task and User.

        {% with task|get_upload_form:request.user as form %}
            ...
        {% endif %}

    """
    return TaskUploadForm(user=user, video=task.team_video.video)

@register.filter
def extra_pages(team, user):
    return team.new_workflow.extra_pages(user)

@register.filter
def extra_settings_pages(team, user):
    return team.new_workflow.extra_settings_pages(user)
