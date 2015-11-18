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

from django.conf import settings
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.template import RequestContext
from django.template.defaultfilters import slugify
from django.template.loader import render_to_string
from django.utils.translation import ugettext as _
from django.utils.translation import ugettext_lazy

from subtitles.models import SubtitleLanguage
from videos.models import Video, Action
from videos.tasks import send_change_title_email
from utils.celery_search_index import update_search_index
from utils.multi_query_set import MultiQuerySet
from utils.rpc import Error, Msg, RpcExceptionEvent, add_request_to_kwargs
from utils.translation import get_user_languages_from_request

class VideosApiClass(object):
    authentication_error_msg = ugettext_lazy(u'You should be authenticated.')

    popular_videos_sorts = {
        'week': 'week_views',
        'month': 'month_views',
        'year': 'year_views',
        'total': 'total_views'
    }

    def unfeature_video(self, video_id, user):
        if not user.has_perm('videos.edit_video'):
            raise RpcExceptionEvent(_(u'You have not permission'))

        try:
            c = Video.objects.filter(pk=video_id).update(featured=None)
        except (ValueError, TypeError):
            raise RpcExceptionEvent(_(u'Incorrect video ID'))

        if not c:
            raise RpcExceptionEvent(_(u'Video does not exist'))

        update_search_index.delay(Video, video_id)

        return {}

    def feature_video(self, video_id, user):
        if not user.has_perm('videos.edit_video'):
            raise RpcExceptionEvent(_(u'You have not permission'))

        try:
            c = Video.objects.filter(pk=video_id).update(featured=datetime.datetime.today())
        except (ValueError, TypeError, Video.DoesNotExist):
            raise RpcExceptionEvent(_(u'Incorrect video ID'))

        if not c:
            raise RpcExceptionEvent(_(u'Video does not exist'))

        update_search_index.delay(Video, video_id)

        return {}

    @add_request_to_kwargs
    def load_video_languages(self, video_id, user, request):
        """
        Load langs for search pages. Will take into consideration
        the languages the user speaks.
        Ordering is user language, then completness , then percentage
        then name of the language.
        We're sorting all in memory since those sets should be pretty small
        """
        LANGS_COUNT = 7

        try:
            video = Video.objects.get(pk=video_id)
        except Video.DoesNotExist:
            video = None

        user_langs = get_user_languages_from_request(request)

        langs = list(video.newsubtitlelanguage_set.having_nonempty_tip())

        first_languages = [] #user languages and original
        other_languages = [] #other languages already ordered by subtitle_count

        for language in langs:
            if language.language_code in user_langs or language.is_primary_audio_language():
                first_languages.append(language)
            else:
                other_languages.append(language)

        def _cmp_first_langs(lang1, lang2):
            """
            languages should original in user_langs
            """
            in_user_language_cmp = cmp(lang1.language_code in user_langs, lang2.language_code in user_langs)

            #one is not in user language
            if in_user_language_cmp != 0:
                return in_user_language_cmp

            if lang1.language_code in user_langs:
                #both in user's language, sort alphabetically
                return cmp(lang2.get_language_code_display(), lang1.get_language_code_display())

            #one should be original
            return cmp(lang1.is_original, lang2.is_original)

        first_languages.sort(cmp=_cmp_first_langs, reverse=True)

        #fill first languages to LANGS_COUNT
        if len(first_languages) < LANGS_COUNT:
            other_languages = other_languages[:(LANGS_COUNT-len(first_languages))]
            other_languages.sort(lambda l1, l2: cmp(l1.get_language_code_display(), l2.get_language_code_display()))
            langs = first_languages + other_languages
        else:
            langs = first_languages[:LANGS_COUNT]

        context = {
            'video': video,
            'languages': langs
        }

        return {
            'content': render_to_string('videos/_video_languages.html', context)
        }

    @add_request_to_kwargs
    def load_featured_page(self, page, request, user):
        qs = Video.objects.featured()
        return render_page(page, qs, request=request)

    @add_request_to_kwargs
    def load_latest_page(self, page, request, user):
        qs = Video.objects.latest()
        return render_page(page, qs, request=request)

    @add_request_to_kwargs
    def load_featured_page_volunteer(self, page, request, user):
        rel, rest = self._get_volunteer_sqs(request, user)

        rel = rel.filter(featured__gt=datetime.datetime(datetime.MINYEAR, 1, 1)) \
            .order_by('-featured')

        rest = rest.filter(featured__gt=datetime.datetime(datetime.MINYEAR, 1, 1)) \
            .order_by('-featured')

        count = rel.count() + rest.count()

        mqs = MultiQuerySet(rel, rest)
        mqs.set_count(count)

        return render_page(page, mqs, request=request)

    @add_request_to_kwargs
    def load_requested_page_volunteer(self, page, request, user):
        user_langs = get_user_languages_from_request(request)

        rel, rest = self._get_volunteer_sqs(request, user)

        rel = rel.filter(requests_exact__in=user_langs)
        rest = rest.filter(requests_exact__in=user_langs)

        count = rel.count() + rest.count()

        mqs = MultiQuerySet(rel, rest)
        mqs.set_count(count)

        return render_page(page, mqs, request=request)

    @add_request_to_kwargs
    def load_latest_page_volunteer(self, page, request, user):
        rel, rest = self._get_volunteer_sqs(request, user)
        rel = rel.order_by('-created')
        rest = rest.order_by('-created')

        count = rel.count() + rest.count()

        mqs = MultiQuerySet(rel, rest)
        mqs.set_count(count)

        return render_page(page, mqs, request=request)

    @add_request_to_kwargs
    def load_popular_page_volunteer(self, page, sort, request, user):

        sort_types = {
            'today': 'today_views',
            'week' : 'week_views',
            'month': 'month_views',
            'year' : 'year_views',
            'total': 'total_views'
        }

        sort_field = sort_types.get(sort, 'week_views')

        rel, rest = self._get_volunteer_sqs(request, user)
        rel = rel.order_by('-%s' % sort_field)
        rest = rest.order_by('-%s' % sort_field)

        count = rel.count() + rest.count()

        mqs = MultiQuerySet(rel, rest)
        mqs.set_count(count)

        return render_page(page, mqs,  request=request)

    def change_title_video(self, video_pk, title, user):
        title = title.strip()
        if not user.is_authenticated():
            return Error(self.authentication_error_msg)

        if not title:
            return Error(_(u'Title can\'t be empty'))

        try:
            video = Video.objects.get(pk=video_pk)
            if title and not video.title or video.is_html5() or user.is_superuser:
                if title != video.title:
                    old_title = video.title_display()
                    video.title = title
                    video.slug = slugify(video.title)
                    video.save()
                    update_search_index.delay(Video, video.pk)
                    Action.change_title_handler(video, user)
                    send_change_title_email.delay(video.id, user and user.id, old_title.encode('utf8'), video.title.encode('utf8'))
            else:
                return Error(_(u'Title can\'t be changed for this video'))
        except Video.DoesNotExist:
            return Error(_(u'Video does not exist'))

        return Msg(_(u'Title was changed success'))

    def follow(self, video_id, user):
        if not user.is_authenticated():
            return Error(self.authentication_error_msg)

        try:
            video = Video.objects.get(pk=video_id)
        except Video.DoesNotExist:
            return Error(_(u'Video does not exist.'))

        video.followers.add(user)

        for l in video.newsubtitlelanguage_set.all():
            l.followers.add(user)

        return Msg(_(u'You are following this video now.'))

    def unfollow(self, video_id, user):
        if not user.is_authenticated():
            return Error(self.authentication_error_msg)

        try:
            video = Video.objects.get(pk=video_id)
        except Video.DoesNotExist:
            return Error(_(u'Video does not exist.'))

        video.followers.remove(user)

        for l in video.newsubtitlelanguage_set.all():
            l.followers.remove(user)

        return Msg(_(u'You stopped following this video now.'))

    def follow_language(self, language_id, user):
        if not user.is_authenticated():
            return Error(self.authentication_error_msg)

        try:
            language = SubtitleLanguage.objects.get(pk=language_id)
        except SubtitleLanguage.DoesNotExist:
            return Error(_(u'Subtitles does not exist.'))

        language.followers.add(user)

        return Msg(_(u'You are following this subtitles now.'))

    def unfollow_language(self, language_id, user):
        if not user.is_authenticated():
            return Error(self.authentication_error_msg)

        try:
            language = SubtitleLanguage.objects.get(pk=language_id)
        except SubtitleLanguage.DoesNotExist:
            return Error(_(u'Subtitles does not exist.'))

        language.followers.remove(user)

        return Msg(_(u'You stopped following this subtitles now.'))

def render_page(page, qs, on_page=30, request=None,
                 template='videos/_watch_page.html', extra_context={},
                 display_views='total'):
    paginator = Paginator(qs, on_page)

    try:
        page = int(page)
    except ValueError:
        page = 1

    try:
        page_obj = paginator.page(page)
    except (EmptyPage, InvalidPage):
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'video_list': page_obj.object_list,
        'page': page_obj,
        'display_views': display_views
    }
    context.update(extra_context)

    if request:
        content = render_to_string(template, context, RequestContext(request))
    else:
        context['STATIC_URL'] = settings.STATIC_URL
        content = render_to_string(template, context)

    total = qs.count()
    from_value = (page - 1) * on_page + 1
    to_value = from_value + on_page - 1

    if to_value > total:
        to_value = total

    return {
        'content': content,
        'total': total,
        'pages': paginator.num_pages,
        'from': from_value,
        'to': to_value
    }

