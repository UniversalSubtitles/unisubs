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

import collections
import datetime
from urllib import quote_plus
import urlparse

from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import query, Q
from django.utils.translation import ugettext_lazy as _
import babelsubs

from auth.models import CustomUser as User
from externalsites import google
from externalsites import syncing
from externalsites.exceptions import (SyncingError, RetryableSyncingError,
                                      YouTubeAccountExistsError)
from subtitles.models import SubtitleLanguage, SubtitleVersion
from teams.models import Team, TeamVideo
from utils.text import fmt
from videos.models import Video, VideoUrl, VideoFeed
from videos.permissions import can_user_resync_own_video
import videos.models
import videos.tasks

def now():
    # define now as a function so it can be patched in the unittests
    return datetime.datetime.now()

class ExternalAccountManager(models.Manager):
    def create(self, team=None, user=None, **kwargs):
        if team is not None and user is not None:
            raise ValueError("team and user can't both be specified")
        if team is not None:
            kwargs['type'] = ExternalAccount.TYPE_TEAM
            kwargs['owner_id'] = team.id
        elif user is not None:
            kwargs['type'] = ExternalAccount.TYPE_USER
            kwargs['owner_id'] = user.id

        return super(ExternalAccountManager, self).create(**kwargs)

    def for_owner(self, owner):
        if isinstance(owner, Team):
            type_ = ExternalAccount.TYPE_TEAM
        elif isinstance(owner, User):
            type_ = ExternalAccount.TYPE_USER
        else:
            raise TypeError("Invalid owner type: %r" % owner)
        return self.filter(type=type_, owner_id=owner.id)

    def get_sync_account(self, video, video_url):
        team_video = video.get_team_video()
        if team_video is not None:
            return self._get_sync_account_team_video(team_video, video_url)
        else:
            return self._get_sync_account_nonteam_video(video, video_url)

    def _get_sync_account_team_video(self, team_video, video_url):
        return self.get(type=ExternalAccount.TYPE_TEAM,
                      owner_id=team_video.team_id)

    def _get_sync_account_nonteam_video(self, video, video_url):
            return self.get(type=ExternalAccount.TYPE_USER,
                          owner_id=video.user_id)

    def team_accounts(self):
        return self.filter(type=ExternalAccount.TYPE_TEAM)

    def user_accounts(self):
        return self.filter(type=ExternalAccount.TYPE_USER)

class ExternalAccount(models.Model):
    account_type = NotImplemented
    video_url_type = NotImplemented

    TYPE_USER = 'U'
    TYPE_TEAM = 'T'
    TYPE_CHOICES = (
        (TYPE_USER, _('User')),
        (TYPE_TEAM, _('Team')),
    )

    type = models.CharField(max_length=1,choices=TYPE_CHOICES)
    owner_id = models.IntegerField()

    objects = ExternalAccountManager()

    def delete(self):
        models_to_delete = [
            SyncedSubtitleVersion,
            SyncHistory,
        ]
        for model in models_to_delete:
            qs = model.objects.filter(account_type=self.account_type,
                                      account_id=self.id)
            qs.delete()
        super(ExternalAccount, self).delete()

    @property
    def team(self):
        if self.type == ExternalAccount.TYPE_TEAM:
            return Team.objects.get(id=self.owner_id)
        else:
            return None

    @property
    def user(self):
        if self.type == ExternalAccount.TYPE_USER:
            return User.objects.get(id=self.owner_id)
        else:
            return None

    def should_sync_video_url(self, video, video_url):
        return video_url.type == self.video_url_type

    def update_subtitles(self, video_url, language):
        version = language.get_public_tip()
        if version is None or self.should_skip_syncing():
            return
        sync_history_values = {
            'account': self,
            'video_url': video_url,
            'language': language,
            'action': SyncHistory.ACTION_UPDATE_SUBTITLES,
            'version': version,
        }

        try:
            self.do_update_subtitles(video_url, language, version)
        except Exception, e:
            SyncHistory.objects.create_for_error(e, **sync_history_values)
        else:
            SyncHistory.objects.create_for_success(**sync_history_values)
            SyncedSubtitleVersion.objects.set_synced_version(
                self, video_url, language, version)

    def delete_subtitles(self, video_url, language):
        sync_history_values = {
            'account': self,
            'language': language,
            'video_url': video_url,
            'action': SyncHistory.ACTION_DELETE_SUBTITLES,
        }
        if self.should_skip_syncing():
            return

        try:
            self.do_delete_subtitles(video_url, language)
        except Exception, e:
            SyncHistory.objects.create_for_error(e, **sync_history_values)
        else:
            SyncHistory.objects.create_for_success(**sync_history_values)
            SyncedSubtitleVersion.objects.unset_synced_version(
                self, video_url, language)

    def do_update_subtitles(self, video_url, language, version):
        """Do the work needed to update subititles.

        Subclasses must implement this method.
        """
        raise NotImplementedError()

    def do_delete_subtitles(self, video_url, language):
        """Do the work needed to delete subtitles

        Subclasses must implement this method.
        """
        raise NotImplementedError()

    def should_skip_syncing(self):
        """Return True if we should not sync subtitles.

        Subclasses may optionally override this method.
        """
        return False

    class Meta:
        abstract = True

