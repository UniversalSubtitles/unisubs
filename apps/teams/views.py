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
import functools
import json
import logging
import random
import pickle

import babelsubs

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.db.models import Q, Count
from django.http import (
    Http404, HttpResponseForbidden, HttpResponseRedirect, HttpResponse,
    HttpResponseBadRequest, HttpResponseServerError
)
from django.shortcuts import (get_object_or_404, redirect, render_to_response,
                              render)
from django.template import RequestContext
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import iri_to_uri, force_unicode
from django.core.cache import cache

import widget
from auth.models import UserLanguage, CustomUser as User
from videos.templatetags.paginator import paginate
from messages import tasks as notifier
from teams.forms import (
    CreateTeamForm, AddTeamVideoForm, EditTeamVideoForm,
    AddTeamVideosFromFeedForm, TaskAssignForm, SettingsForm, TaskCreateForm,
    PermissionsForm, WorkflowForm, InviteForm, TaskDeleteForm,
    GuidelinesMessagesForm, RenameableSettingsForm, ProjectForm, LanguagesForm,
    DeleteLanguageForm, MoveTeamVideoForm, TaskUploadForm,
    make_billing_report_form, TaskCreateSubtitlesForm,
    TeamMultiVideoCreateSubtitlesForm, MoveVideosForm, AddVideoToTeamForm,
    GuidelinesLangMessagesForm,
)
from teams.models import (
    Team, TeamMember, Invite, Application, TeamVideo, Task, Project, Workflow,
    Setting, TeamLanguagePreference, InviteExpiredException, BillingReport,
    ApplicationInvalidException
)
from teams.permissions import (
    can_add_video, can_assign_role, can_assign_tasks, can_create_task_subtitle,
    can_create_task_translate, can_view_tasks_tab, can_invite,
    roles_user_can_assign, can_join_team, can_edit_video, can_delete_tasks,
    can_perform_task, can_rename_team, can_change_team_settings,
    can_perform_task_for, can_delete_team, can_delete_video, can_remove_video,
    can_delete_language, can_move_videos, can_view_stats_tab, can_sort_by_primary_language
)
from teams.signals import api_teamvideo_new
from teams.tasks import (
    invalidate_video_caches, invalidate_video_moderation_caches,
    update_video_moderation, update_video_public_field,
    invalidate_video_visibility_caches, process_billing_report
)
from videos.tasks import video_changed_tasks
from utils import render_to, render_to_json, DEFAULT_PROTOCOL
from utils.forms import flatten_errorlists
from utils.objectlist import object_list
from utils.panslugify import pan_slugify
from utils.searching import get_terms
from utils.text import fmt
from utils.translation import (
    get_language_choices, get_language_choices_as_dicts, languages_with_labels, get_user_languages_from_request
)
from utils.chunkediter import chunkediter
from videos.types import UPDATE_VERSION_ACTION
from videos import metadata_manager
from videos.models import Action, VideoUrl, Video, VideoFeed
from subtitles.models import SubtitleLanguage, SubtitleVersion
from widget.rpc import add_general_settings
from widget.views import base_widget_params
from teams import workflows
from statistics import compute_statistics

from teams.bulk_actions import complete_approve_tasks

logger = logging.getLogger("teams.views")

TASKS_ON_PAGE = getattr(settings, 'TASKS_ON_PAGE', 20)
TEAMS_ON_PAGE = getattr(settings, 'TEAMS_ON_PAGE', 10)
MAX_MEMBER_SEARCH_RESULTS = 40
HIGHTLIGHTED_TEAMS_ON_PAGE = getattr(settings, 'HIGHTLIGHTED_TEAMS_ON_PAGE', 10)
CUTTOFF_DUPLICATES_NUM_VIDEOS_ON_TEAMS = getattr(settings, 'CUTTOFF_DUPLICATES_NUM_VIDEOS_ON_TEAMS', 20)

VIDEOS_ON_PAGE = getattr(settings, 'VIDEOS_ON_PAGE', 16)
MEMBERS_ON_PAGE = getattr(settings, 'MEMBERS_ON_PAGE', 15)
APLICATIONS_ON_PAGE = getattr(settings, 'APLICATIONS_ON_PAGE', 15)
UNASSIGNED_TASKS_ON_PAGE = getattr(settings, 'UNASSIGNED_TASKS_ON_PAGE', 15)
ACTIONS_ON_PAGE = getattr(settings, 'ACTIONS_ON_PAGE', 20)
DEV = getattr(settings, 'DEV', False)
DEV_OR_STAGING = DEV or getattr(settings, 'STAGING', False)
ALL_LANGUAGES_DICT = dict(settings.ALL_LANGUAGES)
BILLING_CUTOFF = getattr(settings, 'BILLING_CUTOFF', None)

def get_team_for_view(slug, user, exclude_private=True):
    if isinstance(slug, Team):
        # hack to handle the new view code calling this page.  In that
        # case it passes the team directly rather than the slug
        return slug
    try:
        return Team.objects.for_user(user, exclude_private).get(slug=slug)
    except Team.DoesNotExist:
        raise Http404

def settings_page(view_func):
    """Decorator for the team settings pages."""

    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        team = get_team_for_view(slug, request.user)
        if not can_change_team_settings(team, request.user):
            messages.error(request, _(u'You do not have permission to edit this team.'))
            return HttpResponseRedirect(team.get_absolute_url())
        return view_func(request, team, *args, **kwargs)
    return login_required(wrapper)

# Management
def index(request, my_teams=False):
    q = request.REQUEST.get('q')

    if my_teams and request.user.is_authenticated():
        ordering = 'name'
        qs = Team.objects.filter(members__user=request.user)
    else:
        ordering = request.GET.get('o', 'members')
        qs = (Team.objects.for_user(request.user)
              .add_videos_count().add_members_count()
              .add_user_is_member(request.user))

    if q:
        qs = qs.filter(Q(name__icontains=q)|Q(description__icontains=q))

    order_fields = {
        'name': 'name',
        'date': 'created',
        'members': '_members_count'
    }
    order_fields_name = {
        'name': _(u'Name'),
        'date': _(u'Newest'),
        'members': _(u'Most Members')
    }
    order_fields_type = {
        'name': 'asc',
        'date': 'desc',
        'members': 'desc'
    }
    order_type = request.GET.get('ot', order_fields_type.get(ordering, 'desc'))

    if ordering in order_fields and order_type in ['asc', 'desc']:
        qs = qs.order_by(('-' if order_type == 'desc' else '')+order_fields[ordering])

    extra_context = {
        'my_teams': my_teams,
        'query': q,
        'ordering': ordering,
        'order_type': order_type,
        'order_name': order_fields_name.get(ordering, 'name'),
    }
    return object_list(request, queryset=qs,
                       paginate_by=TEAMS_ON_PAGE,
                       template_name='teams/teams-list.html',
                       template_object_name='teams',
                       extra_context=extra_context)

@render_to('teams/create.html')
@staff_member_required
def create(request):
    user = request.user

    if not DEV and not (user.is_superuser and user.is_active):
        raise Http404

    if request.method == 'POST':
        form = CreateTeamForm(request.user, request.POST, request.FILES)
        if form.is_valid():
            team = form.save(user)
            messages.success(request, fmt(_(
                """Your team has been created. Here are some next steps:
                <ul>
                    <li><a href="%(edit)s">Edit team members' permissions</a></li>
                    <li><a href="%(activate)s">Activate and customize workflows for your team</a></li>
                    <li><a href="%(create)s">Create and customize projects</a></li>
                    <li><a href="%(lang)s">Edit language preferences</a></li>
                    <li><a href="%(custom)s">Customize instructions to caption makers and translators</a></li>
                </ul>"""),
                edit=reverse("teams:settings_workflows", kwargs={"slug": team.slug}),
                activate=reverse("teams:settings_workflows", kwargs={"slug": team.slug}),
                create=reverse("teams:settings_projects", kwargs={"slug": team.slug}),
                lang=reverse("teams:settings_languages", kwargs={"slug": team.slug}),
                custom=reverse("teams:settings_messages", kwargs={"slug": team.slug}),
            ))
            return redirect(reverse("teams:settings_basic", kwargs={"slug":team.slug}))
    else:
        form = CreateTeamForm(request.user)

    return { 'form': form }

# Settings
def _delete_team(request, team):
    if not can_delete_team(team, request.user):
        messages.error(request, _(u'You do not have permission to delete this team.'))
        return None

    team.deleted = True
    team.save()

    return HttpResponseRedirect(reverse('teams:index'))

@render_to('teams/settings.html')
@settings_page
def settings_basic(request, team):
    if request.POST.get('delete'):
        r = _delete_team(request, team)
        if r:
            return r

    if can_rename_team(team, request.user):
        FormClass = RenameableSettingsForm
    else:
        FormClass = SettingsForm

    if request.POST:
        form = FormClass(request.POST, request.FILES, instance=team)

        is_visible = team.is_visible

        if form.is_valid():
            try:
                form.save()
            except:
                logger.exception("Error on changing team settings")
                raise

            if is_visible != form.instance.is_visible:
                update_video_public_field.delay(team.id)
                invalidate_video_visibility_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = FormClass(instance=team)

    return { 'team': team, 'form': form, }

@render_to('teams/settings-messages.html')
@settings_page
def settings_messages(request, team):
    initial = team.settings.all_messages()
    if request.POST:
        form = GuidelinesMessagesForm(request.POST, initial=initial)

        if form.is_valid():
            for key, val in form.cleaned_data.items():
                setting, c = Setting.objects.get_or_create(team=team, key=Setting.KEY_IDS[key])
                setting.data = val
                setting.save()

            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = GuidelinesMessagesForm(initial=initial)

    return { 'team': team, 'form': form, }

