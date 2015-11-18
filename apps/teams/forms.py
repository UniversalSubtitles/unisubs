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
import logging
import re

from auth.models import CustomUser as User
from django import forms
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.db import transaction
from django.forms.util import ErrorDict
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ugettext
from django.utils.translation import ungettext

from subtitles.forms import SubtitlesUploadForm
from teams.behaviors import get_main_project
from teams.models import (
    Team, TeamMember, TeamVideo, Task, Project, Workflow, Invite,
    BillingReport, MembershipNarrowing, Application
)
from teams import permissions
from teams.exceptions import ApplicationInvalidException
from teams.fields import TeamMemberInput
from teams.permissions import (
    roles_user_can_invite, can_delete_task, can_add_video, can_perform_task,
    can_assign_task, can_delete_language, can_remove_video,
    can_add_video_somewhere
)
from teams.permissions_const import ROLE_NAMES
from teams.workflows import TeamWorkflow
from videos.forms import (AddFromFeedForm, VideoForm, CreateSubtitlesForm,
                          MultiVideoCreateSubtitlesForm)
from videos.models import (
        VideoMetadata, VIDEO_META_TYPE_IDS, Video, VideoFeed,
)
from videos.tasks import import_videos_from_feed
from utils.forms import (ErrorableModelForm, get_label_for_value,
                         UserAutocompleteField)
from utils.forms.unisub_video_form import UniSubBoundVideoField
from utils.panslugify import pan_slugify
from utils.searching import get_terms
from utils.translation import get_language_choices, get_language_label
from utils.text import fmt
from utils.validators import MaxFileSizeValidator

logger = logging.getLogger(__name__)

class EditTeamVideoForm(forms.ModelForm):
    author = forms.CharField(max_length=255, required=False)
    creation_date = forms.DateField(required=False, input_formats=['%Y-%m-%d'],
                                    help_text="Format: YYYY-MM-DD")

    project = forms.ModelChoiceField(
        label=_(u'Project'),
        queryset = Project.objects.none(),
        required=True,
        empty_label=None,
        help_text=_(u"Let's keep things tidy, shall we?")
    )

    class Meta:
        model = TeamVideo
        fields = ('description', 'thumbnail', 'project',)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")

        super(EditTeamVideoForm, self).__init__(*args, **kwargs)

        self.fields['project'].queryset = self.instance.team.project_set.all()

    def clean(self, *args, **kwargs):
        super(EditTeamVideoForm, self).clean(*args, **kwargs)

        return self.cleaned_data

    def save(self, *args, **kwargs):
        obj = super(EditTeamVideoForm, self).save(*args, **kwargs)

        video = obj.video

        author = self.cleaned_data['author'].strip()
        creation_date = VideoMetadata.date_to_string(self.cleaned_data['creation_date'])

        self._save_metadata(video, 'Author', author)
        self._save_metadata(video, 'Creation Date', creation_date)
        # store the uploaded thumb on the video itself
        # TODO: simply remove the teamvideo.thumbnail image
        if obj.thumbnail:
            content = ContentFile(obj.thumbnail.read())
            name = obj.thumbnail.url.split('/')[-1]
            video.s3_thumbnail.save(name, content)

    def _save_metadata(self, video, meta, data):
        '''Save a single piece of metadata for the given video.

        The metadata is only saved if necessary (i.e. it's not blank OR it's blank
        but there's already other data that needs to be overwritten).

        '''
        meta_type_id = VIDEO_META_TYPE_IDS[meta]

        try:
            meta = VideoMetadata.objects.get(video=video, key=meta_type_id)
            meta.data = data
            meta.save()
        except VideoMetadata.DoesNotExist:
            if data:
                VideoMetadata(video=video, key=meta_type_id, data=data).save()

class MoveTeamVideoForm(forms.Form):
    team_video = forms.ModelChoiceField(queryset=TeamVideo.objects.all(),
                                        required=True)
    team = forms.ModelChoiceField(queryset=Team.objects.all(),
                                  required=True)

    project = forms.ModelChoiceField(queryset=Project.objects.all(),
                                     required=False)

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super(MoveTeamVideoForm, self).__init__(*args, **kwargs)

    def clean(self):
        team_video = self.cleaned_data.get('team_video')
        team = self.cleaned_data.get('team')
        project = self.cleaned_data.get('project')

        if not team_video or not team:
            return

        if project and project.team != team:
            raise forms.ValidationError(u"That project does not belong to that team.")

        if team_video.team.pk == team.pk:
            raise forms.ValidationError(u"That video is already in that team.")

        if not can_add_video(team, self.user):
            raise forms.ValidationError(u"You can't add videos to that team.")

        if not can_remove_video(team_video, self.user):
            raise forms.ValidationError(u"You can't remove that video from its team.")

        return self.cleaned_data

class BaseVideoBoundForm(forms.ModelForm):
    video_url = UniSubBoundVideoField(label=_('Video URL'),
        help_text=_("Enter the URL of any compatible video or any video on our site. You can also browse the site and use the 'Add Video to Team' menu."))

    def __init__(self, *args, **kwargs):
        super(BaseVideoBoundForm, self).__init__(*args, **kwargs)
        if hasattr(self, 'user'):
            self.fields['video_url'].user = self.user

class AddVideoToTeamForm(forms.Form):
    """Used to add a non-team video to one of the user's managed teams."""

    team = forms.ChoiceField()

    def __init__(self, user, data=None, **kwargs):
        super(AddVideoToTeamForm, self).__init__(data, **kwargs)
        team_qs = (Team.objects
                   .filter(users=user)
                   .prefetch_related('project_set'))
        self.fields['team'].choices = [
            (team.id, unicode(team)) for team in team_qs
            if can_add_video_somewhere(team, user)
        ]