class KalturaAccount(ExternalAccount):
    account_type = 'K'
    video_url_type = videos.models.VIDEO_TYPE_KALTURA

    partner_id = models.CharField(max_length=100,
                                  verbose_name=_('Partner ID'))
    secret = models.CharField(
        max_length=100, verbose_name=_('Secret'),
        help_text=_('Administrator secret found in Settings -> '
                    'Integration on your Kaltura control panel'))

    class Meta:
        verbose_name = _('Kaltura account')
        unique_together = [
            ('type', 'owner_id')
        ]

    def __unicode__(self):
        return "Kaltura: %s" % (self.partner_id)

    def get_owner_display(self):
        return fmt(_('partner id %(partner_id)s',
                     partner_id=self.partner_id))

    def do_update_subtitles(self, video_url, language, tip):
        kaltura_id = video_url.get_video_type().kaltura_id()
        subtitles = tip.get_subtitles()
        sub_data = babelsubs.to(subtitles, 'srt')

        syncing.kaltura.update_subtitles(self.partner_id, self.secret,
                                         kaltura_id, language.language_code,
                                         sub_data)

    def do_delete_subtitles(self, video_url, language):
        kaltura_id = video_url.get_video_type().kaltura_id()
        syncing.kaltura.delete_subtitles(self.partner_id, self.secret,
                                         kaltura_id, language.language_code)

class BrightcoveAccount(ExternalAccount):
    account_type = 'B'
    video_url_type = videos.models.VIDEO_TYPE_BRIGHTCOVE

    publisher_id = models.CharField(max_length=100,
                                    verbose_name=_('Publisher ID'))
    write_token = models.CharField(max_length=100)
    import_feed = models.OneToOneField(VideoFeed, null=True,
                                       on_delete=models.SET_NULL)

    class Meta:
        verbose_name = _('Brightcove account')
        unique_together = [
            ('type', 'owner_id')
        ]

    def __unicode__(self):
        return "Brightcove: %s" % (self.publisher_id)

    def get_owner_display(self):
        return fmt(_('publisher id %(publisher_id)s',
                     publisher_id=self.publisher_id))

    def do_update_subtitles(self, video_url, language, tip):
        video_id = video_url.get_video_type().brightcove_id
        syncing.brightcove.update_subtitles(self.write_token, video_id,
                                            language.video)

    def do_delete_subtitles(self, video_url, language):
        video_id = video_url.get_video_type().brightcove_id
        if language.video.get_merged_dfxp() is not None:
            # There are other languaguages still, we need to update the
            # subtitles by merging those language's DFXP
            syncing.brightcove.update_subtitles(self.write_token, video_id,
                                                language.video)
        else:
            # No languages left, delete the subtitles
            syncing.brightcove.delete_subtitles(self.write_token, video_id)

    def should_skip_syncing(self):
        return self.write_token == ''

    def feed_url(self, player_id, tags):
        url_start = ('http://link.brightcove.com'
                    '/services/mrss/player%s/%s') % (
                        player_id, self.publisher_id)
        if tags is not None:
            return '%s/tags/%s' % (url_start,
                                   '/'.join(quote_plus(t) for t in tags))
        else:
            return url_start + "/new"

    def make_feed(self, player_id, tags=None):
        """Create a feed for this account.

        :returns: True if the feed was changed
        """
        feed_url = self.feed_url(player_id, tags)
        if self.import_feed:
            if feed_url != self.import_feed.url:
                self.import_feed.url = feed_url
                self.import_feed.save()
                return True
        else:
            self.import_feed = VideoFeed.objects.create(
                url=self.feed_url(player_id, tags),
                team=self.team)
            self.save()
            return True
        return False

    def remove_feed(self):
        if self.import_feed:
            self.import_feed.delete();
            self.import_feed = None
            self.save()

    def feed_info(self):
        if self.import_feed is None:
            return None
        path_parts = urlparse.urlparse(self.import_feed.url).path.split("/")
        for part in path_parts:
            if part.startswith("player"):
                player_id = part[len("player"):]
                break
        else:
            raise ValueError("Unable to parse feed URL")

        try:
            i = path_parts.index('tags')
        except ValueError:
            tags = None
        else:
            tags = tuple(path_parts[i+1:])
        return player_id, tags


