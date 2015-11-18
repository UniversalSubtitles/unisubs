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

import datetime
import string
import urllib, urllib2
from collections import namedtuple

import json
from babelsubs.storage import diff as diff_subs
from babelsubs.generators.html import HTMLGenerator
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from videos.templatetags.paginator import paginate
from django.core.urlresolvers import reverse
from django.db import IntegrityError
from django.db.models import Sum
from django.http import (HttpResponse, Http404, HttpResponseRedirect,
                         HttpResponseForbidden)
from django.shortcuts import (render, render_to_response, get_object_or_404,
                              redirect)
from django.template import RequestContext
from django.utils.encoding import force_unicode
from django.utils.http import urlquote_plus
from django.utils.translation import ugettext, ugettext_lazy as _
from django.views.decorators.http import require_POST
from gdata.service import RequestError
from vidscraper.errors import Error as VidscraperError

import widget
from widget import rpc as widget_rpc
from auth.models import CustomUser as User
from subtitles.models import SubtitleLanguage, SubtitleVersion
from subtitles.permissions import (user_can_view_private_subtitles,
                                   user_can_edit_subtitles)
from subtitles.forms import SubtitlesUploadForm
from subtitles.pipeline import rollback_to
from teams.models import Task
from videos import permissions
from videos.decorators import (get_video_revision, get_video_from_code,
                               get_cached_video_from_code)
from videos.forms import (
    VideoForm, FeedbackForm, EmailFriendForm, UserTestResultForm,
    CreateVideoUrlForm, AddFromFeedForm,
    ChangeVideoOriginalLanguageForm, CreateSubtitlesForm,
)
from videos.models import (
    Video, Action, VideoUrl, AlreadyEditingException
)
from videos.rpc import VideosApiClass
from videos import share_utils
from videos.tasks import video_changed_tasks
from widget.views import base_widget_params
from externalsites.models import can_sync_videourl, get_sync_account
from utils import send_templated_email
from utils.basexconverter import base62
from utils.decorators import never_in_prod
from utils.objectlist import object_list
from utils.rpc import RpcRouter
from utils.text import fmt
from utils.translation import get_user_languages_from_request

from teams.permissions import can_edit_video, can_add_version, can_resync
from . import video_size

VIDEO_IN_ROW = 6

rpc_router = RpcRouter('videos:rpc_router', {
    'VideosApi': VideosApiClass()
})


# .e.g json, nor include aliases
AVAILABLE_SUBTITLE_FORMATS_FOR_DISPLAY = [
    'dfxp',  'sbv', 'srt', 'ssa', 'txt', 'vtt',
]

LanguageListItem = namedtuple("LanguageListItem", "name status tags url")

class LanguageList(object):
    """List of languages for the video pages."""

    def __init__(self, video):
        original_languages = []
        other_languages = []
        for lang in video.all_subtitle_languages():
            public_tip = lang.get_tip(public=False)
            if public_tip is None or public_tip.subtitle_count == 0:
                # no versions in this language yet
                continue
            language_name = lang.get_language_code_display()
            status = self._calc_status(lang)
            tags = self._calc_tags(lang)
            url = lang.get_absolute_url()
            item = LanguageListItem(language_name, status, tags, url)
            if lang.language_code == video.primary_audio_language_code:
                original_languages.append(item)
            else:
                other_languages.append(item)
        original_languages.sort(key=lambda li: li.name)
        other_languages.sort(key=lambda li: li.name)
        self.items = original_languages + other_languages

    def _calc_status(self, lang):
        if lang.subtitles_complete:
            if lang.has_public_version():
                return 'complete'
            else:
                return 'needs-review'
        else:
            if lang.is_synced(public=False):
                return 'incomplete'
            else:
                return 'needs-timing'

    def _calc_tags(self, lang):
        tags = []
        if lang.is_primary_audio_language():
            tags.append(ugettext(u'original'))

        team_video = lang.video.get_team_video()

        if not lang.subtitles_complete:
            tags.append(ugettext(u'incomplete'))
        elif team_video is not None:
            # subtiltes are complete, check if they are under review/approval.
            incomplete_tasks = (Task.objects.incomplete()
                                            .filter(team_video=team_video,
                                                    language=lang.language_code))
            for t in incomplete_tasks:
                if t.type == Task.TYPE_IDS['Review']:
                    tags.append(ugettext(u'needs review'))
                    break
                elif t.type == Task.TYPE_IDS['Approve']:
                    tags.append(ugettext(u'needs approval'))
                    break
                else:
                    # subtitles are complete, but there's a subtitle/translate
                    # task for them.  They must have gotten sent back.
                    tags.append(ugettext(u'needs editing'))
                    break
        return tags

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

