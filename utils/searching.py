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

quote_re = re.compile(r'(["\'])')

def get_terms(query):
    """Return a list of search terms from a query."""

    terms = []
    pos = 0
    def add_unquoted_term(t):
        terms.extend(t.split())
    while pos < len(query):
        m = quote_re.search(query, pos)
        if not m:
            add_unquoted_term(query[pos:])
            break
        if m.start() > pos:
            add_unquoted_term(query[pos:m.start()])
        after_quote_pos = m.start()+1
        try:
            end_quote_pos = query.index(m.group(1), after_quote_pos)
        except ValueError:
            add_unquoted_term(query[pos+1:])
            break
        terms.append(query[after_quote_pos:end_quote_pos])
        pos = end_quote_pos + 1
    return [t for t in terms if t]