class YouTubeAccountManager(ExternalAccountManager):
    def _get_sync_account_team_video(self, team_video, video_url):
        query = self.filter(type=ExternalAccount.TYPE_TEAM,
                            channel_id=video_url.owner_username)
        where_sql = (
            '(owner_id = %s OR EXISTS ('
            'SELECT * '
            'FROM externalsites_youtubeaccount_sync_teams '
            'WHERE youtubeaccount_id = externalsites_youtubeaccount.id '
            'AND team_id = %s))'
        )
        query = query.extra(where=[where_sql],
                            params=[team_video.team_id, team_video.team_id])
        return query.get()

    def _get_sync_account_nonteam_video(self, video, video_url):
        return self.get(
            type=ExternalAccount.TYPE_USER,
            channel_id=video_url.owner_username)

    def accounts_to_import(self):
        return self.filter(Q(type=ExternalAccount.TYPE_USER)|
                           Q(import_team__isnull=False))

    def create_or_update(self, channel_id, oauth_refresh_token, **data):
        """Create a new YouTubeAccount, if none exists for the channel_id

        If we already have an account for that channel id, then we don't want
        to create a new account.  Instead, we update the existing account with
        the new refresh token and throw a YouTubeAccountExistsError
        """
        if self.filter(channel_id=channel_id).count() == 0:
            return self.create(channel_id=channel_id,
                               oauth_refresh_token=oauth_refresh_token,
                               **data)
        other_account = self.get(channel_id=channel_id)
        other_account.oauth_refresh_token = oauth_refresh_token
        other_account.save()
        raise YouTubeAccountExistsError(other_account)

