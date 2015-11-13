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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

from optparse import make_option

from django.core.management.base import BaseCommand

from videos.models import VideoIndex

class Command(BaseCommand):
    help = "Adds indexes that have to be defined with raw SQL commands"
    option_list = BaseCommand.option_list + (
        make_option('-a', '--all',
                    action='store_true',
                    dest='all',
                    default=False,
                    help='Reindex all videos'),
    )

    def handle(self, **options):
        VideoIndex.objects.create_missing()
        if options['all']:
            VideoIndex.objects.all().update(needs_update=True)
        while not VideoIndex.objects.all_indexed():
            VideoIndex.objects.index_videos(100)
