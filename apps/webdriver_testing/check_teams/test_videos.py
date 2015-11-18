#!/usr/bin/python
# -*- coding: utf-8 -*-
import json
import time
import os

from utils.factories import *
from subtitles import pipeline	
from webdriver_testing.webdriver_base import WebdriverTestCase
from webdriver_testing.pages.site_pages.teams import videos_tab
from webdriver_testing.pages.site_pages.teams.tasks_tab import TasksTab
from webdriver_testing.pages.site_pages import watch_page
from webdriver_testing.data_factories import TeamManagerLanguageFactory
from webdriver_testing.data_factories import TeamLangPrefFactory
from webdriver_testing import data_helpers
from testhelpers.views import _create_videos
from django.core import management

class TestCaseSearch(WebdriverTestCase):
    """TestSuite for searching team videos
    """
    NEW_BROWSER_PER_TEST_CASE = False

    @classmethod
    def setUpClass(cls):
        super(TestCaseSearch, cls).setUpClass()
        cls.data_utils = data_helpers.DataHelpers()
        cls.admin = UserFactory()
        cls.manager = UserFactory()
        cls.member = UserFactory()
        cls.team = TeamFactory(admin=cls.admin,
                               manager=cls.manager,
                               member=cls.member)
 
        cls.videos_tab = videos_tab.VideosTab(cls)

        cls.test_video = YouTubeVideoFactory() 
        pipeline.add_subtitles(cls.test_video, 'en', SubtitleSetFactory(),
                               action='publish')
        TeamVideoFactory(
            team=cls.team, 
            video=cls.test_video, 
            added_by=cls.manager)
         
        management.call_command('update_index', interactive=False)


    def test_search_title(self):
        """Team video search for title text.

        """
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search(self.test_video.title)
        self.assertTrue(self.videos_tab.video_present(self.test_video.title))

    def test_search_updated_title(self):
        """Team video search for title text after it has been updated.

        """

        #Update the video title and description (via api)
        url_part = 'videos/%s/' % self.test_video.video_id
        new_data = {'title': 'Please do not glance at my mother.',
                    'description': 'Title update for grammar and politeness.'
                   }

        
        self.data_utils.make_request(self.admin, 'put', 
                                     url_part, **new_data)
        #Update the solr index
        management.call_command('update_index', interactive=False)

        #Open team videos page and search for updated title text.
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search('mother')
        self.assertTrue(self.videos_tab.video_present(new_data['title']))


    def test_search_updated_description(self):
        """Team video search for description text after it has been updated.

        """
        #Update the video title and description (via api)
        url_part = 'videos/%s/' % self.test_video.video_id
        new_data = { 'description': 'description update for grammar and politeness.'
                   }
        self.data_utils.make_request(self.admin, 'put', 
                                     url_part, **new_data)

        #Update the solr index
        management.call_command('update_index', interactive=False)

        #Open team videos page and search for updated title text.
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search('grammar and politeness')
        self.assertTrue(self.videos_tab.video_present(self.test_video.title))


    def test_search_subs(self):
        """Team video search for subtitle text.

        """
        
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search('Sub')
        self.assertTrue(self.videos_tab.video_present(self.test_video.title))

    def test_search_nonascii(self):
        """Team video search for non-ascii char strings.
 
        """
        video = TeamVideoFactory(team=self.team).video
        data = {'language_code': 'zh-cn',
                'video': video,
                'subtitles': ('apps/webdriver_testing/subtitle_data/'
                              'Timed_text.zh-cn.sbv'),
                'action': 'publish',
                'complete': None
           }
        self.data_utils.add_subs(**data)
 
        management.call_command('update_index', interactive=False)
        #Search for: first line 我会试着解释为何: by opening entering the text via javascript
        #because webdriver can't type those characters.
        self.videos_tab.open_videos_tab(self.team.slug)
        self.browser.execute_script("document.getElementsByName"
                                    "('q')[0].value='我会试着解释为何'")
        self.videos_tab.search('')
        self.assertTrue(self.videos_tab.video_present(video.title))

    def test_search_no_results(self):
        """Team video search returns no results.

        """
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search('TextThatShouldNOTExistOnTheVideoOrSubs')
        self.assertEqual(self.videos_tab.NO_VIDEOS_TEXT, 
                         self.videos_tab.search_no_result())