class AddTeamVideoForm(BaseVideoBoundForm):
    language = forms.ChoiceField(label=_(u'Video language'), choices=(),
                                 required=False,
                                 help_text=_(u'It will be saved only if video does not exist in our database.'))

    project = forms.ModelChoiceField(
        label=_(u'Project'),
        queryset = Project.objects.none(),
        required=True,
        empty_label=None,
        help_text=_(u"Let's keep things tidy, shall we?")
    )

    class Meta:
        model = TeamVideo
        fields = ('video_url', 'language', 'description', 'thumbnail', 'project',)

    def __init__(self, team, user, *args, **kwargs):
        self.team = team
        self.user = user
        super(AddTeamVideoForm, self).__init__(*args, **kwargs)


        projects = self.team.project_set.all()

        if len(projects) > 1:
            projects = projects.exclude(slug='_root')

        self.fields['project'].queryset = projects

        ordered_projects = ([p for p in projects if p.is_default_project] +
                            [p for p in projects if not p.is_default_project])
        ordered_projects = [p for p in ordered_projects if can_add_video(team, user, p)]

        self.fields['project'].choices = [(p.pk, p) for p in ordered_projects]

        writable_langs = team.get_writable_langs()
        self.fields['language'].choices = [c for c in get_language_choices(True)
                                           if c[0] in writable_langs]

    def clean_video_url(self):
        video_url = self.cleaned_data['video_url']
        video = self.fields['video_url'].video
        try:
            tv = TeamVideo.objects.get(team=self.team, video=video)
            raise forms.ValidationError(mark_safe(u'Team has this <a href="%s">video</a>' % tv.get_absolute_url()))
        except TeamVideo.DoesNotExist:
            pass

        return video_url

    def clean(self):
        language = self.cleaned_data.get('language')
        video = self.fields['video_url'].video

        if video:
            team_video = video.get_team_video()
            if team_video and not team_video.team.deleted:
                team = team_video.team
                if team.user_can_view_videos(self.user):
                    link = '<a href="{}">{}</a>'.format(
                            team_video.team.get_absolute_url(),
                            team_video.team.name)
                    msg = mark_safe(
                        fmt(_(u'This video already belongs to the '
                              '%(team_link)s team.'),
                            team_link=link))
                else:
                    msg = _(u'This video already belongs to a team.')
                self._errors['video_url'] = self.error_class([msg])

            original_sl = video.subtitle_language()

            if (original_sl and not original_sl.language_code) and not language:
                msg = _(u'Set original language for this video.')
                self._errors['language'] = self.error_class([msg])

        return self.cleaned_data

    def success_message(self):
        return 'Video successfully added to team.'

    def save(self, commit=True):
        video = self.fields['video_url'].video

        obj = super(AddTeamVideoForm, self).save(False)

        obj.video = video
        obj.team = self.team
        commit and obj.save()
        return obj

class AddTeamVideosFromFeedForm(AddFromFeedForm):
    def __init__(self, team, user, *args, **kwargs):
        if not can_add_video(team, user):
            raise ValueError("%s can't add videos to %s" % (user, team))
        self.team = team
        super(AddTeamVideosFromFeedForm, self).__init__(user, *args, **kwargs)

    def make_feed(self, url):
        return VideoFeed.objects.create(team=self.team, user=self.user,
                                        url=url)

class CreateTeamForm(BaseVideoBoundForm):
    logo = forms.ImageField(validators=[MaxFileSizeValidator(settings.AVATAR_MAX_SIZE)], required=False)
    workflow_type = forms.ChoiceField(choices=(), initial="O")

    class Meta:
        model = Team
        fields = ('name', 'slug', 'description', 'logo', 'workflow_type',
                  'is_visible', 'video_url')

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super(CreateTeamForm, self).__init__(*args, **kwargs)
        self.fields['workflow_type'].choices = TeamWorkflow.get_choices()
        self.fields['video_url'].label = _(u'Team intro video URL')
        self.fields['video_url'].required = False
        self.fields['video_url'].help_text = _(u'''You can put an optional video
on your team homepage that explains what your team is about, to attract volunteers.
Enter a link to any compatible video, or to any video page on our site.''')
        self.fields['is_visible'].widget.attrs['class'] = 'checkbox'
        self.fields['slug'].label = _(u'Team URL: http://universalsubtitles.org/teams/')

    def clean_slug(self):
        slug = self.cleaned_data['slug']
        if re.match('^\d+$', slug):
            raise forms.ValidationError('Field can\'t contains only numbers')
        return slug

    def save(self, user):
        team = super(CreateTeamForm, self).save(False)
        video = self.fields['video_url'].video
        if video:
            team.video = video
        team.save()
        TeamMember.objects.create_first_member(team=team, user=user)
        return team

class TaskCreateForm(ErrorableModelForm):
    choices = [(10, 'Transcribe'), Task.TYPE_CHOICES[1]]
    type = forms.TypedChoiceField(choices=choices, coerce=int)
    language = forms.ChoiceField(choices=(), required=False)
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), required=False)

    def __init__(self, user, team, team_video, *args, **kwargs):
        self.non_display_form = False
        if kwargs.get('non_display_form'):
            self.non_display_form = kwargs.pop('non_display_form')
        super(TaskCreateForm, self).__init__(*args, **kwargs)

        self.user = user
        self.team_video = team_video

        # TODO: This is bad for teams with 10k members.
        team_user_ids = team.members.values_list('user', flat=True)

        langs = [l for l in get_language_choices(with_empty=True)
                 if l[0] in team.get_writable_langs()]
        self.fields['language'].choices = langs
        self.fields['assignee'].queryset = User.objects.filter(pk__in=team_user_ids)

        if self.non_display_form:
            self.fields['type'].choices = Task.TYPE_CHOICES

    def _check_task_creation_subtitle(self, tasks, cleaned_data):
        if self.team_video.subtitles_finished():
            self.add_error(_(u"This video has already been transcribed."),
                           'type', cleaned_data)
            return

    def _check_task_creation_translate(self, tasks, cleaned_data):
        if not self.team_video.subtitles_finished():
            self.add_error(_(u"No one has transcribed this video yet, so it can't be translated."),
                           'type', cleaned_data)
            return

        sl = self.team_video.video.subtitle_language(cleaned_data['language'])

        if sl and sl.is_complete_and_synced():
            self.add_error(_(u"This language already has a complete set of subtitles."),
                           'language', cleaned_data)

    def _check_task_creation_review_approve(self, tasks, cleaned_data):
        if not self.non_display_form:
            return

        lang = cleaned_data['language']
        video = self.team_video.video
        subtitle_language = video.subtitle_language(lang)

        if not subtitle_language or not subtitle_language.get_tip():
            self.add_error(_(
                u"This language for this video does not exist or doesn't have a version."
            ), 'language', cleaned_data)

    def clean(self):
        cd = self.cleaned_data

        type = cd['type']
        lang = cd['language']

        team_video = self.team_video
        project, team = team_video.project, team_video.team

        # TODO: Manager method?
        existing_tasks = list(Task.objects.filter(deleted=False, language=lang,
                                                  team_video=team_video))

        if any(not t.completed for t in existing_tasks):
            self.add_error(_(u"There is already a task in progress for that video/language."))

        type_name = Task.TYPE_NAMES[type]

        # TODO: Move into _check_task_creation_translate()?
        if type_name != 'Subtitle' and not lang:
            self.add_error(fmt(_(u"You must select a language for a "
                                 "%(task_type)s task."),
                               task_type=type_name))

        {'Subtitle': self._check_task_creation_subtitle,
         'Translate': self._check_task_creation_translate,
         'Review': self._check_task_creation_review_approve,
         'Approve': self._check_task_creation_review_approve
        }[type_name](existing_tasks, cd)

        return cd


    class Meta:
        model = Task
        fields = ('type', 'language', 'assignee')

