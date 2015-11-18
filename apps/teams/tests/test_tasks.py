from __future__ import absolute_import

import datetime
import json

from django.test import TestCase
from django.test.client import Client
from django.core.urlresolvers import reverse

from auth.models import CustomUser as User
from caching.tests.utils import assert_invalidates_model_cache
from teams.forms import TaskCreateForm, TaskAssignForm
from teams.models import Task, Team, TeamVideo, TeamMember
from utils.testeditor import TestEditor
from utils.factories import *
from videos.models import Video

# review setting constants
DONT_REQUIRE_REVIEW = 0
PEER_MUST_REVIEW = 10
MANAGER_MUST_REVIEW = 20
ADMIN_MUST_REVIEW = 30
# approval setting contants
DONT_REQUIRE_APPROVAL = 0
MANAGER_MUST_APPROVE = 10
ADMIN_MUST_APPROVE = 20
# task type constants
TYPE_SUBTITLE = 10
TYPE_TRANSLATE = 20
TYPE_REVIEW = 30
TYPE_APPROVE = 40

class AutoCreateTest(TestCase):
    def setUp(self):
        self.team = TeamFactory(workflow_enabled=True)
        w = WorkflowFactory(team=self.team, autocreate_subtitle=True,
                            autocreate_translate=True)
        self.admin = TeamMemberFactory(team=self.team,
                                       role=TeamMember.ROLE_ADMIN)

    def test_no_audio_language(self):
        video = VideoFactory()
        tv = TeamVideoFactory(team=self.team, video=video,
                              added_by=self.admin.user)
        tasks = tv.task_set.all()
        self.assertEqual(tasks.count() , 1)
        transcribe_task = tasks.filter(type=10, language='')
        self.assertEqual(transcribe_task.count(), 1)

    def test_audio_language(self):
        # create the original language for this video
        video = VideoFactory(primary_audio_language_code='en')
        tv = TeamVideoFactory(team=self.team, video=video,
                              added_by=self.admin.user)
        tasks = tv.task_set.all()
        self.assertEqual(tasks.count() , 1)
        transcribe_task = tasks.filter(type=10, language='en')
        self.assertEqual(transcribe_task.count(), 1)