class TestCaseFilterSort(WebdriverTestCase):
    """TestSuite for searching team videos
    """
    NEW_BROWSER_PER_TEST_CASE = False

    @classmethod
    def setUpClass(cls):
        super(TestCaseFilterSort, cls).setUpClass()

        cls.data_utils = data_helpers.DataHelpers()
        cls.videos_tab = videos_tab.VideosTab(cls)

        cls.admin = UserFactory()
        cls.manager = UserFactory()
        cls.member = UserFactory()
        cls.team = TeamFactory(admin=cls.admin,
                               manager=cls.manager,
                               member=cls.member)


        cls.video = TeamVideoFactory(team=cls.team).video
        data = {'language_code': 'en',
                'video':cls.video,
                'subtitles': ('apps/webdriver_testing/subtitle_data/'
                              'Open Source Philosophy.en.dfxp'),
                'action': 'publish',
           }
        cls.data_utils.add_subs(**data)
        management.call_command('update_index', interactive=False)

    def test_filter_clear(self):
        """Clear filters.

        """
        self.videos_tab.open_videos_tab(self.team.slug)

        #Filter so that no videos are present
        self.videos_tab.sub_lang_filter(language = 'French')
        self.videos_tab.update_filters()
        self.assertEqual(self.videos_tab.NO_VIDEOS_TEXT, 
            self.videos_tab.search_no_result())

        #Clear filters and verify videos are displayed.
        self.videos_tab.clear_filters()
        self.assertTrue(self.videos_tab.video_present(self.video.title))


    def test_filter_languages(self):
        """Filter team videos by language.

        """

        video = TeamVideoFactory(team=self.team).video
        data = {'language_code': 'da',
                'video': video,
                'subtitles': ('apps/webdriver_testing/subtitle_data/'
                              'Open Source Philosophy.da.dfxp'),
                'action': 'publish',
           }
        self.data_utils.add_subs(**data)
        management.call_command('update_index', interactive=False)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.sub_lang_filter(language = 'Danish')
        self.videos_tab.update_filters()

        self.assertTrue(self.videos_tab.video_present(video.title))

    def test_filter_missing_languages(self):
        """Filter team videos by language with no subtitles.

        """
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.sub_lang_filter(language = 'Portuguese', has=False)
        self.videos_tab.update_filters()

        self.assertTrue(self.videos_tab.video_present(self.video.title))

    def test_filter_no_incomplete(self):
        """Filter by language, incomplete subs are not in results. 

        """
        video = TeamVideoFactory(team=self.team).video
        data = {'language_code': 'nl',
                'video': video,
                'subtitles': ('apps/webdriver_testing/subtitle_data/'
                              'Open Source Philosophy.nl.dfxp'),
                'complete': False,
           }
        self.data_utils.add_subs(**data)
 
        management.call_command('update_index', interactive=False)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.sub_lang_filter(language = ['Dutch'])
        self.videos_tab.update_filters()

        self.assertEqual(self.videos_tab.NO_VIDEOS_TEXT, 
            self.videos_tab.search_no_result())


    def test_sort_name_ztoa(self):
        """Sort videos on team page reverse alphabet.

        """
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.video_sort(sort_option = 'name, z-a')
        self.videos_tab.update_filters()

        self.videos_tab.videos_displayed()
        self.assertEqual(self.videos_tab.first_video_listed(), 
            self.video.title)

    def test_sort_time_oldest(self):
        """Sort videos on team page by oldest.

        """
        video = TeamVideoFactory(team=self.team).video
        management.call_command('update_index', interactive=False)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.video_sort(sort_option = 'time, oldest')
        self.videos_tab.update_filters()

        self.videos_tab.videos_displayed()
        self.assertEqual(self.videos_tab.first_video_listed(), 
                         self.video.title)