class TaskAssignForm(forms.Form):
    task = forms.ModelChoiceField(queryset=Task.objects.none())
    assignee = forms.ModelChoiceField(queryset=User.objects.none(), required=False)

    def __init__(self, team, user, *args, **kwargs):
        super(TaskAssignForm, self).__init__(*args, **kwargs)

        self.team = team
        self.user = user
        self.fields['assignee'].queryset = User.objects.filter(team_members__team=team)
        self.fields['task'].queryset = team.task_set.incomplete()


    def clean_assignee(self):
        assignee = self.cleaned_data['assignee']

        if assignee:
            member = self.team.members.get(user=assignee)
            if member.has_max_tasks():
                raise forms.ValidationError(_(
                    u'That user has already been assigned the maximum number of tasks.'))

        return assignee

    def clean(self):
        task = self.cleaned_data['task']
        assignee = self.cleaned_data.get('assignee', -1)

        if assignee != -1:
            # There are a bunch of edge cases here that we need to check.
            unassigning_from_self      = (not assignee) and task.assignee and task.assignee.id == self.user.id
            assigning_to_self          = assignee and self.user.id == assignee.id
            can_assign_to_other_people = can_assign_task(task, self.user)

            # Users can always unassign a task from themselves.
            if not unassigning_from_self:
                # They can also assign a task TO themselves, assuming they can
                # perform it (which is checked further down).
                if not assigning_to_self:
                    # Otherwise they must have assign permissions in the team.
                    if not can_assign_to_other_people:
                        raise forms.ValidationError(_(
                            u'You do not have permission to assign this task.'))

            if assignee is None:
                return self.cleaned_data
            else:
                if not can_perform_task(assignee, task):
                    raise forms.ValidationError(_(
                        u'This user cannot perform that task.'))

        return self.cleaned_data

class TaskDeleteForm(forms.Form):
    task = forms.ModelChoiceField(queryset=Task.objects.all())
    discard_subs = forms.BooleanField(required=False)

    def __init__(self, team, user, *args, **kwargs):
        super(TaskDeleteForm, self).__init__(*args, **kwargs)

        self.user = user

        self.fields['task'].queryset = team.task_set.incomplete()


    def clean_task(self):
        task = self.cleaned_data['task']

        if not can_delete_task(task, self.user):
            raise forms.ValidationError(_(
                u'You do not have permission to delete this task.'))

        return task

class MessageTextField(forms.CharField):
    def __init__(self, *args, **kwargs):
        super(MessageTextField, self).__init__(
            max_length=4000, required=False, widget=forms.Textarea,
            *args, **kwargs)

class GuidelinesMessagesForm(forms.Form):
    pagetext_welcome_heading = MessageTextField(
        label=_('Welcome heading on your landing page for non-members'))

    messages_invite = MessageTextField(
        label=_('When a member is invited to join the team'))
    messages_application = MessageTextField(
        label=_('When a member applies to join the team'))
    messages_joins = MessageTextField(
        label=_('When a member joins the team'))
    messages_manager = MessageTextField(
        label=_('When a member is given the Manager role'))
    messages_admin = MessageTextField(
        label=_('When a member is given the Admin role'))

    guidelines_subtitle = MessageTextField(
        label=('When transcribing'))
    guidelines_translate = MessageTextField(
        label=('When translating'))
    guidelines_review = MessageTextField(
        label=('When reviewing'))

class GuidelinesLangMessagesForm(forms.Form):
  def __init__(self, *args, **kwargs):
    languages = kwargs.pop('languages')
    super(GuidelinesLangMessagesForm, self).__init__(*args, **kwargs)
    self.fields["messages_joins_language"] = forms.ChoiceField(label=_(u'New message language'), choices=get_language_choices(True),
                                                               required=False)

    self.fields["messages_joins_localized"] = MessageTextField(
        label=_('When a member speaking that language joins the team'))

    keys = []
    for language in languages:
        key = 'messages_joins_localized_%s' % language["code"]
        label = _('When a member joins the team, message in ' + get_language_label(language["code"]))
        keys.append({"key": key, "label": label})
        self.fields[key] = MessageTextField(initial=language["data"],
                                            label=label)
    sorted_keys = map(lambda x: x["key"], sorted(keys, key=lambda x: x["label"]))
    self.fields.keyOrder = ["messages_joins_language", "messages_joins_localized"] + sorted_keys

class SettingsForm(forms.ModelForm):
    logo = forms.ImageField(
        validators=[MaxFileSizeValidator(settings.AVATAR_MAX_SIZE)],
        help_text=_('Max 940 x 235'),
        widget=forms.FileInput,
        required=False)
    square_logo = forms.ImageField(
        validators=[MaxFileSizeValidator(settings.AVATAR_MAX_SIZE)],
        help_text=_('Recommended size: 100 x 100'),
        widget=forms.FileInput,
        required=False)

    class Meta:
        model = Team
        fields = ('description', 'logo', 'square_logo', 'is_visible')

class RenameableSettingsForm(SettingsForm):
    class Meta(SettingsForm.Meta):
            fields = SettingsForm.Meta.fields + ('name',)

class WorkflowForm(forms.ModelForm):
    class Meta:
        model = Workflow
        fields = ('autocreate_subtitle', 'autocreate_translate',
                  'review_allowed', 'approve_allowed')

class PermissionsForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ('membership_policy', 'video_policy', 'subtitle_policy',
                  'translate_policy', 'task_assign_policy', 'workflow_enabled',
                  'max_tasks_per_member', 'task_expiration',)

class SimplePermissionsForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ('membership_policy', 'video_policy')
        labels = {
            'membership_policy': _('How can users join your team?'),
            'video_policy': _('Who can add/remove videos?'),
        }

class LanguagesForm(forms.Form):
    preferred = forms.MultipleChoiceField(required=False, choices=())
    blacklisted = forms.MultipleChoiceField(required=False, choices=())

    def __init__(self, team, *args, **kwargs):
        super(LanguagesForm, self).__init__(*args, **kwargs)

        self.team = team
        self.fields['preferred'].choices = get_language_choices()
        self.fields['blacklisted'].choices = get_language_choices()

    def clean(self):
        preferred = set(self.cleaned_data['preferred'])
        blacklisted = set(self.cleaned_data['blacklisted'])

        if len(preferred & blacklisted):
            raise forms.ValidationError(_(u'You cannot blacklist a preferred language.'))

        return self.cleaned_data

class InviteForm(forms.Form):
    username = UserAutocompleteField(error_messages={
        'invalid': _(u'User is already a member of this team'),
    })
    message = forms.CharField(required=False,
                              widget=forms.Textarea(attrs={'rows': 4}),
                              label=_("Message to user"))
    role = forms.ChoiceField(choices=TeamMember.ROLES[1:][::-1],
                             initial='contributor',
                             label=_("Assign a role"))

    def __init__(self, team, user, *args, **kwargs):
        super(InviteForm, self).__init__(*args, **kwargs)
        self.team = team
        self.user = user
        self.fields['role'].choices = [(r, ROLE_NAMES[r])
                                       for r in roles_user_can_invite(team, user)]
        self.fields['username'].queryset = team.invitable_users()
        self.fields['username'].set_autocomplete_url(
            reverse('teams:autocomplete-invite-user', args=(team.slug,))
        )

    def save(self):
        from messages import tasks as notifier
        invite = Invite.objects.create(
            team=self.team, user=self.cleaned_data['username'], 
            author=self.user, role=self.cleaned_data['role'],
            note=self.cleaned_data['message'])
        invite.save()
        notifier.team_invitation_sent.delay(invite.pk)
        return invite

class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ('name', 'description', 'workflow_enabled')

    def __init__(self, team, *args, **kwargs):
        super(ProjectForm, self).__init__(*args, **kwargs)
        self.team = team

    def clean_name(self):
        name = self.cleaned_data['name']

        same_name_qs = self.team.project_set.filter(slug=pan_slugify(name))
        if self.instance.id is not None:
            same_name_qs = same_name_qs.exclude(id=self.instance.id)

        if same_name_qs.exists():
            raise forms.ValidationError(
                _(u"There's already a project with this name"))
        return name

    def save(self):
        project = super(ProjectForm, self).save(commit=False)
        project.team = self.team
        project.save()
        return project

class EditProjectForm(forms.Form):
    project = forms.ChoiceField(choices=[])
    name = forms.CharField(required=True)
    description = forms.CharField(widget=forms.Textarea, required=False)

    def __init__(self, team, *args, **kwargs):
        super(EditProjectForm, self).__init__(*args, **kwargs)
        self.team = team
        self.fields['project'].choices = [
            (p.id, p.id) for p in team.project_set.all()
        ]

    def clean(self):
        if self.cleaned_data.get('name') and self.cleaned_data.get('project'):
            self.check_duplicate_name()
        return self.cleaned_data

    def check_duplicate_name(self):
        name = self.cleaned_data['name']

        same_name_qs = (
            self.team.project_set
            .filter(slug=pan_slugify(name))
            .exclude(id=self.cleaned_data['project'])
        )

        if same_name_qs.exists():
            self._errors['name'] = self.error_class([
                _(u"There's already a project with this name")
            ])
            del self.cleaned_data['name']

    def save(self):
        project = self.team.project_set.get(id=self.cleaned_data['project'])
        project.name = self.cleaned_data['name']
        project.description = self.cleaned_data['description']
        project.save()
        return project

class AddProjectManagerForm(forms.Form):
    member = UserAutocompleteField()

    def __init__(self, team, project, *args, **kwargs):
        super(AddProjectManagerForm, self).__init__(*args, **kwargs)
        self.team = team
        self.project = project
        self.fields['member'].queryset = project.potential_managers()
        self.fields['member'].set_autocomplete_url(
            reverse('teams:autocomplete-project-manager',
                    args=(team.slug, project.slug))
        )

    def clean_member(self):
        return self.team.get_member(self.cleaned_data['member'])

    def save(self):
        member = self.cleaned_data['member']
        member.make_project_manager(self.project)

class RemoveProjectManagerForm(forms.Form):
    member = TeamMemberInput()

    def __init__(self, team, project, *args, **kwargs):
        super(RemoveProjectManagerForm, self).__init__(*args, **kwargs)
        self.team = team
        self.project = project
        self.fields['member'].set_team(team)

    def clean_member(self):
        member = self.cleaned_data['member']
        if not member.is_project_manager(self.project):
            raise forms.ValidationError(_(u'%(user)s is not a manager'),
                                        user=username)
        return member

    def save(self):
        member = self.cleaned_data['member']
        member.remove_project_manager(self.project)

class AddLanguageManagerForm(forms.Form):
    member = UserAutocompleteField()

    def __init__(self, team, language_code, *args, **kwargs):
        super(AddLanguageManagerForm, self).__init__(*args, **kwargs)
        self.team = team
        self.language_code = language_code
        self.fields['member'].queryset = team.potential_language_managers(
            language_code)
        self.fields['member'].widget.set_autocomplete_url(
            reverse('teams:autocomplete-language-manager',
                    args=(team.slug, language_code))
        )

    def clean_member(self):
        return self.team.get_member(self.cleaned_data['member'])

    def save(self):
        member = self.cleaned_data['member']
        member.make_language_manager(self.language_code)