class YouTubeAccount(ExternalAccount):
    """YouTube account to sync to.

    Note that we can have multiple youtube accounts for a user/team.  We use
    the username attribute to lookup a specific account for a video.
    """
    account_type = 'Y'
    video_url_type = videos.models.VIDEO_TYPE_YOUTUBE

    channel_id = models.CharField(max_length=255, unique=True)
    username = models.CharField(max_length=255)
    oauth_refresh_token = models.CharField(max_length=255)
    last_import_video_id = models.CharField(max_length=100, blank=True,
                                            default='')
    import_team = models.ForeignKey(Team, null=True)
    sync_teams = models.ManyToManyField(
        Team, related_name='youtube_sync_accounts')

    objects = YouTubeAccountManager()

    class Meta:
        verbose_name = _('YouTube account')
        unique_together = [
            ('type', 'owner_id', 'channel_id'),
        ]

    def __unicode__(self):
        return "YouTube: %s" % (self.username)

    def set_sync_teams(self, user, teams):
        """Set other teams to sync for

        The default for team youtube accounts is to only sync videos if they
        are part of that team.  This method allows for syncing other team's
        videos as well by altering the sync_teams set.

        This method only works for team accounts.  A ValueError will be thrown
        if called for a user account.

        If user is not an admin for this account's team and all the teams
        being set, then PermissionDenied will be thrown.
        """
        if self.type != ExternalAccount.TYPE_TEAM:
            raise ValueError("Non-team account: %s" % self)
        for team in teams:
            if team == self.team:
                raise ValueError("Can't add account owner to sync_teams")
        admin_team_ids = set([m.team_id for m in
                              user.team_members.admins()])
        if self.team.id not in admin_team_ids:
            raise PermissionDenied("%s not an admin for %s" %
                                   (user, self.team))
        for team in teams:
            if team.id not in admin_team_ids:
                raise PermissionDenied("%s not an admin for %s" %
                                       (user, team))
        self.sync_teams = teams

    def feed_url(self):
        return 'https://gdata.youtube.com/feeds/api/users/%s/uploads' % (
            self.channel_id)

    def get_owner_display(self):
        if self.username:
            return self.username
        else:
            return _('No username')

    def should_sync_video_url(self, video, video_url):
        if not (video_url.type == self.video_url_type and
                video_url.owner_username == self.channel_id):
            return False
        if self.type == ExternalAccount.TYPE_USER:
            # for user accounts, match any video
            return True
        else:
            # for team accounts, we need additional checks
            team_video = video.get_team_video()
            if team_video is None:
                return False
            else:
                return (team_video.team_id == self.owner_id or
                        self.sync_teams.filter(id=team_video.team_id).exists())

    def _get_sync_account_nonteam_video(self, video, video_url):
        return self.get(
            type=ExternalAccount.TYPE_USER,
            channel_id=video_url.owner_username)

    def do_update_subtitles(self, video_url, language, version):
        """Do the work needed to update subtitles.

        Subclasses must implement this method.
        """
        access_token = google.get_new_access_token(self.oauth_refresh_token)
        syncing.youtube.update_subtitles(video_url.videoid, access_token,
                                         version)

    def do_delete_subtitles(self, video_url, language):
        access_token = google.get_new_access_token(self.oauth_refresh_token)
        syncing.youtube.delete_subtitles(video_url.videoid, access_token,
                                         language.language_code)

    def delete(self):
        google.revoke_auth_token(self.oauth_refresh_token)
        super(YouTubeAccount, self).delete()

    def should_import_videos(self):
        return (self.type == ExternalAccount.TYPE_USER or
                (self.type == ExternalAccount.TYPE_TEAM and self.import_team))

    def import_videos(self):
        if not self.should_import_videos():
            return
        video_ids = google.get_uploaded_video_ids(self.channel_id)
        if not video_ids:
            return
        for video_id in video_ids:
            if video_id == self.last_import_video_id:
                break
            video_url = 'http://youtube.com/watch?v={}'.format(video_id)
            if self.type == ExternalAccount.TYPE_USER:
                Video.get_or_create_for_url(video_url, user=self.user)
            elif self.import_team:
                video, created = Video.get_or_create_for_url(video_url)
                TeamVideo.objects.create(video=video, team=self.import_team)

        self.last_import_video_id = video_ids[0]
        self.save()

account_models = [
    KalturaAccount,
    BrightcoveAccount,
    YouTubeAccount,
]
_account_type_to_model = dict(
    (model.account_type, model) for model in account_models
)
_video_type_to_account_model = dict(
    (model.video_url_type, model) for model in account_models
)
_account_type_choices = [
    (model.account_type, model._meta.verbose_name)
    for model in account_models
]

def get_account(account_type, account_id):
    AccountModel = _account_type_to_model[account_type]
    try:
        return AccountModel.objects.get(id=account_id)
    except AccountModel.DoesNotExist:
        return None

def account_display(account):
    if account is None:
        return _('deleted account')
    else:
        return unicode(account)

def get_sync_accounts(video):
    """Lookup an external accounts for a given video.

    This function examines the team associated with the video and the set of
    VideoURLs to determine external accounts that we should sync with.

    :returns: list of (account, video_url) tuples
    """
    team_video = video.get_team_video()
    rv = []
    for video_url in video.get_video_urls():
        account = get_sync_account(video, video_url)
        if account is not None:
            rv.append((account, video_url))
    return rv

def get_sync_account(video, video_url):
    video_url.fix_owner_username()
    AccountModel = _video_type_to_account_model.get(video_url.type)
    if AccountModel is None:
        return None
    try:
        return AccountModel.objects.get_sync_account(video, video_url)
    except AccountModel.DoesNotExist:
        return None

def can_sync_videourl(video_url):
    return video_url.type in _video_type_to_account_model