class TranslateTranscribeTestBase(TestCase):
    """Base class for TranscriptionTaskTest and TranslationTaskTest."""
    def setUp(self):
        self.team = TeamFactory(workflow_enabled=True)
        self.workflow = WorkflowFactory(
            team=self.team,
            review_allowed=DONT_REQUIRE_REVIEW,
            approve_allowed=DONT_REQUIRE_APPROVAL)
        self.admin = TeamMemberFactory(team=self.team,
                                       role=TeamMember.ROLE_ADMIN)
        self.team_video = TeamVideoFactory(
            team=self.team, added_by=self.admin.user,
            video__primary_audio_language_code='en')
        self.client = Client()
        self.login(self.admin.user)


    def login(self, user):
        self.client.login(username=user.username, password="password")

    def get_subtitle_task(self):
        tasks = list(self.team_video.task_set.all_subtitle().all())
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def get_translate_task(self):
        tasks = list(self.team_video.task_set.all_translate().all())
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def get_review_task(self):
        tasks = list(self.team_video.task_set.all_review().all())
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def get_approve_task(self):
        tasks = list(self.team_video.task_set.all_approve().all())
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def get_incomplete_task(self):
        tasks = list(self.team_video.task_set.incomplete().all())
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def check_incomplete_counts(self, subtitle_count, review_count,
                                approve_count):
        self.assertEquals(
            self.team_video.task_set.incomplete_subtitle().count(),
            subtitle_count)
        self.assertEquals(
            self.team_video.task_set.incomplete_review().count(),
            review_count)
        self.assertEquals(
            self.team_video.task_set.incomplete_approve().count(),
            approve_count)

    def check_tip_is_public(self, should_be_public, language_code=None):
        self.team_video.video.clear_language_cache()
        lang = self.team_video.video.subtitle_language(language_code)
        self.assertEquals(lang.get_tip().is_public(), should_be_public)

    def delete_tasks(self):
        self.team_video.task_set.all().delete()

    def submit_assign(self, member, task, ajax=False):
        if ajax:
            url_name = 'teams:assign_task_ajax'
        else:
            url_name = 'teams:assign_task'
        url = reverse(url_name, kwargs={'slug': self.team.slug})
        post_data = {'task': task.pk, 'assignee': member.user.pk}
        response = self.client.post(url, post_data)
        if ('form' in response.context and
            not response.context['form'].is_valid()):
            raise AssertionError("submit failed -- errors:\n%s" %
                                 response.context['form'].errors.as_text)
        return response

    def submit_create_task(self, type, assignee='', language='',
                           expecting_error=False):
        url = reverse('teams:create_task',
                             kwargs={'slug': self.team.slug,
                                     'team_video_pk': self.team_video.pk})
        post_data = {'type': type, 'language': language, 'assignee': assignee}
        response = self.client.post(url, post_data)
        # This isn't the best way to check, but if the form was had an error,
        # than the status code will be 200, since we don't redirect in that
        # case.
        form_had_error = (response.status_code == 200)
        if not expecting_error and form_had_error:
            form = response.context['form']
            raise AssertionError("submit to %s failed -- errors:\n%s" %
                                 (url, form.errors.as_text()))
        elif expecting_error and not form_had_error:
            raise AssertionError("submit to %s succeeded" % url)

    def perform_subtitle_task(self, task, base_language_code=None,
                              language_code='en'):
        """Perform a subtitling task.

        :param task: Task object we are working on.
        :param base_language_code: for translations, the language that this is
        translated from.
        :param language_code: the language that the subtitles are in.
        """
        editor = TestEditor(self.client, self.team_video.video,
                            base_language_code=base_language_code)
        # We don't send task info for transcription tasks.
        editor.run(language_code=language_code)

    def perform_review_task(self, task, notes, base_language_code=None,
                            language_code='en',
                            approval=Task.APPROVED_IDS['Approved']):
        editor = TestEditor(self.client, self.team_video.video, mode="review",
                            base_language_code=base_language_code)
        editor.set_task_data(task, approval, notes)
        editor.run(language_code=language_code)

    def perform_approve_task(self, task, notes, base_language_code=None,
                            language_code='en',
                             approval=Task.APPROVED_IDS['Approved']):
        editor = TestEditor(self.client, self.team_video.video,
                            mode="approve",
                            base_language_code=base_language_code)
        editor.set_task_data(task, approval, notes)
        editor.run(language_code=language_code)

    def change_workflow_settings(self, review_allowed, approve_allowed):
        self.workflow.review_allowed = review_allowed
        self.workflow.approve_allowed = approve_allowed
        self.workflow.save()

    def create_member(self):
        return TeamMemberFactory(team=self.team,
                                 role=TeamMember.ROLE_CONTRIBUTOR)