class RemoveLanguageManagerForm(forms.Form):
    member = TeamMemberInput()

    def __init__(self, team, language_code, *args, **kwargs):
        super(RemoveLanguageManagerForm, self).__init__(*args, **kwargs)
        self.team = team
        self.language_code = language_code
        self.fields['member'].set_team(team)

    def clean_member(self):
        member = self.cleaned_data['member']
        if not member.is_language_manager(self.language_code):
            raise forms.ValidationError(_(u'%(user)s is not a manager'),
                                        user=username)
        return member

    def save(self):
        member = self.cleaned_data['member']
        member.remove_language_manager(self.language_code)

class DeleteLanguageVerifyField(forms.CharField):
    def __init__(self):
        help_text=_('Type "Yes I want to delete this language" if you are '
                    'sure you wish to continue')
        forms.CharField.__init__(self, label=_(u'Are you sure?'),
                                 help_text=help_text)

    def clean(self, value):
        # check text against a translated version of the confirmation string,
        # so when help_text gets translated things still work.
        if value != _(u'Yes I want to delete this language'):
            raise forms.ValidationError(_(u"Confirmation text doesn't match"))

class DeleteLanguageForm(forms.Form):
    verify_text = DeleteLanguageVerifyField()

    def __init__(self, user, team, language, *args, **kwargs):
        super(DeleteLanguageForm, self).__init__(*args, **kwargs)

        self.user = user
        self.team = team
        self.language = language

        # generate boolean fields for deleting languages (rather than forking
        # them).
        for sublanguage in self.language.get_dependent_subtitle_languages():
            key = self.key_for_sublanguage_delete(sublanguage)
            label = sublanguage.get_language_code_display()
            field = forms.BooleanField(label=label, required=False)
            field.widget.attrs['class'] = 'checkbox'
            self.fields[key] = field

    def clean(self):
        team_video = self.language.video.get_team_video()

        if not team_video:
            raise forms.ValidationError(_(
                u"These subtitles are not under a team's control."))

        if not can_delete_language(team_video.team, self.user):
            raise forms.ValidationError(_(
                u'You do not have permission to delete this language.'))

        return self.cleaned_data

    def key_for_sublanguage_delete(self, sublanguage):
        return 'delete_' + sublanguage.language_code

    def sublanguage_fields(self):
        return [self[key] for key in self.fields.keys()
                if key.startswith('delete_')]

    def languages_to_fork(self):
        assert self.is_bound
        rv = []
        for sublanguage in self.language.get_dependent_subtitle_languages():
            key = self.key_for_sublanguage_delete(sublanguage)
            if not self.cleaned_data.get(key):
                rv.append(sublanguage)
        return rv

class TaskUploadForm(SubtitlesUploadForm):
    task = forms.ModelChoiceField(Task.objects, required=True)

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user')
        video = kwargs.pop('video')
        super(TaskUploadForm, self).__init__(user, video, False,
                                             *args, **kwargs)

    def clean_task(self):
        task = self.cleaned_data['task']

        if not can_perform_task(self.user, task):
            raise forms.ValidationError(_(u'You cannot perform that task.'))

        if task.team_video.video_id != self.video.id:
            raise forms.ValidationError(_(u'Mismatched video and task!'))

        return task

    def clean(self):
        super(TaskUploadForm, self).clean()

        try:
            task = self.cleaned_data['task']
        except KeyError:
            raise forms.ValidationError(_(u'Task has been deleted'))
        language_code = self.cleaned_data['language_code']
        from_language_code = self.cleaned_data['from_language_code']

        if task.language and task.language != language_code:
            raise forms.ValidationError(_(
                'The selected language does not match the task.'))

        current_version = task.get_subtitle_version()
        if current_version:
            current_sl = current_version.subtitle_language
            current_source_lc = current_sl.get_translation_source_language_code()
            if current_source_lc and current_source_lc != from_language_code:
                raise forms.ValidationError(fmt(_(
                    "The selected source language %(from_code)s "
                    "does not match the existing source language "
                    "%(cur_code)s for that task."),
                      from_code=from_language_code,
                      cur_code=current_source_lc,
                ))
        return self.cleaned_data

    def save(self, *args, **kwargs):
        task = self.cleaned_data['task']
        language_code = self.cleaned_data['language_code']

        version = super(TaskUploadForm, self).save(*args, **kwargs)

        if not task.assignee:
            task.assignee = self.user
            task.set_expiration()

        task.new_subtitle_version = version
        task.language = language_code

        task.save()

        return version

def make_billing_report_form():
    """Factory function to create a billing report form """
    class BillingReportForm(forms.Form):
        teams = forms.ModelMultipleChoiceField(
            required=True,
            queryset=(Team.objects.with_recent_billing_record(40)
                      .order_by('name')),
            widget=forms.CheckboxSelectMultiple)
        start_date = forms.DateField(required=True, help_text='YYYY-MM-DD')
        end_date = forms.DateField(required=True, help_text='YYYY-MM-DD')
        type = forms.ChoiceField(required=True,
                                 choices=BillingReport.TYPE_CHOICES,
                                 initial=BillingReport.TYPE_BILLING_RECORD)
    return BillingReportForm

class TaskCreateSubtitlesForm(CreateSubtitlesForm):
    """CreateSubtitlesForm that also sets the language for task."""

    def __init__(self, request, task, data=None):
        CreateSubtitlesForm.__init__(self, request, task.team_video.video,
                                     data)
        self.task = task

    def handle_post(self):
        self.task.language = self.cleaned_data['subtitle_language_code']
        self.task.save()
        return CreateSubtitlesForm.handle_post(self)

class TeamMultiVideoCreateSubtitlesForm(MultiVideoCreateSubtitlesForm):
    """MultiVideoCreateSubtitlesForm that is task-aware."""

    def __init__(self, request, team, data=None):
        MultiVideoCreateSubtitlesForm.__init__(self, request, team.videos,
                                               data)
        self.team = team

    def handle_post(self):
        if self.team.workflow_enabled:
            # set the language for the task being performed (if needed)
            language = self.cleaned_data['subtitle_language_code']
            tasks = self.get_video().get_team_video().task_set
            (tasks.incomplete_subtitle().filter(language='')
             .update(language=language))
        return MultiVideoCreateSubtitlesForm.handle_post(self)

class MoveVideosForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.none(),
                                  required=True,
                                  empty_label=None)

    class Meta:
        fields = ('team')

    def __init__(self, user,  *args, **kwargs):
        super(MoveVideosForm, self).__init__(*args, **kwargs)
        self.fields['team'].queryset = user.managed_teams(include_manager=False)