class SyncedSubtitleVersionManager(models.Manager):
    def set_synced_version(self, account, video_url, language, version):
        """Set the synced version for a given account/language."""
        lookup_values = {
            'account_type': account.account_type,
            'account_id': account.id,
            'video_url': video_url,
            'language': language,
        }
        try:
            synced_version = self.get(**lookup_values)
        except SyncedSubtitleVersion.DoesNotExist:
            synced_version = SyncedSubtitleVersion(**lookup_values)
        synced_version.version = version
        synced_version.save()

    def unset_synced_version(self, account, video_url, language):
        """Set the synced version for a given account/language."""
        self.filter(account_type=account.account_type,
                    account_id=account.id,
                    video_url=video_url,
                    language=language).delete()

class SyncedSubtitleVersion(models.Model):
    """Stores the subtitle version that is currently synced to an external
    account.
    """

    account_type = models.CharField(max_length=1,
                                    choices=_account_type_choices)
    account_id = models.PositiveIntegerField()
    video_url = models.ForeignKey(VideoUrl)
    language = models.ForeignKey(SubtitleLanguage, db_index=True)
    version = models.ForeignKey(SubtitleVersion)

    class Meta:
        unique_together = (
            ('account_type', 'account_id', 'video_url', 'language'),
        )

    def __unicode__(self):
        return "SyncedSubtitleVersion: %s %s -> %s (%s)" % (
            self.language.video.video_id,
            self.language.language_code,
            self.version.version_number,
            account_display(self.get_account()))

    objects = SyncedSubtitleVersionManager()

    def get_account(self):
        return get_account(self.account_type, self.account_id)

class SyncHistoryQuerySet(query.QuerySet):
    def fetch_with_accounts(self):
        """Fetch SyncHistory objects and join them to their related accounst

        This reduces the query count if you're going to call get_account() for
        each object in the returned list.
        """
        results = list(self)
        # calculate all account types and ids present in the results
        all_accounts = collections.defaultdict(set)
        for sh in results:
            all_accounts[sh.account_type].add(sh.account_id)
        # do a single lookup for each account type
        account_map = {}
        for account_type, account_ids in all_accounts.items():
            AccountModel = _account_type_to_model[account_type]
            for account in AccountModel.objects.filter(id__in=account_ids):
                account_map[account_type, account.id] = account
        # call cache_account for each result
        for result in results:
            result.cache_account(account_map.get((result.account_type,
                                                  result.account_id)))
        return results

class SyncHistoryManager(models.Manager):
    def get_for_language(self, language):
        return self.filter(language=language).order_by('-id')

    def create_for_success(self, **kwargs):
        sh = self.create(result=SyncHistory.RESULT_SUCCESS, **kwargs)
        # clear the retry flag for this account/language since we just
        # successfully synced.
        self.filter(account_type=sh.account_type, account_id=sh.account_id,
                    video_url=sh.video_url, language=sh.language,
                    retry=True).update(retry=False)
        return sh

    def create_for_error(self, e, **kwargs):
        # for SyncingError, we just use the message directly, since it
        # describes a known failure point, for other errors we convert the
        # object to a string
        if isinstance(e, SyncingError):
            details = e.msg
        else:
            details = str(e)
        if 'retry' not in kwargs:
            kwargs['retry'] = isinstance(e, RetryableSyncingError)
        return self.create(result=SyncHistory.RESULT_ERROR, details=details,
                           **kwargs)

    def create(self, *args, **kwargs):
        if 'datetime' not in kwargs:
            kwargs['datetime'] = now()
        if 'account' in kwargs:
            account = kwargs.pop('account')
            kwargs['account_id'] = account.id
            kwargs['account_type'] = account.account_type
        return models.Manager.create(self, *args, **kwargs)

    def get_query_set(self):
        return SyncHistoryQuerySet(self.model)

    def get_attempts_to_resync(self, team=None, user=None):
        """Lookup failed sync attempt that we should retry,
        for a user or for a team.
        """
        days_of_search = 183
        items_of_search = 20000
        items_to_display = 200
        qs = self
        if team:
            owner = team
        elif user:
            owner = user
        else:
            return None
        accounts = []
        for account_type in [YouTubeAccount, KalturaAccount, BrightcoveAccount]:
            for account_id in account_type.objects.for_owner(owner).values_list('id', flat=True):
                accounts.append(account_id)
        qs = qs.filter(account_id__in=accounts)
        qs = qs.filter(datetime__gt=datetime.datetime.now() - datetime.timedelta(days=days_of_search))
        qs = qs.select_related('language', 'video_url__video').order_by('-id')[:items_of_search]
        keep = []
        seen = set()
        for item in qs:
            if item.language not in seen:
                if (item.result == SyncHistory.RESULT_ERROR) and not item.retry:
                    video_id = item.video_url.video.video_id
                    video_url = item.video_url.url
                    keep.append({'account_type': item.get_account_type_display(),
                                 'id': item.id,
                                 'language_code': item.language.language_code,
                                 'details': item.details,
                                 'video_id': video_id,
                                 'video_url': video_url})
                    if len(keep) >= items_to_display:
                        break
                seen.add(item.language)
        return keep

    def get_attempt_to_resync(self):
        """Lookup failed sync attempt that we should retry.

        Returns:
            SyncHistory object to retry or None if there are no sync attempts
            to retry.  We will clear the retry flag before returning the
            SyncHistory object.
        """
        qs = self.filter(retry=True)[:1]
        try:
            sh = qs.select_related('video_url', 'language').get()
        except SyncHistory.DoesNotExist:
            return None
        sh.retry = False
        sh.save()
        return sh

    def force_retry(self, pk, team=None, user=None):
        try:
            sh = self.get(pk=pk)
        except SyncHistory.DoesNotExist:
            return None
        if team is not None:
            if sh.video_url.video.get_team_video() and sh.video_url.video.get_team_video().team == team:
                sh.retry = True
                sh.save()
        elif user is not None:
            if can_user_resync_own_video(sh.video_url.video, user):
                sh.retry = True
                sh.save()

