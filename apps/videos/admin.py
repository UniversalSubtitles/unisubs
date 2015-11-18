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

from django.contrib import admin
from django.core.urlresolvers import reverse
from django import forms
from djcelery.admin import TaskMonitor
from djcelery.models import TaskState

from videos.models import (
    Video, SubtitleLanguage, SubtitleVersion, VideoFeed, VideoMetadata,
    VideoUrl, SubtitleVersionMetadata, Action, Subtitle, VideoTypeUrlPattern
)
from videos.tasks import (
    video_changed_tasks, import_videos_from_feed
)
from videos.types import video_type_choices

class VideoUrlInline(admin.StackedInline):
    model = VideoUrl
    raw_id_fields = ['added_by']
    extra = 0

    def formfield_for_dbfield(self, db_field, **kwargs):
        if db_field.name == 'type':
            return forms.ChoiceField(choices=video_type_choices(),
                                     label=u'Type')
        return super(VideoUrlInline, self).formfield_for_dbfield(
            db_field, **kwargs)

class VideoAdmin(admin.ModelAdmin):
    actions = None
    list_display = ['__unicode__', 'video_thumbnail', 'languages',
                    'languages_count', 'is_subtitled',
                    'primary_audio_language_code']
    search_fields = ['video_id', 'title', 'videourl__url', 'user__username']
    readonly_fields = ['view_count']
    raw_id_fields = ['user', 'moderated_by']
    inlines = [VideoUrlInline]

    def video_thumbnail(self, obj):
        return '<img width="80" height="60" src="%s"/>' % obj.get_small_thumbnail()

    video_thumbnail.allow_tags = True
    video_thumbnail.short_description = 'Thumbnail'

    def languages(self, obj):
        lang_qs = obj.subtitlelanguage_set.all()
        link_tpl = '<a href="%s">%s</a>'
        links = []
        for item in lang_qs:
            url = reverse('admin:videos_subtitlelanguage_change', args=[item.pk])
            links.append(link_tpl % (url, item.language or '[undefined]'))
        return ', '.join(links)

    languages.allow_tags = True

class VideoMetadataAdmin(admin.ModelAdmin):
    list_display = ['video', 'key', 'data']
    list_filter = ['key', 'created', 'modified']
    search_fields = ['video__video_id', 'video__title', 'video__user__username',
                     'data']
    raw_id_fields = ['video']

class VideoFeedAdmin(admin.ModelAdmin):
    def update(modeladmin, request, queryset):
        for feed in queryset:
            import_videos_from_feed.delay(feed.id)
    update.short_description = "Update feeds"

    list_display = ['url', 'created', 'user']
    raw_id_fields = ['user']
    actions = [update]

class VideoTypeUrlPatternAdmin(admin.ModelAdmin):
    fields = ('type', 'url_pattern')
    def formfield_for_dbfield(self, db_field, **kwargs):
        if db_field.name == 'type':
            return forms.ChoiceField(choices=video_type_choices(),
                                     label=u'Type')
        return super(VideoTypeUrlPatternAdmin, self).formfield_for_dbfield(
            db_field, **kwargs)

admin.site.register(Video, VideoAdmin)
admin.site.register(VideoMetadata, VideoMetadataAdmin)
admin.site.register(VideoFeed, VideoFeedAdmin)
admin.site.register(VideoTypeUrlPattern, VideoTypeUrlPatternAdmin)

class TaskStateForm(forms.ModelForm):
    traceback_display = forms.CharField(required=False, label=u'Traceback')

    class Meta:
        model = TaskState


class FixedTaskMonitor(TaskMonitor):
    form = TaskStateForm
    fieldsets = (
        (None, {
            "fields": ("state", "task_id", "name", "args", "kwargs",
                       "eta", "runtime", "worker", "tstamp"),
            "classes": ("extrapretty", ),
        }),
        ("Details", {
            "classes": ("collapse", "extrapretty"),
            "fields": ("result", "traceback_display", "expires"),
        })
    )

    readonly_fields = ("state", "task_id", "name", "args", "kwargs",
                       "eta", "runtime", "worker", "result", "traceback_display",
                       "expires", "tstamp")

    def traceback_display(self, obj):
        return '<pre>%s</pre>' % obj.traceback

    traceback_display.allow_tags = True
    traceback_display.short_description = 'Traceback'


class ActionAdmin(admin.ModelAdmin):
    list_display = ('video', 'new_language', 'user', 'team', 'action_type',
        'created')

    class Meta:
        model = Action

admin.site.unregister(TaskState)
admin.site.register(TaskState, FixedTaskMonitor)
admin.site.register(Action, ActionAdmin)