class TranscriptionTaskTest(TranslateTranscribeTestBase):
    """Tests for transcription tasks."""

    def test_create(self):
        self.submit_create_task(TYPE_SUBTITLE)
        task = self.get_subtitle_task()
        self.assertEqual(task.type, TYPE_SUBTITLE)
        self.assertEqual(task.language, '')
        self.assertEqual(task.assignee, None)

    def test_create_with_asignee(self):
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, assignee=member.user.pk)
        task = self.get_subtitle_task()
        self.assertEqual(task.assignee, member.user)

    def test_assign_with_form(self):
        # submit the task
        self.submit_create_task(TYPE_SUBTITLE)
        task = self.get_subtitle_task()
        # create a member and assign the task to them
        member = self.create_member()
        self.submit_assign(member, task)
        # check that it worked
        task = self.get_subtitle_task()
        self.assertEquals(task.assignee, member.user)

    def test_assign_with_ajax(self):
        # submit the task
        self.submit_create_task(TYPE_SUBTITLE)
        task = self.get_subtitle_task()
        # create a member and assign the task to them
        member = self.create_member()
        response = self.submit_assign(member, task, ajax=True)
        # check that it worked
        response_data = json.loads(response.content)
        self.assertEquals(response_data.get('success'), True)
        task = self.get_subtitle_task()
        self.assertEquals(task.assignee, member.user)

    def test_perform(self):
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        task = Task.objects.get(pk=task.pk)
        self.assertNotEquals(task.completed, None)
        self.assertEquals(task.approved, None)
        self.check_incomplete_counts(0, 0, 0)

    def test_review(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      DONT_REQUIRE_APPROVAL)
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the review is ready to go
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False)
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Note")
        # The review is now complete, check aftermath
        self.assertEquals(self.get_review_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True)

    def test_review_send_back(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      DONT_REQUIRE_APPROVAL)
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the review is ready to go
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False)
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Note",
                                 approval=Task.APPROVED_IDS['Rejected'])
        # The transcription was sent back, we should now have a transcription
        # task to complete that's assigned to the original transcriber
        self.check_incomplete_counts(1, 0, 0)
        task = self.get_incomplete_task()
        self.assertEquals(task.assignee, member.user)
        self.check_tip_is_public(False)

    def test_approve(self):
        self.change_workflow_settings(DONT_REQUIRE_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the approval task is ready to go
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False)
        # perform the approval
        approve_task = self.get_approve_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note")
        # The approve is now complete, check aftermath
        self.assertEquals(self.get_approve_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True)

    def test_approve_send_back(self):
        self.change_workflow_settings(DONT_REQUIRE_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the approval task is ready to go
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False)
        # perform the approval
        approve_task = self.get_approve_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note",
                                 approval=Task.APPROVED_IDS['Rejected'])
        # The transcription was sent back, we should now have a transcription
        # task to complete that's assigned to the original transcriber
        self.check_incomplete_counts(1, 0, 0)
        task = self.get_incomplete_task()
        self.assertEquals(task.assignee, member.user)
        self.check_tip_is_public(False)

    def test_review_and_approve(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the review task is next
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False)
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Review Note")
        # check that that worked
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False)
        self.assertEquals(self.get_review_task().body, "Test Review Note")
        # perform the approval
        approve_task = self.get_approve_task()
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note")
        # The approve is now complete, check aftermath
        self.assertEquals(self.get_approve_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True)

    def test_review_and_send_back_approve(self):
        admin2 = TeamMemberFactory(team=self.team, role=TeamMember.ROLE_ADMIN)
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # test test that the review task is next
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False)
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Review Note")
        # check that that worked
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False)
        self.assertEquals(self.get_review_task().body, "Test Review Note")
        # send the task back during approval
        approve_task = self.get_approve_task()
        self.login(admin2.user)
        self.submit_assign(admin2, approve_task)
        self.perform_approve_task(approve_task, "Test Note",
                                  approval=Task.APPROVED_IDS['Rejected'])
        # The review task should be assigned to the original reviewer
        self.check_tip_is_public(False)
        self.login(self.admin.user)
        review_task = self.team_video.task_set.incomplete_review().get()
        self.assertEquals(review_task.assignee, self.admin.user)
        # accept the review task again, the approve task should be assigned to
        # the original approver
        self.perform_review_task(review_task, "Test Review Note")
        self.check_tip_is_public(False)
        approve_task = self.team_video.task_set.incomplete_approve().get()
        self.assertEquals(approve_task.assignee, admin2.user)

    def test_review_and_approve_with_old_version(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        # make an old SubtitleVersion object.  The fact that most of this data
        # is bogus is fine, we shouldn't be using the object at all.
        #
        # We will move through the task pipeline and set the subtitle_version
        # attribute on each task.  The point is to simple check that we can
        # move through without any exceptions.
        old_language = OldSubtitleLanguageFactory(video=self.team_video.video,
                                                  language='en')
        old_version = OldSubtitleVersionFactory(language=old_language,
                                                user=member.user)

        # create the task
        self.submit_create_task(TYPE_SUBTITLE, member.user.pk)
        task = self.get_subtitle_task()
        task.subtitle_version = old_version
        task.save()
        self.login(member.user)
        self.perform_subtitle_task(task)
        # perform the review
        review_task = self.get_review_task()
        review_task.subtitle_version = old_version
        review_task.save()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Review Note")
        # perform the approval
        approve_task = self.get_approve_task()
        approve_task.subtitle_version = old_version
        approve_task.save()
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note",
                                 approval=Task.APPROVED_IDS['Rejected'])

    def test_due_date(self):
        self.team.task_expiration = 2
        self.team.save()
        # submit the task.  It shouldn't have an expiration date before it
        # gets assigned
        self.submit_create_task(TYPE_SUBTITLE)
        task = self.get_subtitle_task()
        self.assertEquals(task.expiration_date, None)
        # create a member and assign the task to them.  After thi, the
        # expiration date should be set.
        member = self.create_member()
        self.submit_assign(member, task)
        approx_expiration = (datetime.datetime.now() +
                             datetime.timedelta(days=2))
        expiration_date = self.get_subtitle_task().expiration_date
        self.assert_(approx_expiration - datetime.timedelta(seconds=1) <
                     expiration_date)
        self.assert_(approx_expiration + datetime.timedelta(seconds=1) >
                     expiration_date)