def index(request):
    return render_to_response('index.html', {},
                              context_instance=RequestContext(request))

def watch_page(request):
    context = {
        'featured_videos': Video.objects.featured()[:VIDEO_IN_ROW],
        'latest_videos': Video.objects.latest()[:VIDEO_IN_ROW*3],
    }
    return render_to_response('videos/watch.html', context,
                              context_instance=RequestContext(request))

def featured_videos(request):
    return render_to_response('videos/featured_videos.html', {},
                              context_instance=RequestContext(request))

def latest_videos(request):
    return render_to_response('videos/latest_videos.html', {},
                              context_instance=RequestContext(request))

@login_required
def create(request):
    video_form = VideoForm(request.user, request.POST or None)
    context = {
        'video_form': video_form,
        'initial_url': request.GET.get('initial_url'),
    }
    if video_form.is_valid():
        try:
            video = video_form.save()
        except (VidscraperError, RequestError):
            context['vidscraper_error'] = True
            return render_to_response('videos/create.html', context,
                          context_instance=RequestContext(request))
        messages.info(request, message=_(u'''Here is the subtitle workspace for your video.
        You can share the video with friends, or get an embed code for your site. To start
        new subtitles, click \"Add a new language!\" in the sidebar.'''))

        url_obj = video.videourl_set.filter(primary=True).all()[:1].get()
        if url_obj.type != 'Y':
            # Check for all types except for Youtube
            if not url_obj.effective_url.startswith('https'):
                messages.warning(request, message=_(u'''You have submitted a video
                that is served over http.  Your browser may display mixed
                content warnings.'''))

        if video_form.created:
            messages.info(request, message=_(u'''Existing subtitles will be imported in a few minutes.'''))
        return redirect(video.get_absolute_url())
    return render_to_response('videos/create.html', context,
                              context_instance=RequestContext(request))

create.csrf_exempt = True

def shortlink(request, encoded_pk):
    pk = base62.to_decimal(encoded_pk)
    video = get_object_or_404(Video, pk=pk)
    return redirect(video, video=video, permanent=True)

class VideoPageContext(dict):
    """Context dict for the video page."""
    def __init__(self, request, video, video_url, tab, workflow,
                 tab_only=False):
        dict.__init__(self)
        self.workflow = workflow
        self['video'] = video
        self['create_subtitles_form'] = CreateSubtitlesForm(
            request, video, request.POST or None)
        self['extra_tabs'] = workflow.extra_tabs(request.user)
        if not tab_only:
            self.setup(request, video, video_url)
        self.setup_tab(request, video, video_url, tab)

    def setup(self, request, video, video_url):
        self['widget_settings'] = json.dumps(
            widget_rpc.get_general_settings(request))
        self['add_language_mode'] = self.workflow.get_add_language_mode(
            request.user)

        self['task'] =  _get_related_task(request)
        team_video = video.get_team_video()
        if team_video is not None:
            self['team'] = team_video.team
            self['team_video'] = team_video
        else:
            self['team'] = self['team_video'] = None

    def setup_tab(self, request, video, video_url, tab):
        for name, title in self['extra_tabs']:
            if tab == name:
                self['extra_tab'] = True
                self.setup_extra_tab(request, video, video_url, tab)
                return
        self['extra_tab'] = False
        method_name = 'setup_tab_%s' % tab
        setup_tab_method = getattr(self, method_name, None)
        if setup_tab_method:
            setup_tab_method(request, video, video_url)

    def setup_extra_tab(self, request, video, video_url, tab):
        method_name = 'setup_tab_%s' % tab
        setup_tab_method = getattr(self.workflow, method_name, None)
        if setup_tab_method:
            self.update(setup_tab_method(request, video, video_url))

    def setup_tab_video(self, request, video, video_url):
        self['width'] = video_size["large"]["width"]
        self['height'] = video_size["large"]["height"]

    def setup_tab_urls(self, request, video, video_url):
        self['create_videourl_form'] = CreateVideoUrlForm(request.user, initial={
            'video': video.pk,
        })
        self['video_urls'] = [
            (vurl, get_sync_account(video, vurl))
            for vurl in video.videourl_set.all()
        ]