class VideoFiltersForm(forms.Form):
    """Form to handle the filters on the team videos page

    Note that this form is a bit weird because it uses the GET params, rather
    than POST data.
    """
    q = forms.CharField(label=_('Title/Description'), required=False)
    project = forms.ChoiceField(label=_('Project'), required=False,
                                choices=[])
    has_language = forms.ChoiceField(
        label=_('Has completed language'), required=False,
        choices=get_language_choices(with_empty=True))
    missing_language = forms.ChoiceField(
        label=_('Missing completed language'), required=False,
        choices=get_language_choices(with_empty=True))
    sort = forms.ChoiceField(choices=[
        ('name', _('Name, a-z')),
        ('-name', _('Name, z-a')),
        ('time', _('Time, oldest')),
        ('-time', _('Time, newest')),
        ('-subs', _('Most completed languages')),
        ('subs', _('Least complete languages')),
    ], initial='-time', required=False)

    def __init__(self, team, get_data=None, **kwargs):
        super(VideoFiltersForm, self).__init__(data=self.calc_data(get_data),
                                               **kwargs)
        self.team = team
        self.setup_project_field()
        self.selected_project = None

    def calc_data(self, get_data):
        if get_data is None:
            return None
        data = {
            name: value
            for name, value in get_data.items()
            if name not in ('page', 'selection')
        }
        return data if data else None

    def setup_project_field(self):
        projects = Project.objects.for_team(self.team)
        if projects:
            choices = [
                ('', _('Any')),
                ('none', _('No Project')),
            ] + [
                (p.slug, p.name) for p in projects
            ]
            self.fields['project'].choices = choices
            main_project = get_main_project(self.team)
            if main_project is None:
                self.fields['project'].initial = ''
            else:
                self.fields['project'].initial = main_project.slug
            self.show_project = True
        else:
            del self.fields['project']
            self.show_project = False

    def get_queryset(self):
        project = self.cleaned_data.get('project')
        has_language = self.cleaned_data.get('has_language')
        missing_language = self.cleaned_data.get('missing_language')
        q = self.cleaned_data['q']
        sort = self.cleaned_data['sort']

        qs = Video.objects.filter(teamvideo__team=self.team)

        if q:
            qs = qs.search(q)
        if has_language:
            qs = qs.has_completed_language(has_language)
        if missing_language:
            qs = qs.missing_completed_language(missing_language)
        if project:
            if project == 'none':
                project = Project.DEFAULT_NAME
            qs = qs.filter(teamvideo__project__slug=project)
            try:
                self.selected_project = self.team.project_set.get(
                    slug=project)
            except Project.DoesNotExist:
                pass

        if sort in ('subs', '-subs'):
            qs = qs.add_num_completed_languages()

        qs = qs.order_by({
             'name':  'title',
            '-name': '-title',
             'subs':  'num_completed_languages',
            '-subs': '-num_completed_languages',
             'time':  'created',
            '-time': '-created',
        }.get(sort or '-time'))

        return qs.select_related('video')

    def is_filtered(self):
        return self.is_bound and self.is_valid()

    def get_current_filters(self):
        return [
            u'{}: {}'.format(self[name].label,
                             get_label_for_value(self, name))
            for name in self.changed_data
        ]

class MemberFiltersForm(forms.Form):
    LANGUAGE_CHOICES = [
        ('any', _('Any language')),
    ] + get_language_choices()

    q = forms.CharField(label=_('Search'), required=False)

    role = forms.ChoiceField(choices=[
        ('any', _('All roles')),
        (TeamMember.ROLE_ADMIN, _('Admins')),
        (TeamMember.ROLE_MANAGER, _('Managers')),
        (TeamMember.ROLE_CONTRIBUTOR, _('Contributors')),
    ], initial='any', required=False)
    language = forms.ChoiceField(choices=LANGUAGE_CHOICES,
                                 label=_('Language spoken'),
                                 initial='any', required=False)
    sort = forms.ChoiceField(choices=[
        ('recent', _('Date joined, most recent')),
        ('oldest', _('Date joined, oldest')),
    ], initial='recent', required=False)

    def __init__(self, get_data=None):
        super(MemberFiltersForm, self).__init__(
            self.calc_data(get_data)
        )

    def calc_data(self, get_data):
        if get_data is None:
            return None
        data = {k:v for k, v in get_data.items() if k != 'page'}
        return data if data else None

    def update_qs(self, qs):
        if not self.is_bound:
            data = {}
        elif not self.is_valid():
            # we should never get here
            logger.warn("Invalid member filters: %s", self.data)
            data = {}
        else:
            data = self.cleaned_data

        q = data.get('q', '')
        role = data.get('role', 'any')
        language = data.get('language', 'any')
        sort = data.get('sort', 'recent')

        for term in [term.strip() for term in q.split()]:
            if term:
                qs = qs.filter(Q(user__first_name__icontains=term)
                               | Q(user__last_name__icontains=term)
                               | Q(user__email__icontains=term)
                               | Q(user__username__icontains=term)
                               | Q(user__biography__icontains=term))
        if role != 'any':
            if role != TeamMember.ROLE_ADMIN:
                qs = qs.filter(role=role)
            else:
                qs = qs.filter(Q(role=TeamMember.ROLE_ADMIN)|
                               Q(role=TeamMember.ROLE_OWNER))
        if language != 'any':
            qs = qs.filter(user__userlanguage__language=language)
        if sort == 'oldest':
            qs = qs.order_by('created')
        else:
            qs = qs.order_by('-created')
        return qs