@render_to('teams/settings-lang-messages.html')
@settings_page
def settings_lang_messages(request, team):
    initial = team.settings.all_messages()
    languages = [{"code": l.language_code, "data": l.data} for l in team.settings.localized_messages()]
    if request.POST:
        form = GuidelinesLangMessagesForm(request.POST, languages=languages)
        if form.is_valid():
            new_language = None
            new_message = None
            for key, val in form.cleaned_data.items():
                if key == "messages_joins_localized":
                    new_message = val
                elif key == "messages_joins_language":
                    new_language = val
                else:
                    l = key.split("messages_joins_localized_")
                    if len(l) == 2:
                        code = l[1]
                        try:
                            setting = Setting.objects.get(team=team, key=Setting.KEY_IDS["messages_joins_localized"], language_code=code)
                            if val == "":
                                setting.delete()
                            else:
                                setting.data = val
                                setting.save()
                        except:
                            messages.error(request, _(u'No message for that language.'))
                            return HttpResponseRedirect(request.path)
            if new_message and new_language:
                setting, c = Setting.objects.get_or_create(team=team,
                                  key=Setting.KEY_IDS["messages_joins_localized"],
                                  language_code=new_language)
                if c:
                    setting.data = new_message
                    setting.save()
                else:
                    messages.error(request, _(u'There is already a message for that language.'))
                    return HttpResponseRedirect(request.path)
            elif new_message or new_language:
                messages.error(request, _(u'Please set the language and the message.'))
                return HttpResponseRedirect(request.path)
            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = GuidelinesLangMessagesForm(languages=languages)

    return { 'team': team, 'form': form, }

@render_to('teams/settings-workflows.html')
def old_team_settings_workflows(request, team):
    workflow = Workflow.get_for_target(team.id, 'team')
    moderated = team.moderates_videos()

    if request.POST:
        form = PermissionsForm(request.POST, instance=team)
        workflow_form = WorkflowForm(request.POST, instance=workflow)

        if form.is_valid() and workflow_form.is_valid():
            form.save()

            if form.cleaned_data['workflow_enabled']:
                workflow_form.save()

            moderation_changed = moderated != form.instance.moderates_videos()

            if moderation_changed:
                update_video_moderation.delay(team)
                invalidate_video_moderation_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = PermissionsForm(instance=team)
        workflow_form = WorkflowForm(instance=workflow)

    return { 'team': team, 'form': form, 'workflow_form': workflow_form, }

@render_to('teams/settings-projects.html')
@settings_page
def settings_projects(request, team):
    projects = team.project_set.exclude(name=Project.DEFAULT_NAME)

    return { 'team': team, 'projects': projects, }

def _set_languages(team, codes_preferred, codes_blacklisted):
    tlps = TeamLanguagePreference.objects.for_team(team)

    existing = set(tlp.language_code for tlp in tlps)

    desired_preferred = set(codes_preferred)
    desired_blacklisted = set(codes_blacklisted)
    desired = desired_preferred | desired_blacklisted

    # Figure out which languages need to be deleted/created/changed.
    to_delete = existing - desired

    to_create_preferred = desired_preferred - existing
    to_set_preferred = desired_preferred & existing

    to_create_blacklisted = desired_blacklisted - existing
    to_set_blacklisted = desired_blacklisted & existing

    # Delete unneeded prefs.
    for tlp in tlps.filter(language_code__in=to_delete):
        tlp.delete()

    # Change existing prefs.
    for tlp in tlps.filter(language_code__in=to_set_preferred):
        tlp.preferred, tlp.allow_reads, tlp.allow_writes = True, False, False
        tlp.save()

    for tlp in tlps.filter(language_code__in=to_set_blacklisted):
        tlp.preferred, tlp.allow_reads, tlp.allow_writes = False, False, False
        tlp.save()

    # Create remaining prefs.
    for lang in to_create_preferred:
        tlp = TeamLanguagePreference(team=team, language_code=lang,
                                     allow_reads=False, allow_writes=False,
                                     preferred=True)
        tlp.save()

    for lang in to_create_blacklisted:
        tlp = TeamLanguagePreference(team=team, language_code=lang,
                                     allow_reads=False, allow_writes=False,
                                     preferred=False)
        tlp.save()

@render_to('teams/settings-languages.html')
@login_required
def settings_languages(request, slug):
    team = get_team_for_view(slug, request.user)

    if not can_change_team_settings(team, request.user):
        messages.error(request, _(u'You do not have permission to edit this team.'))
        return HttpResponseRedirect(team.get_absolute_url())

    preferred = [tlp.language_code for tlp in
                 TeamLanguagePreference.objects.for_team(team).filter(preferred=True)]
    blacklisted = [tlp.language_code for tlp in
                   TeamLanguagePreference.objects.for_team(team).filter(preferred=False)]
    initial = {'preferred': preferred, 'blacklisted': blacklisted}

    if request.POST:
        form = LanguagesForm(team, request.POST, initial=initial)

        if form.is_valid():
            _set_languages(team, form.cleaned_data['preferred'], form.cleaned_data['blacklisted'])

            messages.success(request, _(u'Settings saved.'))
            invalidate_video_caches.delay(team.pk)
            return HttpResponseRedirect(request.path)
    else:
        form = LanguagesForm(team, initial=initial)

    return { 'team': team, 'form': form }


def _default_project_for_team(team):
    """Get the default project to filter by for the videos/tasks lists
    """
    if team.slug == 'ted':
        # :( Logic for the TED team is hardcoded here
        try:
            return Project.objects.get(team=team, slug='tedtalks')
        except Project.DoesNotExist:
            logging.warning("_default_project_for_team: "
                    "tedtalks project does not exist")
            return None
    else:
        return None


def _get_videos_for_detail_page(team, user, query, project, language_code,
                                language_mode, sort):
    if not team.is_member(user) and not team.is_visible:
        return Videos.objects.none()

    qs = Video.objects.filter(teamvideo__team=team)
    # add a couple of completed values that we use in the template code
    qs = qs.add_num_completed_languages()
    num_tasks_sql = ("""
                     (SELECT COUNT(*) FROM teams_task WHERE
                     completed IS NULL AND deleted=0 AND
                     team_video_id=teams_teamvideo.id)""")
    qs = qs.extra(select={'num_tasks': num_tasks_sql})

    if query:
        qs = qs.search(query)
    if project:
        qs = qs.filter(teamvideo__project=project)
    if language_mode == '+':
        if language_code is not None:
            qs = qs.has_completed_language(language_code)
    elif language_mode == '-':
        if language_code is not None:
            qs = qs.missing_completed_language(language_mode)
        else:
            qs = qs.no_completed_languages()

    qs = qs.order_by({
         'name':  'title',
        '-name': '-title',
         'subs':  'num_completed_languages',
        '-subs': '-num_completed_languages',
         'time':  'created',
        '-time': '-created',
    }.get(sort or '-time'))

    qs = qs.select_related('teamvideo')

    return qs

# Videos
@render_to('teams/videos-list.html')
def detail(request, slug, project_slug=None, languages=None):
    team = get_team_for_view(slug, request.user)

    try:
        member = team.get_member(request.user)
    except TeamMember.DoesNotExist:
        member = None

    project_filter = (project_slug if project_slug is not None
                      else request.GET.get('project'))
    if project_filter:
        if project_filter == 'any':
            project = None
        else:
            try:
                project = Project.objects.get(team=team, slug=project_filter)
            except Project.DoesNotExist:
                project = None
    else:
        project = _default_project_for_team(team)

    query = request.GET.get('q', '')
    sort = request.GET.get('sort')
    language_filter = request.GET.get('lang')
    language_code = language_filter if language_filter != 'any' else None
    language_mode = request.GET.get('lang-mode', '+')
    filtered = bool(set(request.GET.keys()).intersection([
        'project', 'lang', 'sort']))

    qs = _get_videos_for_detail_page(team, request.user, query, project,
                                     language_code, language_mode, sort)

    extra_context = {
        'team': team,
        'member': member,
        'project':project,
        'project_filter': project_filter,
        'language_filter': language_filter,
        'language_code': language_code,
        'language_mode': language_mode,
        'sort': sort,
        'can_add_video': can_add_video(team, request.user, project),
        'can_move_videos': can_move_videos(team, request.user),
        'can_edit_videos': can_add_video(team, request.user, project),
        'filtered': filtered,
    }

    if extra_context['can_add_video'] or extra_context['can_edit_videos']:
        # Cheat and reduce the number of videos on the page if we're dealing
        # with someone who can edit videos in the team, for performance
        # reasons.
        is_editor = True
        per_page = 8
    else:
        is_editor = False
        per_page = VIDEOS_ON_PAGE

    general_settings = {}
    add_general_settings(request, general_settings)
    extra_context['general_settings'] = json.dumps(general_settings)

    if team.video:
        extra_context['widget_params'] = base_widget_params(request, {
            'video_url': team.video.get_video_url(),
            'base_state': {}
        })

    readable_langs = TeamLanguagePreference.objects.get_readable(team)
    language_choices = [(code, name) for code, name in get_language_choices()
                        if code in readable_langs]

    extra_context['project_choices'] = team.project_set.exclude(name='_root')

    extra_context['language_choices'] = language_choices
    extra_context['query'] = query

    sort_names = {
        'name': 'Name, A-Z',
        '-name': 'Name, Z-A',
        'time': 'Time, Oldest',
        '-time': 'Time, Newest',
        'subs': 'Subtitles, Least',
        '-subs': 'Subtitles, Most',
    }
    if sort:
        extra_context['order_name'] = sort_names[sort]
    else:
        extra_context['order_name'] = sort_names['-time']

    extra_context['current_videos_count'] = qs.count()

    team_video_md_list, pagination_info = paginate(qs, per_page, request.GET.get('page'))
    extra_context.update(pagination_info)
    extra_context['team_video_md_list'] = team_video_md_list
    extra_context['team_workflows'] = list(
        Workflow.objects.filter(team=team.id)
                        .select_related('project', 'team', 'team_video'))

    if not filtered and not query:
        if project:
            is_indexing = project.videos_count != extra_context['current_videos_count']
        else:
            is_indexing = team.videos.all().count() != extra_context['current_videos_count']
        extra_context['is_indexing'] = is_indexing

    return extra_context