@get_video_from_code
def redirect_to_video(request, video):
    return redirect(video, permanent=True)

def calc_tab(request, workflow):
    tab = request.GET.get('tab')
    if tab in ('urls', 'comments', 'activity', 'video'):
        return tab # default tab
    for name, title in workflow.extra_tabs(request.user):
        if name == tab:
            # workflow extra tab
            return tab
    # invalid tab, force it to be video
    return 'video'

@get_cached_video_from_code('video-page')
def video(request, video, video_url=None, title=None):
    """
    If user is about to perform a task on this video, then t=[task.pk]
    will be passed to as a url parameter.
    """

    if video_url:
        video_url = get_object_or_404(VideoUrl, pk=video_url)

    # FIXME: what is this crazy mess?
    if not video_url and ((video.title_for_url() and not video.title_for_url() == title) or (not video.title and title)):
        return redirect(video, permanent=True)

    workflow = video.get_workflow()

    tab = calc_tab(request, workflow)
    template_name = 'videos/video-%s.html' % tab
    context = VideoPageContext(request, video, video_url, tab, workflow)
    context['tab'] = tab

    if context['create_subtitles_form'].is_valid():
        return context['create_subtitles_form'].handle_post()

    return render(request, template_name, context)

def _get_related_task(request):
    """
    Checks if request has t=[task-id], and if so checks if the current
    user can perform it, in case all goes well, return the task to be
    performed.
    """
    task_pk = request.GET.get('t', None)
    if task_pk:
        from teams.permissions import can_perform_task
        try:
            task = Task.objects.get(pk=task_pk)
            if can_perform_task(request.user, task):
                return task
        except Task.DoesNotExist:
            return