class EditMembershipForm(forms.Form):
    member = forms.ChoiceField()
    remove = forms.BooleanField(required=False)
    role = forms.ChoiceField(choices=[
        (TeamMember.ROLE_CONTRIBUTOR, _('Contributor')),
        (TeamMember.ROLE_MANAGER, _('Manager')),
        (TeamMember.ROLE_ADMIN, _('Admin')),
    ], initial=TeamMember.ROLE_CONTRIBUTOR)
    language_narrowings = forms.MultipleChoiceField(required=False)
    project_narrowings = forms.MultipleChoiceField(required=False)

    def __init__(self, member, *args, **kwargs):
        super(EditMembershipForm, self).__init__(*args, **kwargs)
        edit_perms = permissions.get_edit_member_permissions(member)
        self.enabled = True
        member_qs = (TeamMember.objects
                     .filter(team_id=member.team_id)
                     .exclude(id=member.id))

        if edit_perms == permissions.EDIT_MEMBER_NOT_PERMITTED:
            self.enabled = False
            self.fields['role'].choices = []
            member_qs = TeamMember.objects.none()
            del self.fields['remove']
        elif edit_perms == permissions.EDIT_MEMBER_CANT_EDIT_ADMIN:
            del self.fields['role'].choices[-1]
            member_qs = member_qs.exclude(role__in=[
                TeamMember.ROLE_ADMIN, TeamMember.ROLE_OWNER,
            ])

        self.editable_member_ids = set(m.id for m in member_qs)
        # no need for a fancy label, since we set the choices with JS anyway
        self.fields['member'].choices = [
            (mid, mid) for mid in self.editable_member_ids
        ]

    def show_remove_button(self):
        return 'remove' in self.fields

    def save(self):
        member_to_edit = TeamMember.objects.get(
            id=self.cleaned_data['member']
        )
        if self.cleaned_data.get('remove'):
            member_to_edit.delete()
        else:
            member_to_edit.role = self.cleaned_data['role']
            member_to_edit.save()

class BulkTeamVideoForm(forms.Form):
    """Base class for forms that operate on multiple team videos at once."""
    include_all = forms.BooleanField(label='', required=False)

    def __init__(self, team, user, videos_qs, selection, all_selected,
                 filters_form, *args, **kwargs):
        super(BulkTeamVideoForm, self).__init__(*args, **kwargs)
        self.team = team
        self.user = user
        self.videos_qs = videos_qs
        self.selection = selection
        self.filters_form = filters_form
        self.setup_include_all(videos_qs, selection, all_selected)
        self.setup_fields()

    def save(self):
        self.perform_save(self.find_team_videos_to_update())

    def find_team_videos_to_update(self):
        qs = self.videos_qs
        if not self.cleaned_data.get('include_all'):
            qs = qs.filter(id__in=self.selection)
        self.count = qs.count()
        return TeamVideo.objects.filter(video__in=qs).select_related('video')

    def setup_include_all(self, videos_qs, selection, all_selected):
        if not all_selected:
            del self.fields['include_all']
        else:
            total_videos = videos_qs.count()
            if total_videos <= len(selection):
                del self.fields['include_all']
            else:
                self.fields['include_all'].label = fmt(
                    _('Include all %(count)s videos'),
                    count=total_videos)

    def setup_fields(self):
        """Override this if you need to dynamically setup the form fields."""
        pass

    def perform_save(self, qs):
        """Does the work for the save() method.

        Args:
            qs -- queryset of TeamVideos that should be operated on.
        """
        raise NotImplementedError()

class MoveTeamVideosForm(BulkTeamVideoForm):
    new_team = forms.ChoiceField(label=_('New Team'), choices=[])
    project = forms.ChoiceField(label=_('Project'), choices=[],
                                required=False)

    def setup_fields(self):
        dest_teams = [self.team] + permissions.can_move_videos_to(
            self.team, self.user)
        self.fields['new_team'].choices = [
            (dest.id, dest.name) for dest in dest_teams
        ]
        self.setup_project_field(dest_teams)

    def setup_project_field(self, dest_teams):
        # choices regular django choices object.  project_options is a list of
        # (id, name, team_id) tuples.  We need to store team_id in the
        # <option> tag to make our javascript work
        choices = [ ('', _('None')) ]
        self.project_options = [
            ('', _('None'), 0),
        ]

        qs = (Project.objects
              .filter(team__in=dest_teams)
              .exclude(name=Project.DEFAULT_NAME))
        for project in qs:
            choices.append((project.id, project.name))
            self.project_options.append(
                (project.id, project.name, project.team_id)
            )
        self.fields['project'].choices = choices
        if self.filters_form.selected_project:
            selected_id = self.filters_form.selected_project.id
            self['project'].field.initial = selected_id
        else:
            self['project'].field.initial = ''

    def clean_project(self):
        try:
            team = self.cleaned_data['new_team']
        except KeyError:
            # No valid team, so we can't validate the project.
            return None

        project_id = self.cleaned_data.get('project', '')

        if project_id == '':
            return team.default_project

        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise forms.ValidationError(_("Invalid project"))

        if project.team_id != team.id:
            raise forms.ValidationError(_("Project is not part of team"))
        return project

    def clean_new_team(self):
        if not self.cleaned_data.get('new_team'):
            return None
        return Team.objects.get(id=self.cleaned_data['new_team'])

    def perform_save(self, qs):
        for team_video in qs:
            team_video.move_to(self.cleaned_data['new_team'],
                               self.cleaned_data['project'])

    def message(self):
        new_team = self.cleaned_data['new_team']
        project = self.cleaned_data['project']
        if new_team == self.team:
            if project.is_default_project:
                msg = ungettext(
                    'Video removed from project',
                    '%(count)s videos removed from projects',
                    self.count)
            else:
                msg = ungettext(
                    'Video moved to Project: %(project)s',
                    '%(count)s moved to Project: %(project)s',
                    self.count)
        else:
            if project.is_default_project:
                msg = ungettext(
                    'Video moved to %(team_link)s',
                    '%(count)s moved to %(team_link)s',
                    self.count)
            else:
                msg = ungettext(
                    'Video moved to %(team_link)s (Project: %(project)s)',
                    '%(count)s moved to %(team_link)s (Project: %(project)s)',
                    self.count)
        team_link = '<a href="{}">{}</a>.'.format(
            reverse('teams:dashboard', args=(new_team.slug,)),
            new_team)
        return fmt(msg, team_link=team_link, project=project.name,
                   count=self.count)

class RemoveTeamVideosForm(BulkTeamVideoForm):
    def perform_save(self, qs):
        qs.delete()

    def message(self):
        msg = ungettext('Video removed from project',
                        '%(count)s videos removed from projects',
                        self.count)
        return fmt(msg, count=self.count)

