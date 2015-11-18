from __future__ import absolute_import

from django.core.urlresolvers import reverse
from django.utils import translation
from django.test import TestCase
from nose.tools import *
import mock

from utils import test_utils
from utils.factories import *
from videos.models import Video
from subtitles import pipeline
from subtitles.models import SubtitleLanguage

class MetadataFieldsTest(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.video = VideoFactory()

    def test_metadata_starts_blank(self):
        version = pipeline.add_subtitles(self.video, 'en', None)
        self.assertEquals(self.video.get_metadata(), {})
        self.assertEquals(version.get_metadata(), {})

    def test_add_metadata_through_version(self):
        metadata = {
            'speaker-name': 'Santa',
            'location': 'North Pole',
        }
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata)
        self.assertEquals(version.get_metadata(),  metadata)
        # the video should still have no metadata set
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertEquals(self.video.get_metadata(), {
            'speaker-name': '',
            'location': '',
        })

    def test_language_get_metadata(self):
        self.video.update_metadata({ 'speaker-name': 'Santa'})
        lang = SubtitleLanguage.objects.create(video=self.video,
                                               language_code='fr')
        # initially, it should return the keys, but not have any content
        self.assertEquals(lang.get_metadata(), {'speaker-name': ''})
        # check that convert_for_display doesn't crash (#815)
        lang.get_metadata().convert_for_display()
        # after versions get created, it stould have the data from the tip
        pipeline.add_subtitles(self.video, 'fr', None,
                               metadata = {'speaker-name': 'French Santa'})
        lang = SubtitleLanguage.objects.get(id=lang.id)
        self.assertEquals(lang.get_metadata(),
                          {'speaker-name': 'French Santa'})
        # by default, it's the public tip, but that can be changed
        pipeline.add_subtitles(self.video, 'fr', None, visibility='private',
                               metadata = {'speaker-name': 'French Santa2'})
        self.assertEquals(lang.get_metadata(),
                          {'speaker-name': 'French Santa'})
        self.assertEquals(lang.get_metadata(public=False),
                          {'speaker-name': 'French Santa2'})

    def test_update_video(self):
        # test that when we set metadata for the primary language, it updates
        # the video's metadata
        self.video.update_metadata({'speaker-name': 'Speaker1'})
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker1'})
        self.video.update_metadata({'speaker-name': 'Speaker2'})
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker2'})

    def test_add_metadata_for_primary_language_updates_video(self):
        # When we set metadata for the primary audio language, we should
        # update the video's metadata
        self.video.update_metadata({'speaker-name': 'Speaker1'})
        self.video.primary_audio_language_code = 'en'
        self.video.save()
        version = pipeline.add_subtitles(
            self.video, 'en', None,
            metadata={'speaker-name': 'Speaker2'})
        self.video = test_utils.reload_obj(self.video)
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker2'})

    def test_add_metadata_for_private_versions_dont_update_video(self):
        # When we set metadata for the primary audio language, but the version
        # is private, we shouldn't update the video's metadata
        TeamVideoFactory(video=self.video)
        self.video.update_metadata({'speaker-name': 'Speaker1'})
        self.video.primary_audio_language_code = 'en'
        self.video.save()
        version = pipeline.add_subtitles(
            self.video, 'en', None, visibility='private',
            metadata={'speaker-name': 'Speaker2'})
        self.video = test_utils.reload_obj(self.video)
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker1'})

    def test_publish_primary_audio_language_updates_video(self):
        # When we publish a version for the primary audio language, we should
        # update the video's metadata
        TeamVideoFactory(video=self.video)
        self.video.update_metadata({'speaker-name': 'Speaker1'})
        self.video = test_utils.reload_obj(self.video)
        self.video.primary_audio_language_code = 'en'
        self.video.save()
        version = pipeline.add_subtitles(
            self.video, 'en', None, visibility='private',
            metadata={'speaker-name': 'Speaker2'})
        version.publish()
        self.video = test_utils.reload_obj(self.video)
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker2'})

    def test_add_metadata_for_other_languages_dont_update_video(self):
        # When we set metadata for a language other than the primary audio
        # language, it shouldn't update the video
        self.video.update_metadata({'speaker-name': 'Speaker1'})
        self.video.primary_audio_language_code = 'en'
        self.video.save()
        version = pipeline.add_subtitles(
            self.video, 'fr', None,
            metadata={'speaker-name': 'Speaker2'})
        self.video = test_utils.reload_obj(self.video)
        self.assertEquals(self.video.get_metadata(),
                          {'speaker-name': 'Speaker1'})

    def test_add_metadata_twice(self):
        metadata_1 = {
            'speaker-name': 'Santa',
            'location': 'North Pole',
        }
        metadata_2 = {
            'speaker-name': 'Santa2',
            'location': 'North Pole2',
        }
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata_1)
        version2 = pipeline.add_subtitles(self.video, 'en', None,
                                          metadata=metadata_2)
        self.assertEquals(version2.get_metadata(),  metadata_2)
        self.assertEquals(version.get_metadata(),  metadata_1)

    def test_languages_without_metadata(self):
        # languages without metadata set shouldn't get the metadata from other
        # languages
        metadata = {
            'speaker-name': 'Santa',
            'location': 'North Pole',
        }
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata)
        version2 = pipeline.add_subtitles(self.video, 'fr', None,
                                          metadata=None)
        self.assertEquals(version2.get_metadata(),  {
            'speaker-name': '',
            'location': '',
        })

    def test_additional_field_in_update(self):
        metadata_1 = { 'speaker-name': 'Santa', }
        metadata_2 = {
            'speaker-name': 'Santa',
            'location': 'North Pole',
        }
        # version 1 only has 1 field
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata_1)
        # version 2 only has 2 fields
        version2 = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata_2)
        # we should add the additional field in the version
        self.assertEquals(version2.get_metadata(),  metadata_2)

    def test_field_missing_in_update(self):
        metadata_1 = {
            'speaker-name': 'Santa',
            'location': 'North Pole',
        }
        metadata_2 = { 'speaker-name': 'Santa', }
        # version 1 only has 2 fields
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata=metadata_1)
        # version 2 only has 1 field
        version2 = pipeline.add_subtitles(self.video, 'en', None,
                                          metadata=metadata_2)
        # version2 should not have data for location
        self.assertEquals(version2.get_metadata(), {
            'speaker-name': 'Santa',
            'location': '',
        })

    def test_metadata_display(self):
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata={
                                             'speaker-name': 'Santa',
                                             'location': 'North Pole',
                                         })
        self.assertEquals(version.get_metadata().convert_for_display(), [
            { 'label': 'Speaker Name', 'content': 'Santa'},
            { 'label': 'Location', 'content': 'North Pole'},
        ])

    def test_metadata_display_is_translated(self):
        version = pipeline.add_subtitles(self.video, 'en', None,
                                         metadata={
                                             'speaker-name': 'Santa',
                                             'location': 'North Pole',
                                         })
        with mock.patch('videos.metadata._') as mock_gettext:
            mock_gettext.return_value = 'Mock Translation'
            metadata = version.get_metadata()
            self.assertEquals(metadata.convert_for_display(), [
                { 'label': 'Mock Translation', 'content': 'Santa'},
                { 'label': 'Mock Translation', 'content': 'North Pole'},
            ])

    def test_metadata_content_empty(self):
        self.video.update_metadata({'speaker-name': ''})
        # get_metadata() should return metadata with the key
        self.assertEquals(self.video.get_metadata(), {'speaker-name': ''})
        # but convert_for_display() should eliminate the value
        self.assertEquals(self.video.get_metadata().convert_for_display(), [])

class MetadataForLocaleTest(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        self.video = VideoFactory()
        self.video.update_metadata({
            'location': 'Place',
        })
        pipeline.add_subtitles(self.video, 'fr', None, metadata={
            'location': 'Place-fr',
        })

    def test_locale_with_metadata(self):
        assert_equal(self.video.get_metadata_for_locale('fr'), {
            'location': 'Place-fr',
        })

    def test_locale_without_metadata(self):
        assert_equal(self.video.get_metadata_for_locale('de'), {
            'location': 'Place',
        })