def actions_list(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    qs = Action.objects.for_video(video)

    extra_context = {
        'video': video
    }

    return object_list(request, queryset=qs, allow_empty=True,
                       paginate_by=settings.ACTIVITIES_ONPAGE,
                       template_name='videos/actions_list.html',
                       template_object_name='action',
                       extra_context=extra_context)

def check_upload_subtitles_permissions(request):
    # check authorization...  This is pretty hacky.  We should implement
    # #1830.
    if request.user.is_authenticated():
        return True
    username = request.META.get('HTTP_X_API_USERNAME', None)
    api_key = request.META.get( 'HTTP_X_APIKEY', None)
    if not username or not api_key:
        return False
    try:
        import apiv2
        from tastypie.models import ApiKey
    except ImportError:
        return False
    try:
        api_user = User.objects.get(username=username)
    except User.DoesNotExist:
        return False
    if not ApiKey.objects.filter(user=api_user, key=api_key).exists():
        return False
    request.user = api_user
    return True

def upload_subtitles(request):
    if not check_upload_subtitles_permissions(request):
        path = request.get_full_path()
        return redirect_to_login(path)

    output = {'success': False}
    video = Video.objects.get(id=request.POST['video'])
    form = SubtitlesUploadForm(request.user, video, True, request.POST,
                               request.FILES,
                               initial={'primary_audio_language_code':video.primary_audio_language_code},
                               allow_all_languages=True)

    try:
        if form.is_valid():
            version = form.save()
            output['success'] = True
            output['next'] = version.subtitle_language.get_absolute_url()
            output['msg'] = ugettext(
                u'Thank you for uploading. '
                u'It may take a minute or so for your subtitles to appear.')
        else:
            output['errors'] = form.get_errors()
    except AlreadyEditingException, e:
        output['errors'] = {'__all__': [force_unicode(e.msg)]}
    except Exception, e:
        import traceback
        traceback.print_exc()
        from raven.contrib.django.models import client
        client.create_from_exception()
        output['errors'] = {'__all__': [force_unicode(e)]}

    return HttpResponse(json.dumps(output))

def feedback(request, hide_captcha=False):
    output = dict(success=False)
    form = FeedbackForm(request.POST, initial={'captcha': request.META['REMOTE_ADDR']},
                        hide_captcha=hide_captcha)
    if form.is_valid():
        form.send(request)
        output['success'] = True
    else:
        output['errors'] = form.get_errors()
    return HttpResponse(json.dumps(output), "text/javascript")

def email_friend(request):
    text = request.GET.get('text', '')
    link = request.GET.get('link', '')
    if link:
        text = link if not text else '%s\n%s' % (text, link)
    from_email = ''
    if request.user.is_authenticated():
        from_email = request.user.email
    initial = dict(message=text, from_email=from_email)
    if request.method == 'POST':
        form = EmailFriendForm(request.POST, auto_id="email_friend_id_%s", label_suffix="")
        if form.is_valid():
            form.send()
            messages.info(request, 'Email Sent!')

            return redirect(request.get_full_path())
    else:
        form = EmailFriendForm(auto_id="email_friend_id_%s", initial=initial, label_suffix="")
    context = {
        'form': form
    }
    return render_to_response('videos/email_friend.html', context,
                              context_instance=RequestContext(request))


@get_video_from_code
def legacy_history(request, video, lang=None):
    """
    In the old days we allowed only one translation per video.
    Therefore video urls looked like /vfjdh2/en/
    Now that this constraint is removed we need to redirect old urls
    to the new view, that needs
    """
    try:
        language = video.subtitle_language(lang)
        if language is None:
            raise SubtitleLanguage.DoesNotExist("No such language")
    except SubtitleLanguage.DoesNotExist:
        raise Http404()

    return HttpResponseRedirect(reverse("videos:translation_history", kwargs={
            'video_id': video.video_id,
            'lang_id': language.pk,
            'lang': language.language_code,
            }))

class LanguagePageContext(dict):
    """Context dict for language pages

    This class defines the base class that sets up the variables we use for
    all the languages classes.  For the specific language pages (subtitles,
    comments, revisions), we use a subclass of this.
    """
    def __init__(self, request, video, lang_code, lang_id, version_id,
                 tab_only=False):
        dict.__init__(self)
        language = self._get_language(video, lang_code, lang_id)
        self.public_only = self.calc_public_only(request, video,
                                                 language.language_code)
        version = self._get_version(request, video, language, version_id)
        self['video'] = video
        self['language'] = language
        self['version'] = version
        self['user'] = request.user
        self['create_subtitles_form'] = CreateSubtitlesForm(
            request, video, request.POST or None)
        if not tab_only:
            self.setup(request, video, language, version)
        self.setup_tab(request, video, language, version)

    def _get_language(self, video, lang_code, lang_id):
        """Get a language for the language page views.

        For historical reasons, we normally specify both a language code and a
        language id.  This method takes both of those and returns a
        SubtitleLanguage.
        """
        try:
            language = video.language_with_pk(lang_id)
        except SubtitleLanguage.DoesNotExist:
            raise Http404
        if language.language_code != lang_code:
            raise Http404
        return language

    def calc_public_only(self, request, video, language_code):
        return not user_can_view_private_subtitles(request.user, video,
                                                   language_code)

    def _get_version(self, request, video, language, version_id):
        """Get the SubtitleVersion to use for a language page."""
        team_video = video.get_team_video()
        if version_id:
            try:
                return language.get_version_by_id(version_id,
                                                  public=self.public_only)
            except SubtitleVersion.DoesNotExist:
                raise Http404
        else:
            return language.get_tip(public=self.public_only)

    def setup(self, request, video, language, version):
        """Setup context variables."""

        self['revision_count'] = language.version_count()
        self['page_title'] = self.page_title(language)
        self['edit_url'] = language.get_widget_url()
        self['width'] = video_size["thumb"]["width"]
        self['height'] = video_size["thumb"]["height"]
        self['video_url'] = video.get_video_url()
        self['language'] = language
        share_utils.add_share_panel_context_for_history(self, video, language)
        if video.get_team_video() is not None:
            self['team'] = video.get_team_video().team
        else:
            self['team'] = None
        if version is not None:
            self['metadata'] = version.get_metadata().convert_for_display()
        else:
            self['metadata'] = video.get_metadata().convert_for_display()

        self['rollback_allowed'] = self.calc_rollback_allowed(
            request, video, version, language)

    def calc_rollback_allowed(self, request, video, version, language):
        if version and version.next_version():
            return user_can_edit_subtitles(request.user, video,
                                           language.language_code)
        else:
            return False

    def setup_tab(self, request, video, language, video_url):
        """Setup tab-specific variables."""
        pass

    @staticmethod
    def page_title(language):
        return fmt(ugettext('%(title)s with subtitles | Amara'),
                   title=language.title_display())

class LanguagePageContextSubtitles(LanguagePageContext):
    def setup_tab(self, request, video, language, version):
        team_video = video.get_team_video()
        user_can_edit = user_can_edit_subtitles(request.user, video,
                                                language.language_code)
        public_langs = (video.newsubtitlelanguage_set
                        .having_public_versions().count())

        self['downloadable_formats'] = AVAILABLE_SUBTITLE_FORMATS_FOR_DISPLAY
        self['edit_disabled'] = not user_can_edit
        self['show_download_all'] = public_langs > 1
        # If there are tasks for this language, the user has to go through the
        # tasks panel to edit things instead of doing it directly from here.
        if user_can_edit and video.get_team_video():
            has_open_task = (Task.objects.incomplete()
                             .filter(team_video=video.get_team_video(),
                                     language=language.language_code)
                             .exists())
            if has_open_task:
                self['edit_disabled'] = True
                self['must_use_tasks'] = True
        if 'rollback_allowed' not in self:
            self['rollback_allowed'] = self.calc_rollback_allowed(
                request, video, version, language)

class LanguagePageContextComments(LanguagePageContext):
    pass

class LanguagePageContextRevisions(LanguagePageContext):
    REVISIONS_PER_PAGE = 10

    def setup_tab(self, request, video, language, version):
        if self.public_only:
            revisions_qs = language.subtitleversion_set.public()
        else:
            revisions_qs = language.subtitleversion_set.extant()
        revisions_qs = revisions_qs.order_by('-version_number')
        revisions_per_page =  request.GET.get('revisions_per_page') or self.REVISIONS_PER_PAGE
        revisions, pagination_info = paginate(
            revisions_qs, revisions_per_page, request.GET.get('page'))
        self.update(pagination_info)
        self['more'] = int(revisions_per_page) + 10
        self['revisions'] = language.optimize_versions(revisions)

class LanguagePageContextSyncHistory(LanguagePageContext):
    def setup_tab(self, request, video, language, version):
        self['sync_history'] = (language.synchistory_set
                                .select_related('version')
                                .fetch_with_accounts())
        self['current_version'] = language.get_public_tip()
        synced_versions = []
        for video_url in video.get_video_urls():
            if not can_sync_videourl(video_url):
                continue
            try:
                version = (language.syncedsubtitleversion_set.
                           select_related('version').
                           get(video_url=video_url)).version
            except ObjectDoesNotExist:
                version = None
            synced_versions.append({
                'video_url': video_url,
                'version': version,
                'syncable': get_sync_account(video, video_url),
            })
        self['synced_versions'] = synced_versions

@get_video_from_code
def language_subtitles(request, video, lang, lang_id, version_id=None):
    tab = request.GET.get('tab')
    if tab == 'revisions':
        ContextClass = LanguagePageContextRevisions
    elif tab == 'comments':
        ContextClass = LanguagePageContextComments
    elif tab == 'sync-history':
        if not permissions.can_user_resync(video, request.user):
            return redirect_to_login(request.build_absolute_uri())
        ContextClass = LanguagePageContextSyncHistory
    else:
        # force tab to be subtitles if it doesn't match either of the other
        # tabs
        tab = 'subtitles'
        ContextClass = LanguagePageContextSubtitles
    template_name = 'videos/language-%s.html' % tab
    context = ContextClass(request, video, lang, lang_id, version_id)
    context['tab'] = tab
    if context['create_subtitles_form'].is_valid():
        return context['create_subtitles_form'].handle_post()
    return render(request, template_name, context)

def _widget_params(request, video, version_no=None, language=None, video_url=None, size=None):
    primary_url = video_url or video.get_video_url()
    alternate_urls = [vu.effective_url for vu in video.videourl_set.all()
                      if vu.effective_url != primary_url]
    params = {'video_url': primary_url,
              'alternate_video_urls': alternate_urls,
              'base_state': {}}

    if version_no:
        params['base_state']['revision'] = version_no

    if language:
        params['base_state']['language_code'] = language.language_code
        params['base_state']['language_pk'] = language.pk
    if size:
        params['video_config'] = {"width":size[0], "height":size[1]}

    return base_widget_params(request, params)

@login_required
@get_video_revision
def rollback(request, version):
    is_writelocked = version.subtitle_language.is_writelocked
    if not user_can_edit_subtitles(request.user, version.video,
                                   version.subtitle_language.language_code):
        messages.error(request, _(u"You don't have permission to rollback "
                                  "this language"))
    elif is_writelocked:
        messages.error(request, u'Can not rollback now, because someone is editing subtitles.')
    elif not version.next_version():
        messages.error(request, message=u'Can not rollback to the last version')
    else:
        messages.success(request, message=u'Rollback successful')
        version = rollback_to(version.video,
                version.subtitle_language.language_code,
                version_number=version.version_number,
                rollback_author=request.user)
        video_changed_tasks.delay(version.video.id, version.id)
        return redirect(version.subtitle_language.get_absolute_url()+'#revisions')
    return redirect(version)

@get_video_revision
def diffing(request, first_version, second_pk):
    language = first_version.subtitle_language
    second_version = get_object_or_404(
        SubtitleVersion.objects.extant(),
        pk=second_pk, subtitle_language=language)

    if first_version.video != second_version.video:
        # this is either a bad bug, or someone evil
        raise "Revisions for diff videos"

    if first_version.pk < second_version.pk:
        # this is just stupid Instead of first, second meaning
        # chronological order (first cames before second)
        # it means  the opposite, so make sure the first version
        # has a larger version no than the second
        first_version, second_version = second_version, first_version

    video = first_version.subtitle_language.video
    diff_data = diff_subs(first_version.get_subtitles(), second_version.get_subtitles(), mappings=HTMLGenerator.MAPPINGS)
    team_video = video.get_team_video()
    first_version_previous = first_version.previous_version()
    first_version_next = first_version.next_version()
    second_version_previous = second_version.previous_version()
    second_version_next = second_version.next_version()
    context = {
        'video': video,
        'diff_data': diff_data,
        'language': language,
        'first_version': first_version,
        'second_version': second_version,
        'latest_version': language.get_tip(),
        'first_version_previous': first_version_previous if (first_version_previous != second_version) else None,
        'first_version_next': first_version_next,
        'second_version_previous': second_version_previous,
        'second_version_next': second_version_next if (second_version_next != first_version) else None,
        'rollback_allowed': user_can_edit_subtitles(request.user, video,
                                                    language.language_code),
        'width': video_size["small"]["width"],
        'height': video_size["small"]["height"],
        'video_url': video.get_video_url(),
    }

    return render_to_response('videos/diffing.html', context,
                              context_instance=RequestContext(request))

@login_required
def stop_notification(request, video_id):
    user_id = request.GET.get('u')
    hash = request.GET.get('h')

    if not user_id or not hash:
        raise Http404

    video = get_object_or_404(Video, video_id=video_id)
    user = get_object_or_404(User, id=user_id)
    context = dict(video=video, u=user)

    if hash and user.hash_for_video(video_id) == hash:
        video.followers.remove(user)
        for l in video.subtitlelanguage_set.all():
            l.followers.remove(user)
        if request.user.is_authenticated() and not request.user == user:
            logout(request)
    else:
        context['error'] = u'Incorrect secret hash'
    return render_to_response('videos/stop_notification.html', context,
                              context_instance=RequestContext(request))

@login_required
@require_POST
def video_url_make_primary(request):
    output = {}
    id = request.POST.get('id')
    status = 200
    if id:
        try:
            obj = VideoUrl.objects.get(id=id)
            tv = obj.video.get_team_video()
            if tv and not permissions.can_user_edit_video_urls(obj.video, request.user):
                output['error'] = ugettext('You have not permission change this URL')
                status = 403
            else:
                obj.make_primary(user=request.user)
        except VideoUrl.DoesNotExist:
            output['error'] = ugettext('Object does not exist')
            status = 404
    return HttpResponse(json.dumps(output), status=status)

@login_required
@require_POST
def video_url_remove(request):
    output = {}
    id = request.POST.get('id')
    status = 200
    if id:
        try:
            obj = VideoUrl.objects.get(id=id)
            tv = obj.video.get_team_video()
            if tv and not permissions.can_user_edit_video_urls(obj.video, request.user):
                output['error'] = ugettext('You have not permission delete this URL')
                status = 403
            else:
                try:
                    obj.remove(request.user)
                except IntegrityError, e:
                    output['error'] = str(e)
                    status = 403
        except VideoUrl.DoesNotExist:
            output['error'] = ugettext('Object does not exist')
            status = 404
    return HttpResponse(json.dumps(output), status=status)

@login_required
def video_url_create(request):
    output = {}

    form = CreateVideoUrlForm(request.user, request.POST)
    if form.is_valid():
        obj = form.save()
        video = form.cleaned_data['video']
        users = video.notification_list(request.user)

        for user in users:
            subject = u'New video URL added by %(username)s to "%(video_title)s" on amara.org'
            subject = subject % {'url': obj.url, 'username': obj.added_by, 'video_title': video}
            context = {
                'video': video,
                'video_url': obj,
                'user': user,
                'domain': Site.objects.get_current().domain,
                'hash': user.hash_for_video(video.video_id)
            }
            send_templated_email(user, subject,
                                 'videos/email_video_url_add.html',
                                 context, fail_silently=not settings.DEBUG)
    else:
        output['errors'] = form.get_errors()

    return HttpResponse(json.dumps(output))

@staff_member_required
def reindex_video(request, video_id):
    from teams.tasks import update_one_team_video

    video = get_object_or_404(Video, video_id=video_id)
    video.update_search_index()

    team_video = video.get_team_video()

    if team_video:
        update_one_team_video.delay(team_video.id)

def subscribe_to_updates(request):
    email_address = request.POST.get('email_address', '')
    data = urllib.urlencode({'email': email_address})
    req = urllib2.Request(
        'http://pcf8.pculture.org/interspire/form.php?form=3', data)
    urllib2.urlopen(req)
    return HttpResponse('ok', 'text/plain')

def test_celery(request):
    from videos.tasks import add
    add.delay(1, 2)
    return HttpResponse('Hello, from Amazon SQS backend for Celery!')

@staff_member_required
def test_celery_exception(request):
    from videos.tasks import raise_exception
    raise_exception.delay('Exception in Celery', should_be_logged='Hello, man!')
    return HttpResponse('Hello, from Amazon SQS backend for Celery! Look for exception.')

@never_in_prod
@staff_member_required
def video_staff_delete(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    video.delete()
    return HttpResponse("ok")

def video_debug(request, video_id):
    from widget import video_cache as vc
    from django.core.cache import cache
    from videos.models import VIDEO_TYPE_YOUTUBE

    video = get_object_or_404(Video, video_id=video_id)
    vid = video.video_id
    get_subtitles_dict = {}

    for l in video.newsubtitlelanguage_set.all():
        cache_key = vc._subtitles_dict_key(vid, l.pk, None)
        get_subtitles_dict[l.language_code] = cache.get(cache_key)

    cache = {
        "get_video_urls": cache.get(vc._video_urls_key(vid)),
        "get_subtitles_dict": get_subtitles_dict,
        "get_video_languages": cache.get(vc._video_languages_key(vid)),

        "get_video_languages_verbose": cache.get(vc._video_languages_verbose_key(vid)),
        "writelocked_langs": cache.get(vc._video_writelocked_langs_key(vid)),
    }

    tasks = Task.objects.filter(team_video=video)

    is_youtube = video.videourl_set.filter(type=VIDEO_TYPE_YOUTUBE).count() != 0

    return render_to_response("videos/video_debug.html", {
            'video': video,
            'is_youtube': is_youtube,
            'tasks': tasks,
            "cache": cache
    }, context_instance=RequestContext(request))

def reset_metadata(request, video_id):
    video = get_object_or_404(Video, video_id=video_id)
    video_changed_tasks.delay(video.id)
    return HttpResponse('ok')

def set_original_language(request, video_id):
    """
    We only allow if a video is own a team, or the video owner is the
    logged in user
    """
    video = get_object_or_404(Video, video_id=video_id)
    if not (can_edit_video(video.get_team_video(), request.user) or video.user == request.user):
        return HttpResponseForbidden("Can't touch this.")
    form = ChangeVideoOriginalLanguageForm(request.POST or None, initial={
        'language_code': video.primary_audio_language_code
    })
    if request.method == "POST" and form.is_valid():
        video.primary_audio_language_code = form.cleaned_data['language_code']
        video.save()
        messages.success(request, fmt(
            _(u'The language for %(video)s has been changed'),
            video=video))
        return HttpResponseRedirect(reverse("videos:set_original_language", args=(video_id,)))
    return render_to_response("videos/set-original-language.html", {
        "video": video,
        'form': form
    }, context_instance=RequestContext(request)
    )
