# Amara, universalsubtitles.org
#
# Copyright (C) 2015 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

from contextlib import contextmanager
from datetime import datetime, timedelta
from django.test import TestCase
from nose.tools import *

from utils.test_utils import *
from utils.factories import *
from subtitles import pipeline
from videos.models import VideoIndex

class VideoIndexingTest(TestCase):
    def test_create_index_for_new_video(self):
        v = VideoFactory()
        index = VideoIndex.objects.get(video=v)
        assert_true(index.needs_update)
        assert_equal(index.update_lock_time, None)
        assert_equal(index.update_lock_key, '')

    def make_video_index(self, **attrs):
        index = VideoFactory().index
        for name, value in attrs.items():
            setattr(index, name, value)
        index.save()
        return index

    def test_lock_for_indexing(self):
        # needs an update and no one has locked it
        should_index = self.make_video_index(needs_update=True)
        # Doesn't need an update
        shouldnt_index = self.make_video_index(needs_update=False)
        # Needs an update, but another process has locked it
        shouldnt_index2 = self.make_video_index(needs_update=True,
                                            update_lock_key='abc',
                                            update_lock_time=datetime.now())
        # Needs an update, has a lock, but the lock has timed out
        should_index2 = self.make_video_index(
            needs_update=True, update_lock_key='abc',
            update_lock_time=datetime.now() - timedelta(days=2))

        assert_items_equal(VideoIndex.objects.lock_for_indexing(),
                           [should_index.video, should_index2.video])

    def test_lock_for_indexing_with_limit(self):
        for i in xrange(3):
            VideoFactory()
        assert_equal(len(VideoIndex.objects.lock_for_indexing(limit=2)), 2)

    def test_create_missing_index_objects(self):
        v = VideoFactory()
        v.index.delete()
        assert_false(VideoIndex.objects.filter(video=v).exists())
        VideoIndex.objects.create_missing()
        assert_true(VideoIndex.objects.filter(video=v).exists())

    def test_index_text(self):
        video = VideoFactory(title='video_title',
                             description='video_description',
                             video_url__url='http://example.com/url_1')
        VideoURLFactory(video=video, url='http://example.com/url_2')
        pipeline.add_subtitles(video, 'en', [
            (0, 500, 'en_line1'),
            (1000, 1500, 'en_line2'),
        ], title='en_title', description='en_description', metadata={
            'speaker-name': 'en_speaker',
        }, visibility='public')
        pipeline.add_subtitles(video, 'fr', [
            (0, 500, 'fr_line1'),
            (1000, 1500, 'fr_line2'),
        ], title='fr_title', description='fr_description', metadata={
            'speaker-name': 'fr_speaker',
        }, visibility='public')
        # add a private tip, this text should not be included in the index
        pipeline.add_subtitles(video, 'es', [
            (0, 500, 'es_line1'),
            (1000, 1500, 'es_line2'),
        ], title='es_title', description='es_description', metadata={
            'speaker-name': 'es_speaker',
        }, visibility='private')

        VideoIndex.objects.index_videos()
        index_text = VideoIndex.objects.get(video=video).text
        assert_true('video_title' in index_text)
        assert_true('video_description' in index_text)
        assert_true('url_1' in index_text)
        assert_true('url_2' in index_text)
        assert_true('en_title' in index_text)
        assert_true('en_description' in index_text)
        assert_true('en_line1' in index_text)
        assert_true('en_line2' in index_text)
        assert_true('fr_title' in index_text)
        assert_true('fr_description' in index_text)
        assert_true('fr_line1' in index_text)
        assert_true('fr_line2' in index_text)
        assert_false('es_title' in index_text)
        assert_false('es_description' in index_text)
        assert_false('es_line1' in index_text)
        assert_false('es_line2' in index_text)

    def test_release_lock(self):
        video = VideoFactory()
        VideoIndex.objects.index_videos()
        index = VideoIndex.objects.get(video=video)
        assert_false(index.needs_update)
        assert_equal(index.update_lock_key, '')
        assert_equal(index.update_lock_time, None)

    # FIXME we should have searching tests, but we can't since we use sqlite
    # databases for our unittests and it has a different matching syntax then
    # MySQL

class ReindexingTest(TestCase):
    def setUp(self):
        self.video = VideoFactory()

    @contextmanager
    def assert_causes_reindex(self):
        self.video.index.needs_update = False
        self.video.index.save()
        yield
        index = reload_obj(self.video.index)
        assert_true(index.needs_update)

    def test_update_video(self):
        with self.assert_causes_reindex():
            self.video.title = 'new title'
            self.video.save()

    def test_create_version(self):
        with self.assert_causes_reindex():
            pipeline.add_subtitles(self.video, 'en', SubtitleSetFactory(),
                                   visibility='public')

    def test_publish_version(self):
        version = pipeline.add_subtitles(self.video, 'en',
                                         SubtitleSetFactory(),
                                         visibility='private')
        with self.assert_causes_reindex():
            version.publish()

    def test_add_url(self):
        version = pipeline.add_subtitles(self.video, 'en',
                                         SubtitleSetFactory(),
                                         visibility='private')
        with self.assert_causes_reindex():
            VideoURLFactory(video=self.video)