@render_to('teams/move_videos.html')
def move_videos(request, slug, project_slug=None, languages=None):
    team = get_team_for_view(slug, request.user)
    if not can_move_videos(team, request.user):
        return HttpResponseForbidden("Not allowed")

    try:
        member = team.get_member(request.user)
    except TeamMember.DoesNotExist:
        member = None

    managed_teams = request.user.managed_teams(include_manager=False)
    managed_projects = Project.objects.filter(team__in=managed_teams)
    managed_projects_choices = map(lambda project: {'id': project.id, 'team': str(project.team.id), 'name': str(project)}, managed_projects)

    if request.method == 'POST':
        form = MoveVideosForm(request.user, request.POST)
        if 'move' in request.POST and form.is_valid():
            target_team = form.cleaned_data['team']
            if target_team not in managed_teams:
                return  HttpResponseForbidden("Not allowed")
    
            project_id = request.POST.get('projects',None)
            target_project = None
            if project_id:
                try:
                    target_project = Project.objects.get(id=project_id)
                    if target_project not in managed_projects:
                        return  HttpResponseForbidden("Not allowed")
                except Entry.DoesNotExist:
                    return  HttpResponseBadRequest("Illegal Request")
                except MultipleObjectsReturned:
                    return  HttpResponseServerError("Internal Error")
            selected_videos = request.POST.getlist('selected_videos[]')
            for video_id in selected_videos:
                try:
                    team_video = TeamVideo.objects.get(id=video_id)
                    if team_video.team not in managed_teams:
                        return  HttpResponseForbidden("Not allowed")
                    team_video.move_to(target_team, project=target_project)
                except Entry.DoesNotExist:
                    return  HttpResponseBadRequest("Illegal Request")
                except MultipleObjectsReturned:
                    return  HttpResponseServerError("Internal Error")
    else:
        form = MoveVideosForm(request.user)
     
    project_filter = (project_slug if project_slug is not None
                      else request.GET.get('project'))
    if project_filter:
        if project_filter == 'any':
            project = None
        else:
            try:
                project = Project.objects.get(team=team, slug=project_filter)
            except Project.DoesNotExist:
                project = None
    else:
        project = _default_project_for_team(team)

    query = request.GET.get('q', '')
    sort = request.GET.get('sort')
    language_filter = request.GET.get('lang')
    primary_audio_language_filter = request.GET.get('primary-audio-lang', 'any' if can_sort_by_primary_language(team, request.user) else None)
    language_code = language_filter if language_filter != 'any' else None
    primary_audio_language_code = primary_audio_language_filter if primary_audio_language_filter != 'any' else None
    language_mode = request.GET.get('lang-mode', '+')
    filtered = bool(set(request.GET.keys()).intersection([
        'project', 'lang', 'sort']))

    qs = _get_videos_for_detail_page(team, request.user, query, project,
                                     language_code, language_mode,
                                     sort)

    if primary_audio_language_code is not None:
        qs = qs.filter(
            primary_audio_language_code=primary_audio_language_code)

    extra_context = {
        'team': team,
        'member': member,
        'project':project,
        'project_filter': project_filter,
        'language_filter': language_filter,
        'language_code': language_code,
        'language_mode': language_mode,
        'sort': sort,
        'primary_audio_language_filter': primary_audio_language_filter,
        'can_add_video': can_add_video(team, request.user, project),
        'can_move_videos': can_move_videos(team, request.user),
        'can_edit_videos': can_add_video(team, request.user, project),
        'filtered': filtered,
        'form': form,
        'projects': managed_projects_choices
    }

    if extra_context['can_add_video'] or extra_context['can_edit_videos']:
        # Cheat and reduce the number of videos on the page if we're dealing
        # with someone who can edit videos in the team, for performance
        # reasons.
        is_editor = True
        per_page = 8
    else:
        is_editor = False
        per_page = VIDEOS_ON_PAGE

    general_settings = {}
    add_general_settings(request, general_settings)
    extra_context['general_settings'] = json.dumps(general_settings)

    if team.video:
        extra_context['widget_params'] = base_widget_params(request, {
            'video_url': team.video.get_video_url(),
            'base_state': {}
        })

    readable_langs = TeamLanguagePreference.objects.get_readable(team)
    language_choices = [(code, name) for code, name in get_language_choices()
                        if code in readable_langs]

    extra_context['project_choices'] = team.project_set.exclude(name='_root')

    extra_context['language_choices'] = language_choices
    extra_context['query'] = query

    sort_names = {
        'name': 'Name, A-Z',
        '-name': 'Name, Z-A',
        'time': 'Time, Oldest',
        '-time': 'Time, Newest',
        'subs': 'Subtitles, Least',
        '-subs': 'Subtitles, Most',
    }
    if sort:
        extra_context['order_name'] = sort_names[sort]
    else:
        extra_context['order_name'] = sort_names['-time']

    extra_context['current_videos_count'] = len(qs)

    team_video_md_list, pagination_info = paginate(qs, per_page, request.GET.get('page'))
    extra_context.update(pagination_info)
    extra_context['team_video_md_list'] = team_video_md_list
    extra_context['team_workflows'] = list(
        Workflow.objects.filter(team=team.id)
                        .select_related('project', 'team', 'team_video'))

    if not filtered and not query:
        if project:
            is_indexing = project.videos_count != extra_context['current_videos_count']
        else:
            is_indexing = team.videos.all().count() != extra_context['current_videos_count']
        extra_context['is_indexing'] = is_indexing

    return extra_context