class TranslationTaskTest(TranslateTranscribeTestBase):
    """Tests for translation tasks."""

    def setUp(self):
        TranslateTranscribeTestBase.setUp(self)
        # make a transcription that we can use for our translations
        editor = TestEditor(self.client, self.team_video.video)
        editor.run(language_code='en')

    def test_create(self):
        self.submit_create_task(TYPE_TRANSLATE, language='ru')
        task = self.get_translate_task()
        self.assertEqual(task.type, TYPE_TRANSLATE)
        self.assertEqual(task.language, 'ru')
        self.assertEqual(task.assignee, None)

    def test_create_with_asignee(self):
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, assignee=member.user.pk,
                                language='ru')
        task = self.get_translate_task()
        self.assertEqual(task.type, TYPE_TRANSLATE)
        self.assertEqual(task.language, 'ru')
        self.assertEqual(task.assignee, member.user)

    def test_assign_with_form(self):
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru')
        task = self.get_translate_task()
        self.assertEquals(task.assignee, None)
        self.submit_assign(member, task)
        self.assertEqual(self.get_translate_task().assignee, member.user)

    def test_assign_with_ajax(self):
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru')
        task = self.get_translate_task()
        self.assertEquals(task.assignee, None)
        self.submit_assign(member, task, ajax=True)
        self.assertEqual(self.get_translate_task().assignee, member.user)

    def test_perform(self):
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru',
                                assignee=member.user.pk)
        task = self.get_translate_task()
        self.login(member.user)
        self.perform_subtitle_task(task, 'en', 'ru')
        task = self.get_translate_task()
        self.assertNotEquals(task.completed, None)
        self.assertEquals(task.approved, None)
        self.check_incomplete_counts(0, 0, 0)

    def test_review(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      DONT_REQUIRE_APPROVAL)
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru',
                                assignee=member.user.pk)
        task = self.get_translate_task()
        self.login(member.user)
        self.perform_subtitle_task(task, 'en', 'ru')
        # test test that the review is ready to go
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False, 'ru')
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Note", 'en', 'ru')
        # The review is now complete, check aftermath
        self.assertEquals(self.get_review_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True, 'ru')

    def test_approve(self):
        self.change_workflow_settings(DONT_REQUIRE_REVIEW,
                                      ADMIN_MUST_APPROVE)
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru',
                                assignee=member.user.pk)
        task = self.get_translate_task()
        self.login(member.user)
        self.perform_subtitle_task(task, 'en', 'ru')
        # test test that the review is ready to go
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False, 'ru')
        # perform the approve
        approve_task = self.get_approve_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note", 'en', 'ru')
        # The approve is now complete, check aftermath
        self.assertEquals(self.get_approve_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True, 'ru')

    def test_review_and_approve(self):
        self.change_workflow_settings(ADMIN_MUST_REVIEW,
                                      ADMIN_MUST_APPROVE)
        self.workflow.save()
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru',
                                assignee=member.user.pk)
        task = self.get_translate_task()
        self.login(member.user)
        self.perform_subtitle_task(task, 'en', 'ru')
        # test test that the review is ready to go
        self.check_incomplete_counts(0, 1, 0)
        self.check_tip_is_public(False, 'ru')
        # perform the review
        review_task = self.get_review_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, review_task)
        self.perform_review_task(review_task, "Test Note", 'en', 'ru')
        # The review is now complete, next step is approval
        self.assertEquals(self.get_review_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 1)
        self.check_tip_is_public(False, 'ru')
        # perform the approve
        approve_task = self.get_approve_task()
        self.login(self.admin.user)
        self.submit_assign(self.admin, approve_task)
        self.perform_approve_task(approve_task, "Test Note", 'en', 'ru')
        # The approve is now complete, check aftermath
        self.assertEquals(self.get_approve_task().body, "Test Note")
        self.check_incomplete_counts(0, 0, 0)
        self.check_tip_is_public(True)

    def test_due_date(self):
        self.team.task_expiration = 2
        self.team.save()
        # submit the task.  It shouldn't have an expiration date before it
        # gets assigned
        member = self.create_member()
        self.submit_create_task(TYPE_TRANSLATE, language='ru')
        task = self.get_translate_task()
        self.assertEquals(task.expiration_date, None)
        # create a member and assign the task to them.  After thi, the
        # expiration date should be set.
        member = self.create_member()
        self.submit_assign(member, task)
        approx_expiration = (datetime.datetime.now() +
                             datetime.timedelta(days=2))
        expiration_date = self.get_translate_task().expiration_date
        self.assert_(approx_expiration - datetime.timedelta(seconds=1) <
                     expiration_date)
        self.assert_(approx_expiration + datetime.timedelta(seconds=1) >
                     expiration_date)