class TestCaseProjectsAddEdit(WebdriverTestCase):
    NEW_BROWSER_PER_TEST_CASE = False

    @classmethod
    def setUpClass(cls):
        super(TestCaseProjectsAddEdit, cls).setUpClass()
        cls.data_utils = data_helpers.DataHelpers()
        cls.videos_tab = videos_tab.VideosTab(cls)
        cls.admin = UserFactory()
        cls.manager = UserFactory()
        cls.member = UserFactory()
        cls.team = TeamFactory(admin=cls.admin,
                               manager=cls.manager,
                               member=cls.member)

        cls.project1 = ProjectFactory(team=cls.team)
        cls.project2 = ProjectFactory(team=cls.team)

        cls.project1_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project1.slug))
        cls.project2_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project2.slug))
        test_videos = ['jaws.mp4', 'Birds_short.oggtheora.ogg', 'fireplace.mp4']
        cls.videos_list = []
        for vid in test_videos:
            video_url = 'http://qa.pculture.org/amara_tests/%s' % vid[0]
            tv = VideoFactory(video_url=video_url,
                              title=vid)
            TeamVideoFactory(video = tv, 
                                 team = cls.team,
                                 project = cls.project2)
            cls.videos_list.append(tv)
        management.call_command('update_index', interactive=False)
        cls.videos_tab.open_videos_tab(cls.team)
        cls.videos_tab.log_in(cls.admin.username, 'password')


    def test_add_new(self):
        """Submit a new video for the team and assign to a project.

        """
        test_url='http://www.youtube.com/watch?v=i_0DXxNeaQ0'
        project_page = 'teams/{0}/videos/?project={1}'.format(self.team.slug, 
            self.project2.slug)
        self.videos_tab.open_page(project_page)
        self.videos_tab.add_video(url=test_url, project=self.project2.name)
        self.videos_tab.open_page(project_page)
        self.assertTrue(self.videos_tab.video_present('test-title'))

    def test_search_simple(self):
        """Peform a basic search on the project page for videos.

        """
        tv = self.videos_list[0]
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.search(tv.title)
        self.videos_tab.project_filter(project=self.project2.name)
        self.videos_tab.update_filters()
        self.assertTrue(self.videos_tab.video_present(tv.title))


    def test_remove(self):
        """Remove a video from the team project.

        """
        tv = self.videos_list[1]

        self.videos_tab.open_page(self.project2_page)
        self.videos_tab.remove_video(video = tv.title)
        self.videos_tab.search(tv.title)
        self.assertEqual(self.videos_tab.NO_VIDEOS_TEXT, 
            self.videos_tab.search_no_result())


    def test_edit_change_project(self):
        """Move a video from project2 to project 1.

        """
        tv = self.videos_list[2]
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.open_page(self.project2_page)
        self.videos_tab.edit_video(video=tv.title,
                                   project = self.project1.name)
        management.call_command('update_index', interactive=False)

        self.videos_tab.open_page(self.project1_page)
        self.assertTrue(self.videos_tab.video_present(tv.title))


class TestCaseProjectsFilter(WebdriverTestCase):
    NEW_BROWSER_PER_TEST_CASE = False

    @classmethod
    def setUpClass(cls):
        super(TestCaseProjectsFilter, cls).setUpClass()
        management.call_command('flush', interactive=False)
        cls.data_utils = data_helpers.DataHelpers()
        cls.videos_tab = videos_tab.VideosTab(cls)
        cls.admin = UserFactory()
        cls.manager = UserFactory()
        cls.member = UserFactory()
        cls.team = TeamFactory(admin=cls.admin,
                               manager=cls.manager,
                               member=cls.member,
                               workflow_enabled=True)

        cls.project1 = ProjectFactory(team=cls.team)
        cls.project2 = ProjectFactory(team=cls.team)

        cls.project1_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project1.slug))
        cls.project2_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project2.slug))
        management.call_command('update_index', interactive=False)


    def setUp(self):
        self.videos_tab.open_videos_tab(self.team.slug)


    def test_filter_projects(self):
        """Filter video view by project.

        """
        
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.project_filter(project=self.project1.name)
        self.videos_tab.update_filters()
        time.sleep(3)
        self.assertEqual(self.videos_tab.NO_VIDEOS_TEXT, 
            self.videos_tab.search_no_result())


    def test_filter_languages(self):
        """Filter on the project page by language.

        """
        video = TeamVideoFactory(team=self.team, project=self.project2).video
        data = {'language_code': 'en',
                'video': video,
                'subtitles': ('apps/webdriver_testing/subtitle_data/'
                              'Open Source Philosophy.en.dfxp'),
                'complete': True,
           }
        self.data_utils.add_subs(**data)
 
        management.call_command('update_index', interactive=False)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.project_filter(project=self.project2.name)
        self.videos_tab.sub_lang_filter(language = 'English')
        self.videos_tab.update_filters()
        self.assertTrue(self.videos_tab.video_present(video.title))

    def test_sort_most_subtitles(self):
        """Sort on the project page by most subtitles.

        """
        data2 = json.load(open(
            'apps/webdriver_testing/check_teams/lots_of_subtitles.json'))
        videos2 = _create_videos(data2, [])
        for video in videos2:
            TeamVideoFactory.create(
                team=self.team, 
                video=video, 
                added_by=self.admin,
                project = self.project2)
        management.call_command('update_index', interactive=False)
        self.videos_tab.open_page(self.project2_page)
        self.videos_tab.video_sort(sort_option = 'most subtitles')
        self.videos_tab.update_filters()
        self.videos_tab.videos_displayed()
        self.assertEqual(self.videos_tab.first_video_listed(), 
            'lots of translations')


