from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from auth.models import CustomUser as User
from utils.graphing import plot
from utils import DEFAULT_PROTOCOL
from datetime import datetime
from django.utils.timezone import utc

def compute_statistics(team, stats_type):
    """computes a bunch of statistics for the team, either at
    the video or member levels.
    """
    from views import TableCell, ALL_LANGUAGES_DICT
    summary = ''
    graph = ''
    graph_recent = ''
    summary_recent = ''
    graph_additional = ''
    graph_additional_recent = ''
    summary_additional = ''
    summary_additional_recent = ''
    summary_table = ''
    if stats_type == 'videosstats':
        (complete_languages, incomplete_languages) = team.get_team_languages()
        languages = complete_languages + incomplete_languages
        unique_languages = set(languages)
        total = 0
        numbers = []
        y_title = "Number of edited subtitles"
        for l in unique_languages:
            count_complete = complete_languages.count(l)
            count_incomplete = incomplete_languages.count(l)
            numbers.append((ALL_LANGUAGES_DICT[l], count_complete + count_incomplete, "Published: %s, total edits:" % count_complete))
            total += count_complete + count_incomplete
        summary = 'Top languages (all time)'
        title = ""
        graph = plot(numbers, title=title, graph_type='HorizontalBar', labels=True, max_entries=20, y_title=y_title)

        (complete_languages_recent, incomplete_languages_recent, new_languages) = team.get_team_languages(since=30)
        languages_recent = complete_languages_recent + incomplete_languages_recent
        unique_languages_recent = set(languages_recent)
        summary_recent = "Top languages (past 30 days)"
        numbers_recent = []
        total_recent = 0
        for l in unique_languages_recent:
            count_complete_recent = complete_languages_recent.count(l)
            count_incomplete_recent = incomplete_languages_recent.count(l)
            numbers_recent.append((ALL_LANGUAGES_DICT[l], count_complete_recent + count_incomplete_recent, "Published: %s, total edits:" % count_complete_recent))
            total_recent += count_complete_recent + count_incomplete_recent
        title_recent = ""
        graph_recent = plot(numbers_recent, title=title_recent, graph_type='HorizontalBar', labels=True, max_entries=20, y_title=y_title)

        summary_table = []
        summary_table.append([TableCell("", header=True), TableCell("all time", header=True), TableCell("past 30 days", header=True)])
        summary_table.append([TableCell("videos added", header=True), TableCell(str(team.videos_count)), TableCell(str(team.videos_count_since(30)))])
        summary_table.append([TableCell("languages edited", header=True), TableCell(str(len(unique_languages))), TableCell(str(len(unique_languages_recent)))])
        summary_table.append([TableCell("subtitles edited", header=True), TableCell(str(total)), TableCell(str(total_recent))])

    elif stats_type == 'teamstats':
        languages = list(team.languages())
        unique_languages = set(languages)
        summary = u'Members by language (all time)'
        numbers = []
        for l in unique_languages:
            numbers.append((ALL_LANGUAGES_DICT[l], languages.count(l), ALL_LANGUAGES_DICT[l]))
        title = ''
        graph = plot(numbers, graph_type='HorizontalBar', title=title, max_entries=25, labels=True, total_label="Members: ")
        languages_recent = list(team.languages(members_joined_since=30))
        unique_languages_recent = set(languages_recent)
        summary_recent = u'New members by language (past 30 days)'
        numbers_recent = []
        for l in unique_languages_recent:
            numbers_recent.append(
                (ALL_LANGUAGES_DICT[l],
                 languages_recent.count(l),
                 ALL_LANGUAGES_DICT[l],
                 "%s://%s%s" % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, reverse('teams:members', args=[], kwargs={'slug': team.slug}) + "?sort=-joined&lang=%s" % l))
                )
        title_recent = ''
        graph_recent = plot(numbers_recent, graph_type='HorizontalBar', title=title_recent, max_entries=25, labels=True, xlinks=True, total_label="Members: ")

        summary_table = []
        summary_table.append([TableCell("", header=True), TableCell("all time", header=True), TableCell("past 30 days", header=True)])
        summary_table.append([TableCell("members joined", header=True), TableCell(str(team.members_count)), TableCell(str(team.members_count_since(30)))])
        summary_table.append([TableCell("member languages", header=True), TableCell(str(len(unique_languages))), TableCell(str(len(unique_languages_recent)))])

        active_users = {}
        for sv in team.active_users():
            if sv[0] in active_users:
                active_users[sv[0]].add(sv[1])
            else:
                active_users[sv[0]] = set([sv[1]])

        most_active_users = active_users.items()
        most_active_users.sort(reverse=True, key=lambda x: len(x[1]))
        if len(most_active_users) > 20:
            most_active_users = most_active_users[:20]

        active_users_recent = {}
        for sv in team.active_users(since=30):
            if sv[0] in active_users_recent:
                active_users_recent[sv[0]].add(sv[1])
            else:
                active_users_recent[sv[0]] = set([sv[1]])

        most_active_users_recent = active_users_recent.items()
        most_active_users_recent.sort(reverse=True, key=lambda x: len(x[1]))
        if len(most_active_users_recent) > 20:
            most_active_users_recent = most_active_users_recent[:20]

        def displayable_user(user, users_details):
            user_details = users_details[user[0]]
            return ("%s %s (%s)" % (user_details[1], user_details[2], user_details[3]),
                    len(user[1]),
                    "%s %s (%s)" % (user_details[1], user_details[2], user_details[3]),
                    "%s://%s%s" % (DEFAULT_PROTOCOL, Site.objects.get_current().domain, reverse("profiles:profile", kwargs={'user_id': str(user[0])}))
            )

        user_details = User.displayable_users(map(lambda x: int(x[0]), most_active_users))
        user_details_dict = {}
        for user in user_details:
            user_details_dict[user[0]] = user

        most_active_users = map(lambda x: displayable_user(x, user_details_dict), most_active_users)

        summary_additional = u'Top contributors (all time)'
        graph_additional = plot(most_active_users, graph_type='HorizontalBar', title='', labels=True, xlinks=True, total_label="Contributions: ")


        user_details_recent = User.displayable_users(map(lambda x: int(x[0]), most_active_users_recent))
        user_details_dict_recent = {}
        for user in user_details_recent:
            user_details_dict_recent[user[0]] = user

        most_active_users_recent = map(lambda x: displayable_user(x, user_details_dict_recent), most_active_users_recent)

        summary_additional_recent = u'Top contributors (past 30 days)'
        graph_additional_recent = plot(most_active_users_recent, graph_type='HorizontalBar', title='', labels=True, xlinks=True, total_label="Contributions: ")

    statistics = {
        'computed_on': datetime.utcnow().replace(tzinfo=utc).strftime("%A %d. %B %Y %H:%M:%S UTC"),
        'summary': summary,
        'summary_recent': summary_recent,
        'activity_tab': stats_type,
        'graph': graph,
        'graph_recent': graph_recent,
        'graph_additional': graph_additional,
        'graph_additional_recent': graph_additional_recent,
        'summary_additional': summary_additional,
        'summary_additional_recent': summary_additional_recent,
        'summary_table': summary_table,
    }
    return statistics