class ViewsTest(TestCase):
    def setUp(self):
        self.team = TeamFactory(workflow_enabled=True)
        w = WorkflowFactory(team=self.team, autocreate_subtitle=True)
        self.admin = TeamMemberFactory(team=self.team,
                                       role=TeamMember.ROLE_ADMIN)
        self.client.login(username=self.admin.user.username,
                          password='password')

    def check_task_list(self, tasks, **query_args):
        url = reverse("teams:team_tasks", kwargs={'slug': self.team.slug})
        response = self.client.get(url, query_args)
        self.assertEquals(response.status_code, 200)
        self.assertEquals([t.id for t in response.context['tasks']],
                          [t.id for t in tasks])

    def test_search(self):
        video = VideoFactory(primary_audio_language_code='en',
                             title='MyTitle')
        tv = TeamVideoFactory(team=self.team, video=video,
                              added_by=self.admin.user)
        self.assertEqual(tv.task_set.count(), 1)
        self.check_task_list(tv.task_set.all(), q='MyTitle')
        self.check_task_list(tv.task_set.all(), q='mytitle')
        self.check_task_list(tv.task_set.all(), q='my')
        self.check_task_list([], q='OtherTitle')

    def test_search_by_metadata(self):
        video = VideoFactory(primary_audio_language_code='en',
                             title='MyTitle')
        video.update_metadata({'speaker-name': 'Person'})
        tv = TeamVideoFactory(team=self.team, video=video,
                              added_by=self.admin.user)
        self.assertEqual(tv.task_set.count(), 1)
        self.check_task_list(tv.task_set.all(), q='Person')
        self.check_task_list(tv.task_set.all(), q='person')
        self.check_task_list(tv.task_set.all(), q='pers')


class VideoCacheTest(TestCase):
    def test_add_task_invalidates_video_cache(self):
        team_video = TeamVideoFactory()
        with assert_invalidates_model_cache(team_video.video):
            task = Task(team=team_video.team, team_video=team_video,
                        language='en', type=Task.TYPE_IDS['Translate'])
            task.save()
