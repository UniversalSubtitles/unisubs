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

import re

def get_terms(query):
    """Return a list of search terms from a query.

    If there is a set of " chars, then we will treat the contents as one term
    """
    terms = []
    pos = 0
    def add_unquoted_term(t):
        terms.extend(t.split())
    while pos < len(query):
        try:
            quote_start = query.index(u'"', pos)
        except ValueError:
            add_unquoted_term(query[pos:])
            break
        if quote_start > pos:
            add_unquoted_term(query[pos:quote_start])
        pos = quote_start + 1
        try:
            quote_end = query.index(u'"', pos)
        except ValueError:
            add_unquoted_term(query[pos:])
            break
        terms.append(query[pos:quote_end])
        pos = quote_end + 1
    return [t for t in terms if t]