class TestCaseVideosDisplay(WebdriverTestCase):
    """Check actions on team videos tab that are specific to user roles.

    """
    NEW_BROWSER_PER_TEST_CASE = False

    @classmethod
    def setUpClass(cls):
        super(TestCaseVideosDisplay, cls).setUpClass()
        #management.call_command('flush', interactive=False)
        cls.data_utils = data_helpers.DataHelpers()
        cls.videos_tab = videos_tab.VideosTab(cls)
        cls.tasks_tab = TasksTab(cls)

        cls.logger.info('Creating team limited access: workflows enabled, '
                         'video policy set to manager and admin, '
                         'task assign policy set to manager and admin, '
                         'membership policy open.')

        cls.admin = UserFactory()
        cls.manager = UserFactory()
        cls.member = UserFactory()
        cls.team = TeamFactory(admin=cls.admin,
                               manager=cls.manager,
                               member=cls.member,
                               workflow_enabled=True,
                               membership_policy = 4, #open
                               video_policy = 2, #manager and admin
                               task_assign_policy = 20, #manager and admin
                               )


        cls.en_manager = TeamMemberFactory(role="ROLE_MANAGER",
                                           team = cls.team,
                                           user = UserFactory())
        TeamManagerLanguageFactory(member = cls.en_manager,
                                   language = 'en')

        cls.project1 = ProjectFactory(team=cls.team)
        cls.project2 = ProjectFactory(team=cls.team)

        cls.project1_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project1.slug))
        cls.project2_page = ('teams/{0}/videos/?project={1}'
                        .format(cls.team.slug, cls.project2.slug))
        management.call_command('update_index', interactive=False)
        cls.videos_tab.open_videos_tab(cls.team.slug)
        cls.videos_tab.log_in(cls.admin.username, 'password')


    def turn_on_automatic_tasks(self):
        self.logger.info('Turning on automatic task creation')
        #Turn on task autocreation for the team.
        WorkflowFactory.create(
            team = self.team,
            autocreate_subtitle = True,
            autocreate_translate = True,
            review_allowed = 10)

        #Add some preferred languages to the team.
        lang_list = ['en', 'ru', 'pt-br']
        for language in lang_list:
            TeamLangPrefFactory(
                team = self.team,
                language_code = language,
                preferred = True)

    def test_contributor_no_edit(self):
        """Video policy: manager and admin; contributor sees no edit link.

        """
 
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.logger.info('Adding user contributor to the team and logging in')
        #Create a user who is a contributor to the team.
        self.videos_tab.log_in(self.member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Edit'))

    def test_contributor_edit_permission(self):
        """Video policy: all team members; contributor sees the edit link.

        """
        self.logger.info('setup: Setting video policy to all team members')
        self.team.video_policy=1
        self.team.save()
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        #Create a user who is a contributor to the team.
        self.videos_tab.log_in(self.member.username, 'password')

        self.logger.info('Setting video policy to all team members')
        self.team.video_policy=1 
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Edit'))

    def test_contributor_no_tasks(self):
        """Task policy: manager and admin; contributor sees no task link.

        """

        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.logger.info('Adding user contributor to the team and logging in')
        #Create a user who is a contributor to the team.
        self.videos_tab.log_in(self.member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 
                         'Tasks'))

    def test_contributor_tasks_permission(self):
        """Task policy: all team members; contributor sees the task link.

        """
        self.logger.info('setup: Setting task policy to all team members')
        self.team.task_assign_policy=10
        self.team.save()


        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))


    def test_manager_no_edit(self):
        """Video policy: admin only; manager sees no edit link.

        """
        self.logger.info('setup: Setting task policy to admin only')
        self.team.video_policy=3
        self.team.save()

        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.manager.username, 'password')

        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Edit'))

    def test_manager_edit_permission(self):
        """Video policy: manager and admin; manager sees the edit link.

        """
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.manager.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Edit'))

    def test_manager_no_tasks(self):
        """Task policy: admin only; manager sees no task link.

        """
        self.browser.delete_all_cookies()
        self.logger.info('setup: Setting task policy to admin only')
        self.team.task_assign_policy=30
        self.team.save()
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.manager.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_manager_tasks_permission(self):
        """Task policy: manager and admin; manager sees the task link.

        """

        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.manager.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_restricted_manager_no_tasks(self):
        """Task policy: manager and admin; language manager sees no task link.

        """
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.en_manager.user.username, 'password')

        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_restricted_manager_tasks(self):
        """Task policy: all team members; language manager sees task link.

        """
        self.logger.info('setup: Setting task policy to all team members')
        self.team.task_assign_policy=10
        self.team.save()

        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.en_manager.user.username, 'password')

        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_restricted_manager_no_edit(self):
        """Video policy: manager and admin; language manager sees no edit link.

        """
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.en_manager.user.username, 'password')

        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Edit'))


    def test_restricted_manager_edit(self):
        """Video policy: all team members; language manager sees edit link.

        """
        self.logger.info('setup: Setting video policy to all team members')
        self.team.video_policy=1
        self.team.save()
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.en_manager.user.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Edit'))



    def test_nonmember_no_tasks(self):
        """Task policy: all team members; non-member sees no tasks. 

        """
        self.logger.info('setup: Setting task policy to all team members')
        self.team.task_assign_policy=10
        self.team.save()
        
        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        #Create a user who is not a member 
        non_member = UserFactory()
        self.videos_tab.log_in(non_member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_nonmember_no_edit(self):
        """Video policy: all team members; non-memeber sees no edit.

        """
        self.logger.info('setup: Setting video policy to all team members')
        self.team.video_policy=1
        self.team.save()
        
        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        #Create a user who is not a member 
        non_member = UserFactory()
        self.videos_tab.log_in(non_member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Edit'))


    def test_guest_no_tasks(self):
        """Task policy: all team members; guest sees no tasks. 

        """
        self.videos_tab.log_out()
        self.logger.info('setup: Setting task policy to all team members')
        self.team.task_assign_policy=10
        self.team.save()
        
        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_guest_no_edit(self):
        """Video policy: all team members; guest sees no edit.

        """
        self.videos_tab.log_out()
        self.logger.info('setup: Setting video policy to all team members')
        self.team.video_policy=1
        self.team.save()
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertFalse(self.videos_tab.video_has_link(vids[0].title, 'Edit'))


    def test_admin_edit_link(self):
        """Video policy: admin only; admin sees edit link.

        """
        self.logger.info('setup: Setting video policy to admin only')
        self.team.video_policy=3
        self.team.save()
        vids = self.data_utils.create_videos_with_subs(team=self.team)

        #Create a user who is a team admin.
        self.logger.info('Create a team admin and log in as that user')
        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Edit'))

    def test_admin_task_link(self):
        """Task policy: admin only; admin sees task link.

        """
        self.logger.info('setup: Setting task policy to admin only')
        self.team.task_assign_policy=30
        self.team.save()

        vids = self.data_utils.create_videos_with_subs(team=self.team)

        #Create a user who is a team admin.
        self.logger.info('Create a team admin and log in as that user')
        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertTrue(self.videos_tab.video_has_link(vids[0].title, 'Tasks'))

    def test_task_link(self):
        """A task link opens the task page for the video.
        """
        self.videos_tab.log_out()
        #Turn on task autocreation for the team.
        self.turn_on_automatic_tasks()

        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        test_title = vids[0].title
        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab.open_video_tasks(test_title)

        self.assertEqual(test_title, self.tasks_tab.filtered_video())

    def test_languages_no_tasks(self):
        """Without tasks, display number of completed languages for video.

        """
        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)

        self.assertEqual('1 language', 
            self.videos_tab.displayed_languages(vids[0].title))

    def test_languages_automatic_tasks(self):
        """With automatic tasks, display number of needed languages for video.

        """
        self.turn_on_automatic_tasks()

        #Add some test videos to the team.
        vids = self.data_utils.create_videos_with_subs(team=self.team)
        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)

        self.assertEqual('2 languages needed', 
            self.videos_tab.displayed_languages(vids[0].title))

    def test_pagination_admin(self):
        """Check number of videos displayed per page for team admin.

        """
        for x in range(50):
            TeamVideoFactory.create(
                team=self.team, 
                video=VideoFactory.create(), 
                added_by=self.admin)
        management.call_command('update_index', interactive=False)

        self.videos_tab.log_in(self.admin.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.assertEqual(8, self.videos_tab.num_videos())

    def test_pagination_user(self):
        """Check number of videos displayed per page for team contributor.

        """
        for x in range(50):
            TeamVideoFactory.create(
                team=self.team, 
                video=VideoFactory.create(), 
                added_by=self.admin)
        management.call_command('update_index', interactive=False)

        self.videos_tab.log_in(self.member.username, 'password')
        self.videos_tab.open_videos_tab(self.team.slug)
        self.videos_tab._open_filters()
        self.assertEqual(16, self.videos_tab.num_videos())