class SyncHistory(models.Model):
    """History of all subtitle sync attempts."""

    ACTION_UPDATE_SUBTITLES = 'U'
    ACTION_DELETE_SUBTITLES = 'D'
    ACTION_CHOICES = (
        (ACTION_UPDATE_SUBTITLES, 'Update Subtitles'),
        (ACTION_DELETE_SUBTITLES, 'Delete Subtitles'),
    )

    RESULT_SUCCESS = 'S'
    RESULT_ERROR = 'E'

    RESULT_CHOICES = (
        (RESULT_SUCCESS, _('Success')),
        (RESULT_ERROR, _('Error')),
    )

    account_type = models.CharField(max_length=1,
                                    choices=_account_type_choices)
    account_id = models.PositiveIntegerField()
    video_url = models.ForeignKey(VideoUrl)
    language = models.ForeignKey(SubtitleLanguage, db_index=True)
    action = models.CharField(max_length=1, choices=ACTION_CHOICES)
    datetime = models.DateTimeField()
    version = models.ForeignKey(SubtitleVersion, null=True, blank=True)
    result = models.CharField(max_length=1, choices=RESULT_CHOICES)
    details = models.CharField(max_length=255, blank=True, default='')
    # should we try to resync these subtitles?
    retry = models.BooleanField(default=False)

    objects = SyncHistoryManager()

    class Meta:
        verbose_name = verbose_name_plural = _('Sync history')

    def __unicode__(self):
        return "SyncHistory: %s - %s for %s (%s)" % (
            self.datetime.date(),
            self.get_action_display(),
            account_display(self.get_account()),
            self.get_result_display())

    def get_account(self):
        if not hasattr(self, '_account'):
            self._account = get_account(self.account_type, self.account_id)
        return self._account

    def cache_account(self, account):
        self._account = account

class CreditedVideoUrl(models.Model):
    """Track videos that we have added our amara credit to.

    This model is pretty simple.  If a VideoUrl exists in the table, then
    we've added our amara credit to it and we shouldn't try to add it again.
    """

    video_url = models.ForeignKey(VideoUrl, primary_key=True)

class OpenIDConnectLink(models.Model):
    """Link a user to an OpenID Connect ID."""
    sub = models.CharField(max_length=255, primary_key=True)
    user = models.OneToOneField(User, related_name='openid_connect_link')

    def __unicode__(self):
        return u'OpenIDConnectLink: {}'.format(self.user.username)
