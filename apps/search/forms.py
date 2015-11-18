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

from django import forms
from django.db.models import Count
from django.utils.translation import ugettext_lazy as _

from utils.translation import get_language_choices
from videos.models import Video

_sorted_language_choices = None
def sorted_language_choices():
    # query the DB to get the language list.  Save it in a variable and keep
    # the ordering the same.  We'll assume that the language ordering doesn't
    # change between container restarts.
    global _sorted_language_choices
    if _sorted_language_choices is None:
        _sorted_language_choices = _calc_sorted_language_choices()
    return _sorted_language_choices

def _calc_sorted_language_choices():
    qs = (Video.objects.order_by()
          .values_list('primary_audio_language_code')
          .annotate(count=Count('primary_audio_language_code')))
    language_counts = dict(qs)
    choices = get_language_choices()
    choices.sort(key=lambda c: language_counts.get(c[0], 0), reverse=True)
    return [('', _('All Languages'))] + choices

class SearchForm(forms.Form):
    SORT_CHOICES = (
        ('score', _(u'Relevance')),
        ('languages_count', _(u'Most languages')),
        ('today_views', _(u'Views Today')),
        ('week_views', _(u'Views This Week')),
        ('month_views', _(u'Views This Month')),
        ('total_views', _(u'Total Views')),
    )
    q = forms.CharField(label=_(u'query'), required=False)
    langs = forms.ChoiceField(choices=[], required=False, label=_(u'Subtitled Into'),
                              help_text=_(u'Left blank for any language'), initial='')
    video_lang = forms.ChoiceField(choices=[], required=False, label=_(u'Video In'),
                              help_text=_(u'Left blank for any language'), initial='')

    def __init__(self, *args, **kwargs):
        super(SearchForm, self).__init__(*args, **kwargs)

        self.fields['video_lang'].choices = sorted_language_choices()
        self.fields['langs'].choices = sorted_language_choices()

    def queryset(self):
        q = self.data.get('q')
        video_lang = self.data.get('video_lang')
        langs = self.data.get('langs')

        qs = Video.objects.all()
        if q:
            qs = qs.search(q)
        if video_lang:
            qs = qs.filter(primary_audio_language_code=video_lang)
        if langs:
            qs = qs.has_completed_language(langs)

        return qs