class BulkEditTeamVideosForm(BulkTeamVideoForm):
    primary_audio_language = forms.ChoiceField(required=False, choices=[])
    project = forms.ChoiceField(label=_('Project'), choices=[],
                                required=False)
    thumbnail = forms.ImageField(label=_('Change thumbnail'), required=False)

    def setup_fields(self):
        self.fields['primary_audio_language'].choices = \
                get_language_choices(with_empty=True)
        projects = self.team.project_set.all()
        if len(projects) > 1:
            self.fields['project'].choices = [
                ('', '---------'),
            ]
            for p in projects:
                if p.is_default_project:
                    choice = (p.id, _('No Project'))
                else:
                    choice = (p.id, p.name)
                self.fields['project'].choices.append(choice)

        else:
            # only the default project has been created, don't present a
            # selectbox with that as the only choice
            del self.fields['project']

    def perform_save(self, qs):
        qs = qs.select_related('video')
        project = self.cleaned_data.get('project')
        primary_audio_language = self.cleaned_data['primary_audio_language']
        thumbnail = self.cleaned_data['thumbnail']

        for team_video in qs:
            video = team_video.video

            if project and project != team_video.project_id:
                team_video.project_id = project
                team_video.save()
            if (primary_audio_language and
                primary_audio_language != video.primary_audio_language_code):
                video.primary_audio_language_code = primary_audio_language
                video.save()
            if thumbnail:
                team_video.video.s3_thumbnail.save(thumbnail.name, thumbnail)

    def message(self):
        msg = ungettext('Video updated',
                        '%(count)s videos updated',
                        self.count)
        return fmt(msg, count=self.count)

class NewAddTeamVideoForm(VideoForm):
    project = forms.ChoiceField(label=_('Project'), choices=[],
                                required=False)
    thumbnail = forms.ImageField(required=False)

    def __init__(self, team, user, *args, **kwargs):
        super(NewAddTeamVideoForm, self).__init__(user, *args, **kwargs)
        self.team = team
        self.fields['project'].choices = [
            ('', _('None')),
        ] + [
            (p.id, p.name) for p in Project.objects.for_team(team)
        ]
        if not self.fields['project'].choices:
            del self.fields['project']

    def clean(self):
        if not self._errors:
            video, created = Video.get_or_create_for_url(
                self.cleaned_data['video_url'], self._video_type, self.user,
            )
            if not created and video.get_team_video() is not None:
                self._errors['video_url'] = self.error_class([
                    _(u'Video is already part of a team')
                ])
                return
            self.video = video
            self.created = created
        return self.cleaned_data

    def save(self):
        if 'project' in self.fields:
            project_id = self.cleaned_data['project']
            if not project_id:
                project_id = None
        else:
            project_id = None
        team_video = TeamVideo.objects.create(
            video=self.video, team=self.team, project_id=project_id,
        )
        if self.cleaned_data['thumbnail']:
            thumb = self.cleaned_data['thumbnail']
            self.video.s3_thumbnail.save(thumb.name, thumb)
        return team_video

    def message(self):
        if self.created:
            return _('Video added to team.')
        else:
            return _('Existing video added to team.')

class NewEditTeamVideoForm(forms.Form):
    primary_audio_language = forms.ChoiceField(required=False, choices=[])
    project = forms.ChoiceField(label=_('Project'), choices=[],
                                required=False)
    thumbnail = forms.ImageField(label=_('Change thumbnail'), required=False)

    def __init__(self, team, user, videos_qs, selection, all_selected,
                 filters_form, *args, **kwargs):
        super(NewEditTeamVideoForm, self).__init__(*args, **kwargs)
        self.team = team
        self.fetch_video(selection)
        self.setup_project_field()
        self.setup_primary_audio_language_field()

    def fetch_video(self, selection):
        if len(selection) != 1:
            raise ValueError("Exactly 1 video must be selected")
        self.video = (self.team.videos
                      .select_related('teamvideo')
                      .get(id=selection[0]))
        self.team_video = self.video.get_team_video()

    def setup_project_field(self):
        projects = Project.objects.for_team(self.team)
        if projects:
            self.fields['project'].choices = [
                (self.team.default_project.id, _('None')),
            ] + [
                (p.id, p.name) for p in projects
            ]
            self.fields['project'].initial = self.team_video.project_id
        else:
            # only the default project has been created, don't present a
            # selectbox with that as the only choice
            del self.fields['project']

    def setup_primary_audio_language_field(self):
        field = self.fields['primary_audio_language']
        field.choices = get_language_choices(with_empty=True)
        field.initial = self.video.primary_audio_language_code

    def save(self):
        project = self.cleaned_data.get('project')
        primary_audio_language = self.cleaned_data['primary_audio_language']
        thumbnail = self.cleaned_data['thumbnail']

        if 'project' in self.fields:
            if project == '':
                project = self.team.default_project.id
            if project != self.team_video.project_id:
                self.team_video.project_id = project
                self.team_video.save()
        if primary_audio_language != self.video.primary_audio_language_code:
            self.video.primary_audio_language_code = primary_audio_language
            self.video.save()
        if thumbnail:
            self.video.s3_thumbnail.save(thumbnail.name, thumbnail)
        return self.team_video

    def message(self):
        return _('Video updated.')

class ApplicationForm(forms.Form):
    about_you = forms.CharField(widget=forms.Textarea, label="")
    language1 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True))
    language2 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True), required=False)
    language3 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True), required=False)
    language4 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True), required=False)
    language5 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True), required=False)
    language6 = forms.ChoiceField(
        choices=get_language_choices(with_empty=True), required=False)

    def __init__(self, application, *args, **kwargs):
        super(ApplicationForm, self).__init__(*args, **kwargs)
        self.application = application
        self.fields['about_you'].help_text = fmt(
            ugettext('Tell us a little bit about yourself and why '
                     'you\'re interested in translating with '
                     '%(team)s.  This should be 3-5 sentences, no '
                     'longer!'),
            team=application.team)
        for i, language in enumerate(application.user.get_languages()):
            field = self.fields['language{}'.format(i+1)]
            field.initial = language

    def clean(self):
        try:
            self.application.check_can_submit()
        except ApplicationInvalidException, e:
            raise forms.ValidationError(e.message)
        return self.cleaned_data

    def save(self):
        self.application.note = self.cleaned_data['about_you']
        self.application.save()
        languages = []
        for i in xrange(1, 7):
            value = self.cleaned_data['language{}'.format(i)]
            if value:
                languages.append({"language": value, "priority": i})
        self.application.user.set_languages(languages)