@login_required
def add_video_to_team(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    if request.method == 'POST':
        form = AddVideoToTeamForm(request.user, request.POST)
        if form.is_valid():
            team = Team.objects.get(id=form.cleaned_data['team'])
            team_video = TeamVideo.objects.create(video=video, team=team)
            return redirect(video.get_absolute_url())
    else:
        form = AddVideoToTeamForm(request.user)
    return render(request, 'teams/add-video-to-team.html', {
        'video': video,
        'form': form,
    })

@render_to('teams/add_video.html')
@login_required
def add_video(request, slug):
    team = get_team_for_view(slug, request.user)

    project_id = request.GET.get('project') or request.POST.get('project') or None
    project = Project.objects.get(team=team, pk=project_id) if project_id else team.default_project

    if request.POST and not can_add_video(team, request.user, project):
        messages.error(request, _(u"You can't add that video to this team/project."))
        return HttpResponseRedirect(team.get_absolute_url())

    initial = {
        'video_url': request.GET.get('url', ''),
        'title': request.GET.get('title', '')
    }

    if project:
        initial['project'] = project

    form = AddTeamVideoForm(team, request.user, request.POST or None, request.FILES or None, initial=initial)

    if form.is_valid():
        obj = form.save(False)
        obj.added_by = request.user
        obj.save()

        api_teamvideo_new.send(obj)
        video_changed_tasks.delay(obj.video.pk)
        messages.success(request, form.success_message())
        return redirect(team.get_absolute_url())

    return {
        'form': form,
        'team': team
    }

@login_required
def move_video(request):
    form = MoveTeamVideoForm(request.user, request.POST)

    if form.is_valid():
        team_video = form.cleaned_data['team_video']
        team = form.cleaned_data['team']
        project = form.cleaned_data['project']
        team_video.move_to(team, project)
        messages.success(request, _(u'The video has been moved to the new team.'))
    else:
        for e in flatten_errorlists(form.errors):
            messages.error(request, e)

    return HttpResponseRedirect(request.POST.get('next', '/'))

@render_to('teams/add_videos.html')
@login_required
def add_videos(request, slug):
    team = get_team_for_view(slug, request.user)

    if not can_add_video(team, request.user):
        messages.error(request, _(u"You can't add videos to this team/project."))
        return HttpResponseRedirect(team.get_absolute_url())

    form = AddTeamVideosFromFeedForm(team, request.user, request.POST or None)

    if form.is_valid():
        form.save()
        messages.success(request, form.success_message())
        return redirect(reverse('teams:settings_feeds', kwargs={
            'slug': team.slug,
        }))

    return { 'form': form, 'team': team, }

@login_required
@render_to('teams/team_video.html')
def team_video(request, team_video_pk):
    team_video = get_object_or_404(TeamVideo, pk=team_video_pk)

    if not can_edit_video(team_video, request.user):
        messages.error(request, _(u'You can\'t edit this video.'))
        return HttpResponseRedirect(team_video.team.get_absolute_url())

    meta = team_video.video.metadata()
    form = EditTeamVideoForm(request.POST or None, request.FILES or None,
                             instance=team_video, user=request.user, initial=meta)

    if form.is_valid():
        form.save()
        messages.success(request, _('Video has been updated.'))
        return redirect(team_video)

    return {
        'team': team_video.team,
        'team_video': team_video,
        'form': form,
        'user': request.user,
        'widget_params': base_widget_params(request, {'video_url': team_video.video.get_video_url(), 'base_state': {}})
    }

@render_to_json
@login_required
def remove_video(request, team_video_pk):
    def _error_resp(request, next, error):
        if request.is_ajax():
            return { 'success': False, 'error': error }
        else:
            messages.error(request, error)
            return HttpResponseRedirect(next)

    team_video = get_object_or_404(TeamVideo, pk=team_video_pk)

    if request.method != 'POST':
        return _error_resp(request, reverse('teams:user_teams'),
                           _(u'Request must be a POST request.'))

    next = request.POST.get('next', reverse('teams:user_teams'))
    wants_delete = request.POST.get('del-opt') == 'total-destruction'

    if wants_delete:
        if not can_delete_video(team_video, request.user):
            return _error_resp(request, next,
                               _(u"You can't delete that video."))
    else:
        if not can_remove_video(team_video, request.user):
            return _error_resp(request, next,
                               _(u"You can't remove that video."))

    for task in team_video.task_set.all():
        task.delete()

    video = team_video.video

    if wants_delete:
        # create the action handler before deleting the video, so that
        # it can grab the video's title
        Action.delete_video_handler(video, team_video.team, request.user)
        video.delete()
        msg = _(u'Video has been deleted from Amara.')
    else:
        team_video.delete()
        msg = _(u'Video has been removed from the team.')

    if request.is_ajax():
        return { 'success': True }
    else:
        messages.success(request, msg)
        return HttpResponseRedirect(next)


class TableCell():
    """Convenience class to pass
    table data to template, namely
    cell contents and whether they are
    headers.
    """
    def __init__(self, content, header=False):
        self.content = content
        self.header = header
    def __repr__(self):
        return str(self.content)

# Members
@render_to('teams/members-list.html')
def detail_members(request, slug, role=None):
    q = request.REQUEST.get('q')
    lang = request.GET.get('lang')
    sort = request.GET.get('sort', 'joined')
    filtered = False

    team = get_team_for_view(slug, request.user)

    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    qs = team.members.select_related('user').filter(user__is_active=True)

    if q:
        filtered = True
        for term in filter(None, [term.strip() for term in q.split()]):
            qs = qs.filter(Q(user__first_name__icontains=term)
                         | Q(user__last_name__icontains=term)
                         | Q(user__email__icontains=term)
                         | Q(user__username__icontains=term)
                         | Q(user__biography__icontains=term))

    if lang:
        filtered = True
        qs = qs.filter(user__userlanguage__language=lang)

    if role:
        filtered = True
        if role == 'admin':
            qs = qs.filter(role__in=[TeamMember.ROLE_OWNER, TeamMember.ROLE_ADMIN])
        else:
            qs = qs.filter(role=role)

    if sort == 'joined':
        qs = qs.order_by('created')
    elif sort == '-joined':
        qs = qs.order_by('-created')

    extra_context = {
        'filtered': filtered,
    }

    team_member_list, pagination_info = paginate(qs, MEMBERS_ON_PAGE, request.GET.get('page'))
    extra_context.update(pagination_info)
    extra_context['team_member_list'] = team_member_list

    # if we are a member that can also edit roles, we create a dict of
    # roles that we can assign, this will vary from user to user, since
    # let's say an admin can change roles, but not for anyone above him
    # the owner, for example
    assignable_roles = []
    if roles_user_can_assign(team, request.user):
        for member in team_member_list:
            if can_assign_role(team, request.user, member.role, member.user):
                assignable_roles.append(member)

    users = team.members.values_list('user', flat=True)
    user_langs = set(UserLanguage.objects.filter(user__in=users).values_list('language', flat=True))

    extra_context.update({
        'team': team,
        'member': member,
        'query': q,
        'role': role,
        'assignable_roles': assignable_roles,
        'languages': sorted(languages_with_labels(user_langs).items(), key=lambda pair: pair[1]),
    })

    if team.video:
        extra_context['widget_params'] = base_widget_params(request, {
            'video_url': team.video.get_video_url(),
            'base_state': {}
        })

    return extra_context

@login_required
def remove_member(request, slug, user_pk):
    team = get_team_for_view(slug, request.user)

    member = get_object_or_404(TeamMember, team=team, user__pk=user_pk)

    return_path = reverse('teams:members', args=[], kwargs={'slug': team.slug})

    if can_assign_role(team, request.user, member.role, member.user):
        user = member.user
        if not user == request.user:
            [application.on_member_removed(author=request.user, interface='web UI') for application in \
             team.applications.filter(user=user, status=Application.STATUS_APPROVED)]
            TeamMember.objects.filter(team=team, user=user).delete()
            messages.success(request, _(u'Member has been removed from the team.'))
            return HttpResponseRedirect(return_path)
        else:
            messages.error(request, _(u'Use the "Leave this team" button to remove yourself from this team.'))
            return HttpResponseRedirect(return_path)
    else:
        messages.error(request, _(u'You don\'t have permission to remove this member from the team.'))
        return HttpResponseRedirect(return_path)

@login_required
def approvals(request, slug):
    team = get_team_for_view(slug, request.user)

    if not team.is_member(request.user):
        return  HttpResponseForbidden("Not allowed")

    if not team.can_bulk_approve(request.user):
        return  HttpResponseForbidden("Not allowed")

    qs = team.unassigned_tasks(sort='modified')

    # Use prefetch_related to fetch the video for each task.  This dramically
    # reduces the number of queries in order to print out the video title.
    # prefetch_related() is better than select_related() in this case because
    # if multiple tasks are for the same Video object, prefetch_related() will
    # only create 1 object while select_related() will create 1 per task.
    # Re-using the same object means better caching.
    qs = qs.filter(new_subtitle_version__subtitle_language__subtitles_complete=True)
    qs = qs.prefetch_related('team_video__video', 'team_video__project')
    extra_context = {
        'team': team,
        'now':datetime.now()
    }

    if request.method == 'POST':
        if 'approve' in request.POST:
            approvals = request.POST.getlist('approvals[]')
            # Retrieving tasks and updating them is now done in bulk,
            # this should be much more efficient.
            # Not sure about the best place to add that code
            tasks = team.get_tasks(approvals)
            try:
                tasks.update(assignee=request.user,
                             approved=Task.APPROVED_IDS['Approved'],
                             completed=datetime.now())
                complete_approve_tasks(tasks)
            except:
                HttpResponseForbidden(_(u'Invalid task to approve'))

    extra_context['language_choices'] =  [(code, name) for code, name in get_language_choices()]
    extra_context['project_choices'] = team.project_set.exclude(name=Project.DEFAULT_NAME)

    language_filter = request.GET.get('lang')
    language_code = language_filter if language_filter != 'any' else None
    if language_code:
        qs = qs.filter(language=language_code)

    project_filter = request.GET.get('project')
    project = None
    if project_filter:
        if project_filter != 'any':
            try:
                project = Project.objects.get(team=team, slug=project_filter)
            except:
                pass
    if project:
        qs = qs.filter(team_video__project=project)
    
    extra_context['project'] = project
    extra_context['language_code'] = language_code
    extra_context['language_filter'] = language_filter
    return object_list(request, queryset=qs,
                       paginate_by=UNASSIGNED_TASKS_ON_PAGE,
                       template_name='teams/approvals.html',
                       template_object_name='approvals',
                       extra_context=extra_context)


@login_required
def applications(request, slug):
    team = get_team_for_view(slug, request.user)

    if not team.is_member(request.user):
        return  HttpResponseForbidden("Not allowed")

    # default to showing only applications that need to be acted upon
    status = int(request.GET.get('status', Application.STATUS_PENDING))
    qs = team.applications.filter(status=status)

    extra_context = {
        'team': team
    }
    if request.method == 'POST':
        if 'approve' in request.POST or 'deny' in request.POST:
            if not team.is_member(request.user):
                raise Http404
            applications = request.POST.getlist('applications[]')
            approve = ('approve' in request.POST)
            if not can_invite(team, request.user):
                if approve:
                    messages.error(request, _(u'You can\'t approve applications.'))
                else:
                    messages.error(request, _(u'You can\'t deny applications.'))
            else:
                for application_pk in applications:
                    try:
                        approve_deny_application(request.user, team, application_pk, approve=approve)
                    except Application.DoesNotExist:
                        messages.error(request, _(u'Application does not exist.'))
                        break
                    except ApplicationInvalidException:
                        messages.error(request, _(u'Application already processed.'))
                        break
                if approve:
                    messages.success(request, _(u'Applications approved.'))
                else:
                    messages.success(request, _(u'Applications denied.'))

    return object_list(request, queryset=qs,
                       paginate_by=APLICATIONS_ON_PAGE,
                       template_name='teams/applications.html',
                       template_object_name='applications',
                       extra_context=extra_context)

def approve_deny_application(user, team, application_pk, approve=True):
    """
    :raises Http404: If user is not a team member
    :raises Application.DoesNotExist: If application does not exist
    :raises ApplicationInvalidException: If application was already processed
    returns True if user can approve, False otherwise
    """
    application = team.applications.get(pk=application_pk)
    if approve:
        application.approve(user, "web UI")
    else:
        application.deny(user, "web UI")

@login_required
def approve_application(request, slug, application_pk):
    team = get_team_for_view(slug, request.user)
    if not team.is_member(request.user):
        raise Http404
    if not can_invite(team, request.user):
        messages.error(request, _(u'You can\'t approve applications.'))
    try:
        approve_deny_application(request.user, team, application_pk, approve=True)
        messages.success(request, _(u'Application approved.'))
    except Application.DoesNotExist:
        messages.error(request, _(u'Application does not exist.'))
    except ApplicationInvalidException:
        messages.error(request, _(u'Application already processed.'))
    return redirect('teams:applications', team.slug)

@login_required
def deny_application(request, slug, application_pk):
    team = get_team_for_view(slug, request.user)
    if not team.is_member(request.user):
        raise Http404
    if not can_invite(team, request.user):
        messages.error(request, _(u'You can\'t deny applications.'))
    try:
        approve_deny_application(request.user, team, application_pk, approve=False)
        messages.success(request, _(u'Application denied.'))
    except Application.DoesNotExist:
        messages.error(request, _(u'Application does not exist.'))
    except ApplicationInvalidException:
        messages.error(request, _(u'Application already processed.'))
    return redirect('teams:applications', team.slug)

@login_required
def accept_invite(request, invite_pk, accept=True):
    invite = get_object_or_404(Invite, pk=invite_pk, user=request.user)
    try:
        if accept:
            invite.accept()
            return redirect(reverse("teams:dashboard", kwargs={"slug": invite.team.slug}))
        else:
            invite.deny()
            return redirect(request.META.get('HTTP_REFERER', '/'))
    except InviteExpiredException:
        return HttpResponseServerError(render_to_response("generic-error.html", {
            "error_msg": _("This invite is no longer valid"),
        }, RequestContext(request)))

def _check_can_leave(team, user):
    """Return an error message if the member cannot leave the team, otherwise None."""

    try:
        member = TeamMember.objects.get(team=team, user=user)
    except TeamMember.DoesNotExist:
        return u'You are not a member of this team.'

    if not team.members.exclude(pk=member.pk).exists():
        return u'You are the last member of this team.'

    is_last_owner = (
        member.role == TeamMember.ROLE_OWNER
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exclude(pk=member.pk).exists()
    )
    if is_last_owner:
        return u'You are the last owner of this team.'

    is_last_admin = (
        member.role == TeamMember.ROLE_ADMIN
        and not team.members.filter(role=TeamMember.ROLE_ADMIN).exclude(pk=member.pk).exists()
        and not team.members.filter(role=TeamMember.ROLE_OWNER).exists()
    )
    if is_last_admin:
        return u'You are the last admin of this team.'

    return None

@login_required
def leave_team(request, slug):
    team = get_object_or_404(Team, slug=slug)
    user = request.user

    error = _check_can_leave(team, user)
    if error:
        messages.error(request, _(error))
    else:
        member = TeamMember.objects.get(team=team, user=user)
        tm_user_pk = member.user.pk
        team_pk = member.team.pk
        member.delete()
        [application.on_member_leave(request.user, "web UI") for application in \
         member.team.applications.filter(status=Application.STATUS_APPROVED)]

        notifier.team_member_leave(team_pk, tm_user_pk)

        messages.success(request, _(u'You have left this team.'))
    return redirect(request.META.get('HTTP_REFERER') or team)

@permission_required('teams.change_team')
def highlight(request, slug, highlight=True):
    item = get_object_or_404(Team, slug=slug)
    item.highlight = highlight
    item.save()
    return redirect(request.META.get('HTTP_REFERER', '/'))

def _member_search_result(member, team, task_id, team_video_id, task_type, task_lang):
    result = [member.user.id, u'%s (%s)' % (member.user, member.user.username)]

    if task_id:
        task = Task.objects.not_deleted().get(team=team, pk=task_id)
        if member.has_max_tasks():
            result += [False]
        else:
            result += [can_perform_task(member.user, task)]
    elif team_video_id:
        team_video = TeamVideo.objects.get(pk=team_video_id)
        if member.has_max_tasks():
            result += [False]
        else:
            result += [can_perform_task_for(member.user, task_type, team_video, task_lang)]
    else:
        result += [None]

    return result

@render_to_json
def search_members(request, slug):
    team = get_team_for_view(slug, request.user)
    q = request.GET.get('term', '').replace('(', '').replace(')', '')
    terms = get_terms(q)

    task_id = request.GET.get('task')
    task_type = request.GET.get('task_type')
    task_lang = request.GET.get('task_lang')
    team_video_id = request.GET.get('team_video')

    members = team.members.filter(user__is_active=True)
    for term in terms:
        members = members.filter(
            Q(user__username__icontains=term) |
            Q(user__first_name__icontains=term) |
            Q(user__last_name__icontains=term)
        )
    members = members.select_related('user')[:MAX_MEMBER_SEARCH_RESULTS]

    results = [_member_search_result(m, team, task_id, team_video_id, task_type, task_lang)
               for m in members]

    return { 'results': results }

def role_saved(request, slug):
    messages.success(request, _(u'Member saved.'))
    return_path = reverse('teams:members', args=[], kwargs={'slug': slug})
    return HttpResponseRedirect(return_path)


# Tasks
def _get_or_create_workflow(team_slug, project_id, team_video_id):
    try:
        workflow = Workflow.objects.get(team__slug=team_slug, project=project_id,
                                        team_video=team_video_id)
    except Workflow.DoesNotExist:
        # We special case this because Django won't let us create new models
        # with the IDs, we need to actually pass in the Model objects for
        # the ForeignKey fields.
        #
        # Most of the time we won't need to do these three extra queries.

        team = Team.objects.get(slug=team_slug)
        project = Project.objects.get(pk=project_id) if project_id else None
        team_video = TeamVideo.objects.get(pk=team_video_id) if team_video_id else None

        workflow = Workflow(team=team, project=project, team_video=team_video)

    return workflow

def _tasks_list(request, team, project, filters, user):
    '''List tasks for the given team, optionally filtered.

    `filters` should be an object/dict with zero or more of the following keys:

    * type: a string describing the type of task. 'Subtitle', 'Translate', etc.
    * completed: true or false
    * assignee: user ID as an integer
    * team_video: team video ID as an integer

    '''
    tasks = Task.objects.filter(team=team.id, deleted=False)

    if project:
        tasks = tasks.filter(team_video__project = project)

    if filters.get('team_video'):
        tasks = tasks.filter(team_video=filters['team_video'])

    if filters.get('completed'):
        tasks = tasks.filter(completed__isnull=False)
    else:
        tasks = tasks.filter(completed=None)

    if filters.get('language'):
        if filters['language'] != 'all':
            tasks = tasks.filter(language=filters['language'])
    elif request.user.is_authenticated() and request.user.get_languages():
        languages = request.user.get_languages() + ['']
        tasks = tasks.filter(language__in=languages)

    if filters.get('q'):
        terms = get_terms(filters['q'])
        for term in terms:
            tasks = tasks.filter(
                Q(team_video__video__title__icontains=term) |
                Q(team_video__video__meta_1_content__icontains=term) |
                Q(team_video__video__meta_2_content__icontains=term) |
                Q(team_video__video__meta_3_content__icontains=term))

    if filters.get('type'):
        tasks = tasks.filter(type=Task.TYPE_IDS[filters['type']])

    if filters.get('assignee'):
        assignee = filters.get('assignee')

        if assignee == 'me':
            tasks = tasks.filter(assignee=user)
        elif assignee == 'none':
            tasks = tasks.filter(assignee=None)
        elif assignee and assignee.isdigit():
            tasks = tasks.filter(assignee=int(assignee))
        elif assignee and assignee != 'anyone':
            tasks = tasks.filter(assignee=User.objects.get(username=assignee))
    else:
        tasks = tasks.filter(assignee=None)

    return tasks

def _order_tasks(request, tasks):
    sort = request.GET.get('sort', '-created')
    # Most teams won't use priorities. For those who do, that should be
    # the default sorting.
    order_clause = ["-priority"]
    if sort == 'created':
        order_clause.append('created')
    elif sort == '-created':
        order_clause.append('-created')
    elif sort == 'expires':
        tasks = tasks.exclude(expiration_date=None)
        order_clause.append('expiration_date')
    elif sort == '-expires':
        tasks = tasks.exclude(expiration_date=None)
        order_clause.append('-expiration_date')
    tasks = tasks.order_by(*order_clause)
    return tasks

def _get_task_filters(request):
    return { 'language': request.GET.get('lang'),
             'type': request.GET.get('type'),
             'team_video': request.GET.get('team_video'),
             'assignee': request.GET.get('assignee'),
             'q': request.GET.get('q'), }

@render_to('teams/dashboard.html')
def old_dashboard(request, team):
    user = request.user if request.user.is_authenticated() else None
    try:
        member = team.members.get(user=user)
    except TeamMember.DoesNotExist:
        member = None

    if member:
        create_subtitles_form = TeamMultiVideoCreateSubtitlesForm(
            request, team, data=request.POST or None)
        if create_subtitles_form.is_valid():
            return create_subtitles_form.handle_post()
    else:
        create_subtitles_form = None

    if user:
        user_languages = set([ul for ul in user.get_languages()])
        user_filter = {'assignee':str(user.id), 'language': 'all'}
        user_tasks = _tasks_list(request, team, None, user_filter, user).order_by('expiration_date')[0:14]
        user_tasks = user_tasks.select_related('team_video')
        Task.add_cached_video_urls(user_tasks)
    else:
        user_languages = None
        user_tasks = None

    filters = {'assignee': 'none'}

    videos = []

    if member and team.workflow_enabled:

        # TED's dashboard should only show TEDTalks tasks
        # http://i.imgur.com/fjjqx.gif
        if team.slug == 'ted':
            project = Project.objects.get(team=team, slug='tedtalks')
        else:
            project = None

        if not user_languages:
            user_languages = get_user_languages_from_request(request)
            filters['language'] = user_languages[0]

        tasks = _order_tasks(request,
                             _tasks_list(request, team,
                                         project, filters,
                                         user))

        tasks = tasks.select_related('team_video', 'team_video__team',
                                     'team_video__project', 'team_video__video')

        for task in chunkediter(tasks, 100):
            if not can_perform_task(user, task):
                continue

            task_vid = task.team_video

            if not task_vid in videos:
                task_vid.tasks = []
                videos.append(task_vid)

            vid_index = videos.index(task_vid)
            videos[vid_index].tasks.append(task)

            if len(videos) >= VIDEOS_ON_PAGE:
                break

        for video in videos:
            Task.add_cached_video_urls(video.tasks)
    elif team.is_visible:
        team_videos = team.videos.select_related("teamvideo").order_by("-teamvideo__created")
        # TED's dashboard should only show TEDTalks videos
        # http://i.imgur.com/fjjqx.gif
        if team.slug == 'ted':
            project = Project.objects.get(team=team, slug='tedtalks')
            team_videos = team_videos.filter(teamvideo__project=project)

        team_videos = team_videos[0:VIDEOS_ON_PAGE]

        if not user_languages:
            for tv in team_videos:
                videos.append(tv.teamvideo)
        else:
            lang_list = user_languages

            for video in team_videos.all():
                subtitled_languages = (video.newsubtitlelanguage_set
                                                 .filter(language_code__in=lang_list)
                                                 .filter(subtitles_complete=True)
                                                 .values_list("language_code", flat=True))
                if len(subtitled_languages) != len(user_languages):
                    tv = video.teamvideo
                    tv.languages = [l for l in user_languages if l not in subtitled_languages]
                    videos.append(tv)
    else:
        videos = []

    context = {
        'team': team,
        'member': member,
        'user_tasks': user_tasks,
        'videos': videos,
        'can_add_video': can_add_video(team, request.user),
        'create_subtitles_form': create_subtitles_form,
    }

    return context

@render_to('teams/tasks.html')
def team_tasks(request, slug, project_slug=None):
    team = get_team_for_view(slug, request.user)

    if not can_view_tasks_tab(team, request.user):
        messages.error(request, _("You cannot view this team's tasks."))
        return HttpResponseRedirect(team.get_absolute_url())

    if not project_slug:
        project_slug = request.GET.get('project')

    user = request.user if request.user.is_authenticated() else None
    member = team.members.get(user=user) if user else None
    languages = get_language_choices_as_dicts()
    filters = _get_task_filters(request)
    filtered = 0

    if member:
        create_subtitles_form = TeamMultiVideoCreateSubtitlesForm(
            request, team, data=request.POST or None)
        if create_subtitles_form.is_valid():
            return create_subtitles_form.handle_post()
    else:
        create_subtitles_form = None

    if project_slug != '' and project_slug != None:
        if project_slug == 'any':
            project = None
        else:
            try:
                project = Project.objects.get(team=team, slug=project_slug)
            except Project.DoesNotExist:
                project = None
    else:
        # User didn't specify a project to filter on.  We use the default
        # project only if:
        #   - There was no team_video specified
        #   - The user isn't looking at their own tasks
        if (filters.get('team_video') is None and
                filters.get('assignee') != 'me'):
            project = _default_project_for_team(team)
        else:
            project = None

    tasks = _order_tasks(request,
                         _tasks_list(request, team, project, filters, user))
    tasks, pagination_info = paginate(tasks, TASKS_ON_PAGE, request.GET.get('page'))

    # We pull out the task IDs here for performance.  It's ugly, I know.
    #
    # MySQL doesn't use the ideal indexes when you try to filter and
    # select_related all the various stuff, but if you split the process into
    # two queries they'll both be fast.
    #
    # Thanks, MySQL.
    task_ids = list(tasks.values_list('id', flat=True))
    tasks = list(Task.objects.filter(id__in=task_ids).select_related(
            'team_video__video',
            'team_video__team',
            'team_video__project',
            'assignee',
            'team',
            'new_subtitle_version__subtitle_language',
            'new_subtitle_version__author'))
    tasks.sort(key=lambda t: task_ids.index(t.pk))

    if filters.get('team_video'):
        filters['team_video'] = TeamVideo.objects.get(pk=filters['team_video'])

    if filters.get('assignee'):
        if filters['assignee'] == 'me':
            filters['assignee'] = team.members.get(user=request.user)
        elif filters['assignee'] == 'none':
            filters['assignee'] == None
        elif filters['assignee'].isdigit():
            filters['assignee'] = team.members.get(user=filters['assignee'])
        elif filters['assignee'] != 'anyone':
            filters['assignee'] = team.members.get(user=User.objects.get(username=filters['assignee']))

        filtered = filtered + 1

    if filters.get('language'):
        filtered = filtered + 1

    if filters.get('type'):
        filtered = filtered + 1

    if project_slug is not None:
        filtered = filtered + 1

    widget_settings = {}
    from widget.rpc import add_general_settings
    add_general_settings(request, widget_settings)

    Task.add_cached_video_urls(tasks)

    context = {
        'team': team,
        'project': project, # TODO: Review
        'user_can_delete_tasks': can_delete_tasks(team, request.user),
        'user_can_assign_tasks': can_assign_tasks(team, request.user),
        'assign_form': TaskAssignForm(team, member),
        'languages': languages,
        'tasks': tasks,
        'filters': filters,
        'widget_settings': widget_settings,
        'filtered': filtered,
        'member': member,
        'project_choices': team.project_set.exclude(name='_root'),
        'create_subtitles_form': create_subtitles_form,
    }

    context.update(pagination_info)

    return context

@render_to('teams/create_task.html')
def create_task(request, slug, team_video_pk):
    team = get_object_or_404(Team, slug=slug)
    team_video = get_object_or_404(TeamVideo, pk=team_video_pk, team=team)
    can_assign = can_assign_tasks(team, request.user, team_video.project)

    if request.POST:
        form = TaskCreateForm(request.user, team, team_video, request.POST)

        if form.is_valid():
            task = form.save(commit=False)

            task.team = team
            task.team_video = team_video

            task.set_expiration()

            if task.type == Task.TYPE_IDS['Subtitle']:
                # For subtitle tasks, let the person who performse the task
                # choose the language.
                task.language = ''

            if task.type in [Task.TYPE_IDS['Review'], Task.TYPE_IDS['Approve']]:
                task.approved = Task.APPROVED_IDS['In Progress']
                task.new_subtitle_version = task.team_video.video.latest_version(language_code=task.language)

            task.save()
            notifier.team_task_assigned.delay(task.pk)
            return HttpResponseRedirect(reverse('teams:team_tasks', args=[],
                                                kwargs={'slug': team.slug}))
    else:
        form = TaskCreateForm(request.user, team, team_video)

    subtitlable = json.dumps(can_create_task_subtitle(team_video, request.user))
    translatable_languages = json.dumps(can_create_task_translate(team_video, request.user))

    language_choices = json.dumps(get_language_choices(True))

    return { 'form': form, 'team': team, 'team_video': team_video,
             'translatable_languages': translatable_languages,
             'language_choices': language_choices,
             'subtitlable': subtitlable,
             'can_assign': can_assign, }

@login_required
def perform_task(request, slug=None, task_pk=None):
    task_pk = task_pk or request.POST.get('task_id')

    task = get_object_or_404(Task, pk=task_pk)

    if slug:
        team = get_object_or_404(Team,slug=slug)
        if task.team != team:
            return HttpResponseForbidden(_(u'You are not allowed to perform this task.'))

    if not can_perform_task(request.user, task):
        return HttpResponseForbidden(_(u'You are not allowed to perform this task.'))

    if task.assignee_id != request.user.id:
        task.assignee = request.user
        task.set_expiration()
        task.save()

    if not task.needs_start_dialog():
        # ... perform task ...
        return HttpResponseRedirect(task.get_widget_url())
    else:
        # need to set the language first
        return start_subtitle_task(request, team, task)

@render_to('teams/start-subtitle-task.html')
def start_subtitle_task(request, team, task):
    form = TaskCreateSubtitlesForm(request, task, request.POST or None)
    if form.is_valid():
        return form.handle_post()
    return {
        'team': team,
        'form': form,
    }

def _delete_subtitle_version(version):
    sl = version.subtitle_language
    n = version.version_number

    # "Delete" this specific version...
    version.visibility_override = 'deleted'
    version.save()

    # We also want to "delete" all draft subs leading up to this version.
    previous_versions = (sl.subtitleversion_set.extant()
                                               .filter(version_number__lt=n)
                                               .order_by('-version_number'))
    for v in previous_versions:
        if v.is_public():
            break
        v.visibility_override = 'deleted'
        v.save()


def delete_task(request, slug):
    '''Mark a task as deleted.

    The task will not be physically deleted from the database, but will be
    flagged and won't appear in further task listings.

    '''
    team = get_object_or_404(Team, slug=slug)
    next = request.POST.get('next', reverse('teams:team_tasks', args=[],
                                            kwargs={'slug': slug}))

    form = TaskDeleteForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        video = task.team_video.video
        task.deleted = True

        if task.new_subtitle_version:
            if form.cleaned_data['discard_subs']:
                _delete_subtitle_version(task.new_subtitle_version)
                task.subtitle_version = None
                task.new_subtitle_version = None

            if task.get_type_display() in ['Review', 'Approve']:
                # TODO: Handle subtitle/translate tasks here too?
                if not form.cleaned_data['discard_subs'] and task.new_subtitle_version:
                    task.new_subtitle_version.visibility_override = 'public'
                    task.new_subtitle_version.save()
                    metadata_manager.update_metadata(video.pk)

        task.save()

        messages.success(request, _('Task deleted.'))
    else:
        messages.error(request, _('You cannot delete this task.'))

    return HttpResponseRedirect(next)

def assign_task(request, slug):
    '''Assign a task to the given user, or unassign it if null/None.'''
    team = get_object_or_404(Team, slug=slug)
    next = request.POST.get('next', reverse('teams:team_tasks', args=[], kwargs={'slug': slug}))

    form = TaskAssignForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        assignee = form.cleaned_data['assignee']

        if task.assignee == request.user:
            was_mine = True
        else:
            was_mine = False

        task.assignee = assignee
        task.set_expiration()
        task.save()
        notifier.team_task_assigned.delay(task.pk)

        if task.assignee is None and was_mine:
            messages.success(request, _('Task declined.'))
        else:
            messages.success(request, _('Task assigned.'))
    else:
        messages.error(request, _('You cannot assign this task.'))

    return HttpResponseRedirect(next)

@render_to_json
@login_required
def assign_task_ajax(request, slug):
    '''Assign a task to the given user, or unassign it if null/None.'''
    team = get_object_or_404(Team, slug=slug)

    form = TaskAssignForm(team, request.user, data=request.POST)
    if form.is_valid():
        task = form.cleaned_data['task']
        assignee = form.cleaned_data['assignee']

        if not assignee:
            return HttpResponseForbidden(u'Invalid assignment attempt - assignee is empty (%s).' % assignee)

        if task.assignee == assignee:
            return { 'success': True }

        task.assignee = assignee
        task.set_expiration()

        task.save()
        notifier.team_task_assigned.delay(task.pk)

        return { 'success': True }
    else:
        return HttpResponseForbidden(u'Invalid assignment attempt.')

@login_required
def upload_draft(request, slug, video_id):
    if request.POST:
        video = Video.objects.get(video_id=video_id)
        form = TaskUploadForm(request.POST, request.FILES,
                              user=request.user, video=video,
                              initial={'primary_audio_language_code':video.primary_audio_language_code}
        )

        if form.is_valid():
            form.save()
            messages.success(request, _(u"Draft uploaded successfully."))
        else:
            for key, value in form.errors.items():
                messages.error(request, '\n'.join([force_unicode(i) for i in value]))

        return HttpResponseRedirect(reverse('teams:team_tasks', args=[],
                                            kwargs={'slug': slug}))
    else:
        return HttpResponseBadRequest()

# copied a lot of those from widget/views.py:download_subtitles
# we need to make them share some code. for sure.
def download_draft(request, slug, task_pk, type="srt"):
    task = Task.objects.get(pk=task_pk)
    team = get_object_or_404(Team,slug=slug)

    if task.team != team:
        return HttpResponseForbidden(_(u'You are not allowed to download this transcript.'))

    if type not in babelsubs.get_available_formats():
        raise Http404

    subtitle_version = task.get_subtitle_version()

    subtitles = babelsubs.to(subtitle_version.get_subtitles(), type)
    response = HttpResponse(unicode(subtitles), mimetype="text/plain")
    original_filename = '%s.%s' % (subtitle_version.video.lang_filename(task.language), type)

    if not 'HTTP_USER_AGENT' in request.META or u'WebKit' in request.META['HTTP_USER_AGENT']:
        # Safari 3.0 and Chrome 2.0 accepts UTF-8 encoded string directly.
        filename_header = 'filename=%s' % original_filename.encode('utf-8')
    elif u'MSIE' in request.META['HTTP_USER_AGENT']:
        try:
            original_filename.encode('ascii')
        except UnicodeEncodeError:
            original_filename = 'subtitles.%s' % type

        filename_header = 'filename=%s' % original_filename
    else:
        # For others like Firefox, we follow RFC2231 (encoding extension in HTTP headers).
        filename_header = 'filename*=UTF-8\'\'%s' % iri_to_uri(original_filename.encode('utf-8'))

    response['Content-Disposition'] = 'attachment; ' + filename_header

    return response


# Projects
def project_list(request, slug):
    team = get_object_or_404(Team, slug=slug)
    projects = Project.objects.for_team(team)
    return render_to_response("teams/project_list.html", {
        "team":team,
        "projects": projects
    }, RequestContext(request))

@render_to('teams/settings-projects-add.html')
@login_required
def add_project(request, slug):
    team = get_team_for_view(slug, request.user)

    if request.POST:
        form = ProjectForm(team, request.POST)
        workflow_form = WorkflowForm(request.POST)

        if form.is_valid() and workflow_form.is_valid():

            project = form.save()
            if project.workflow_enabled:
                workflow = workflow_form.save(commit=False)
                workflow.team = team
                workflow.project = project
                workflow.save()

            messages.success(request, _(u'Project added.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,)))
    else:
        form = ProjectForm(team)
        workflow_form = WorkflowForm()

    return { 'team': team, 'form': form, 'workflow_form': workflow_form, }

@render_to('teams/settings-projects-edit.html')
@login_required
def edit_project(request, slug, project_slug):
    team = get_team_for_view(slug, request.user)
    project = Project.objects.get(slug=project_slug, team=team)
    project_list_url = reverse('teams:settings_projects', args=[], kwargs={'slug': team.slug})

    if project.is_default_project:
        messages.error(request, _(u'You cannot edit that project.'))
        return HttpResponseRedirect(project_list_url)

    try:
        workflow = Workflow.objects.get(team=team, project=project)
    except Workflow.DoesNotExist:
        workflow = None

    if request.POST:
        if request.POST.get('delete', None) == 'Delete':
            project.delete()
            messages.success(request, _(u'Project deleted.'))
            return HttpResponseRedirect(project_list_url)
        else:
            form = ProjectForm(team, request.POST, instance=project)
            workflow_form = WorkflowForm(request.POST, instance=workflow)

            # if the project doesn't have workflow enabled, the workflow form
            # is going to fail to validate (workflow is None)
            # there's probably a better way of doing this...
            if form.is_valid() and workflow_form.is_valid if project.workflow_enabled else form.is_valid():
                form.save()

                if project.workflow_enabled:
                    workflow = workflow_form.save(commit=False)
                    workflow.team = team
                    workflow.project = project
                    workflow.save()

                messages.success(request, _(u'Project saved.'))
                return HttpResponseRedirect(project_list_url)

    else:
        form = ProjectForm(team, instance=project)
        workflow_form = WorkflowForm(instance=workflow)

    return { 'team': team, 'project': project, 'form': form, 'workflow_form': workflow_form, }

def _add_task_note(task, note):
    """Add a note to the body of the Task in a nice way.

    Does not save() the Task.

    """
    task.body = (task.body + u'\n\n' + note).strip()

def _current_task(team_video, language_code):
    """Return the currently incomplete Task for the given video/language, or None.

    If there are multiple incomplete tasks, something is very wrong.  This
    function will delete them all and return None in that case.

    """
    tasks = list(team_video.task_set.incomplete().filter(language=language_code))

    if len(tasks) > 1:
        # There should only ever be one open task for a given language.  But
        # we'll be liberal here and deal with multiples.  And by "deal with"
        # I mean "ruthlessly delete".
        for task in tasks:
            task.deleted = True
            _add_task_note(task, u'Deleted due to duplicate tasks.')
            task.save()

        return None
    else:
        return tasks[0] if tasks else None

def _ensure_trans_task(team_video, subtitle_language):
    """Ensure that a trans[late/scribe] task exists for this (empty) language.

    This function should only be called after unpublishing, and when the
    subtitle language has no extant versions.

    Any existing review/approve tasks will be deleted.  If a trans[late/scribe]
    task exists, it will be updated.  Otherwise one will be created if and only
    if the language is on the teams "auto-create" list.

    """
    lc = subtitle_language.language_code
    task = _current_task(team_video, lc)

    if task:
        # Okay, at this point we know that we have a single current task.
        if task.type in (Task.TYPE_IDS['Subtitle'], Task.TYPE_IDS['Translate']):
            # If it's a trans[late/scribe] task, we can just update its version
            # and we're done.
            task.new_subtitle_version = None
            _add_task_note(task, u'Subtitle version unset when unpublishing.')
            task.save()
            return
        else:
            # Otherwise it's a review/approve task that needs to be deleted.
            task.deleted = True
            _add_task_note(task, u'Deleted when unpublishing.')
            task.save()

    # If we've reached this point, we know there are no existing tasks for this
    # language.  We may need to create one.
    workflow = Workflow.get_for_team_video(team_video)
    video = team_video.video
    preferred_langs = (
        TeamLanguagePreference.objects.get_preferred(team_video.team))

    other_languages_with_public_versions = (
        video.newsubtitlelanguage_set.having_public_versions()
                                     .exclude(pk=subtitle_language.pk))
    other_languages_with_versions = (
        video.newsubtitlelanguage_set.having_versions()
                                     .exclude(pk=subtitle_language.pk))

    if other_languages_with_public_versions.exists():
        # If there are other languages that already have public versions, we may
        # want to create a translate task.
        if workflow.autocreate_translate and lc in preferred_langs:
            # If this is one of the preferred languages for an autocreating
            # team, create it and get out.
            Task(team=team_video.team, team_video=team_video, assignee=None,
                 language=lc, type=Task.TYPE_IDS['Translate'],
                 new_subtitle_version=None).save()
            return
        else:
            # Otherwise the team doesn't want the task autocreated for some
            # reason, so we're done!
            return
    elif other_languages_with_versions.exists():
        # If there are other languages with versions, but they're not PUBLIC
        # yet, then this language will eventually get a translation task created
        # for it whenever the subtitling of those is finished.  We don't need to
        # do anything yet.
        return
    else:
        # Otherwise there are NO other languages with versions.  This one
        # doesn't have any versions either.  So we may need to create a subtitle
        # task for this video.
        if team_video.task_set.not_deleted().exists():
            # If there's already any task for any other language, we know we
            # don't need a subtitle task, so just bail now.
            #
            # TODO: Is this actually right?  We may actually need to create the
            # subtitle task here after all.  It depends.
            return
        elif workflow.autocreate_subtitle:
            # If the team autocreates subtitle tasks, then we might create one
            # here, depending on languages.
            palc = team_video.video.primary_audio_language_code
            if (not palc) or palc == lc:
                # If this language matches the video's primary audio language,
                # or if we don't know the PAL, we'll create the subtitle task.
                Task(team=team_video.team, team_video=team_video,
                    subtitle_version=None, language=(palc or ''),
                    type=Task.TYPE_IDS['Subtitle']).save()
                return
            else:
                # Otherwise the video has a PAL and this SubtitleLanguage isn't
                # for it, so we can just wait.
                return
        else:
            # If the team doesn't autocreate subtitle tasks at all we're done.
            return

def _ensure_task_exists(team_video, subtitle_language):
    """Ensure that the proper task exists for this (non-empty) language.

    This function should only be called after unpublishing, and when the
    subtitle language has some private versions, but no public ones.

    Any existing tasks will be updated.  Otherwise a review/approve task will be
    created.

    """
    lc = subtitle_language.language_code
    tip = subtitle_language.get_tip(public=False)

    task = _current_task(team_video, lc)

    if task:
        # Okay, at this point we know that we have a single current task.
        #
        # All we need to do now is point that task at the new extant tip and
        # we're done.
        #
        # TODO: We probably also need to handle new_review_base_version here
        # too at some point.
        task.new_subtitle_version = tip
        _add_task_note(task, u'Subtitle version reset when unpublishing.')
        task.save()
    else:
        # There are no tasks, so we need to create a review/approve task.
        workflow = Workflow.get_for_team_video(team_video)

        if workflow.review_enabled or workflow.approve_enabled:
            # We'll prefer approve to review if it's available.
            type = Task.TYPE_IDS['Approve'
                                 if workflow.approve_enabled
                                 else 'Review']

            Task(team=team_video.team, team_video=team_video, assignee=None,
                 language=lc, type=type, new_subtitle_version=tip,
                 new_review_base_version=tip).save()

            # TODO: Fix assignee with something like this:
            # last_task = (team_video.task_set.complete().filter(language=lang, type=type)
            #                                         .order_by('-completed')
            #                                         [:1])
            # if workflow.approve_allowed:
            #     can_do = can_approve
            # else:
            #     can_do = can_review
            #
            # assignee = None
            # if last_task:
            #     candidate = last_task[0].assignee
            #     if candidate and can_do(team_video, candidate, lang):
            #         assignee = candidate
            # task.set_expiration()
            return
        else:
            # If this workflow doesn't allow review/approve tasks, we'll bail.
            #
            # TODO: Should we create trans[late/scribe] tasks in this case
            # instead?
            return

def _clean_empty_translation_tasks(team_video):
    """Delete translation tasks that haven't been started if the source is gone.

    If we've unpublished (or deleted) a language, there may not be any languages
    to translate from any more.  Existing translation tasks that have already
    been started will be locked until the subs are reapproved, but empty
    translation tasks can just be deleted.  They'll get re-autocreated when the
    source is reapproved.

    """
    video = team_video.video
    sources = video.newsubtitlelanguage_set.having_public_versions()

    if sources.exists():
        # If there are still valid sources for translations, we don't need to
        # touch empty translation tasks.
        return
    else:
        # There are no sources to translate from yet.  So empty translation
        # tasks can be deleted.
        tasks = Task.objects.incomplete_translate().filter(team_video=team_video)
        for task in tasks:
            empty = not (
                SubtitleVersion.objects.extant()
                                       .filter(video=video,
                                               language_code=task.language)
                                       .exists())

            if empty:
                task.deleted = True
                _add_task_note(task, u'Deleted empty translation '
                                     u'task when unpublishing.')
                task.save()


def _get_languages_to_unpublish(subtitle_language):
    """Get a list of SubtitleLanguage objects that should be unpublished.

    Basically, this will return a list containing:

    * The given SubtitleLanguage.
    * Any translations based on it.
    * Any translations based on *those* translations.
    * Etc.

    This function does its best to handle bad data as well (like circular
    translation relationships).

    """
    languages = [subtitle_language]
    languages_to_delete = []
    seen = set()

    while languages:
        next_sl = languages.pop(0)

        if next_sl.language_code in seen:
            continue

        languages_to_delete.append(next_sl)
        languages.extend(next_sl.get_dependent_subtitle_languages())
        seen.add(next_sl.language_code)

    return languages_to_delete

def _writelock_languages_for_delete(request, subtitle_language):
    """Try to writelock the language and all dependents for deletion.

    Returns (could_lock, locked).  could_lock is a boolean, True if all the
    required languages were able to be writelocked, False otherwise.  locked is
    a list of all the languages that were locked in the process.

    Users of this function will need to release the writelock on each item in
    the locked list themselves.

    """
    to_lock = [subtitle_language]
    to_lock.extend(subtitle_language.get_dependent_subtitle_languages())

    locked = []

    for sl in to_lock:
        if sl.can_writelock(request.browser_id):
            sl.writelock(request.user, request.browser_id)
            locked.append(sl)
        else:
            messages.error(request, fmt(
                _(u'Someone else is currently editing %(language)s. '
                  u'Please try again later.'),
                language=sl.get_language_code_display()))
            return False, locked

    return True, locked

def delete_language(request, slug, lang_id):
    team = get_object_or_404(Team, slug=slug)
    if not can_delete_language(team, request.user):
        return redirect_to_login(reverse("teams:delete-language",
                                         kwargs={"slug": team.slug,
                                                 "lang_id": lang_id}))
    language = get_object_or_404(SubtitleLanguage, pk=lang_id)
    next_url = request.POST.get('next', language.video.get_absolute_url())
    team_video = language.video.get_team_video()
    if team_video.team.pk != team.pk:
        raise Http404()

    if request.method == 'POST':
        form = DeleteLanguageForm(request.user, team, language, request.POST)

        if form.is_valid():
            could_lock, locked = _writelock_languages_for_delete(request,
                                                                 language)
            try:
                if could_lock:
                    for sublang in form.languages_to_fork():
                        sublang.is_forked = True
                        sublang.save()

                    language.nuke_language()

                    metadata_manager.update_metadata(language.video.pk)

                    messages.success(request,
                                     _(u'Successfully deleted language.'))
                    return HttpResponseRedirect(next_url)
            finally:
                for sl in locked:
                    # We need to get a fresh copy of the SL here so that the
                    # save() in release_writelock doesn't overwrite any other
                    # changes we've made in the try block.  ORMs are fun.
                    SubtitleLanguage.objects.get(pk=sl.pk).release_writelock()
        else:
            for e in flatten_errorlists(form.errors):
                messages.error(request, e)
    else:
        form = DeleteLanguageForm(request.user, team, language)

    return render_to_response('teams/delete-language.html', {
        'form': form,
        'language': language,
    }, RequestContext(request))

@login_required
def auto_captions_status(request, slug):
    """
    Prints a simple table of partner status for captions, this should
    should be used internally (as a cvs file with tab delimiters)
    """
    buffer = []
    team = get_object_or_404(Team, slug=slug)
    if not team.is_member(request.user):
        return  HttpResponseForbidden("Not allowed")
    buffer.append( "Video\tproject\tURL\tstatus\tjob_id\ttask_id\tcreated on\tcompleted on")
    for tv in team.teamvideo_set.all().select_related("job", "project", "video"):
        jobs = tv.job_set.all()
        extra = ""
        if jobs.exists():
            j = jobs[0]
            extra = "%s\t%s\t%s\t%s\t%s" % (j.status, j.job_id, j.task_id, j.created_on, j.completed_on)
        url = "%s://%s%s" % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, tv.video.get_absolute_url())
        buffer.append( "Video:%s\t %s\t%s\t %s" % (tv.video.title,tv.project.name, url, extra))
    response =  HttpResponse( "\n".join(buffer), content_type="text/csv")
    response['Content-Disposition'] = 'filename=team-status.csv'
    return response


# Billing
@staff_member_required
def billing(request):
    BillingReportForm = make_billing_report_form()
    user = request.user

    if not DEV and not (user.is_superuser and user.is_active):
        raise Http404

    if request.method == 'POST':
        form = BillingReportForm(request.POST)
        if form.is_valid():
            teams = form.cleaned_data.get('teams')
            start_date = form.cleaned_data.get('start_date')
            end_date = form.cleaned_data.get('end_date')
            report_type = form.cleaned_data.get('type')

            report = BillingReport.objects.create( start_date=start_date, end_date=end_date, type=report_type)
            for team in teams:
                report.teams.add(team)

            process_billing_report.delay(report.pk)

    else:
        form = BillingReportForm()
    # We only get reports started less than a year ago, and prefetch teams
    reports = BillingReport.objects.filter(start_date__gte=datetime.now()-timedelta(days=61)).prefetch_related('teams').order_by('-pk')

    return render_to_response('teams/billing/reports.html', {
        'form': form,
        'reports': reports,
        'cutoff': BILLING_CUTOFF
    }, RequestContext(request))

@render_to('teams/feeds.html')
@settings_page
def video_feeds(request, team):
    return {
        'team': team,
        'feeds': team.videofeed_set.all(),
        'can_create_feed': can_add_video(team, request.user)
    }

@render_to('teams/feed.html')
@settings_page
def video_feed(request, team, feed_id):
    feed = get_object_or_404(VideoFeed, team=team, id=feed_id)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update':
            feed.update()
        elif action == 'delete':
            feed.delete()
            return redirect(reverse('teams:settings_feeds', kwargs={
                'slug': team.slug,
            }))
        return redirect(reverse('teams:video_feed', kwargs={
            'slug': team.slug,
            'feed_id': feed.id,
        }))


    imported_videos, pagination_info = paginate(feed.importedvideo_set.all(),
                                                8, request.GET.get('page'))
    context = {
        'team': team,
        'feed': feed,
        'imported_videos': [iv.video for iv in imported_videos],
    }
    context.update(pagination_info)
    return context
