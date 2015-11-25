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

from django.test import TestCase
from nose.tools import *

from utils.searching import get_terms

class GetTermsTest(TestCase):
    def test_simple(self):
        assert_equal(get_terms('dog cat'), ['dog', 'cat'])

    def test_quotes(self):
        assert_equal(get_terms('"dog cat" bear'), ['dog cat', 'bear'])
        assert_equal(get_terms("'dog cat' bear"), ['dog cat', 'bear'])
        assert_equal(get_terms("wolf 'dog cat' bear"), ['wolf', 'dog cat', 'bear'])

    def test_unmatched_quote(self):
        # if a quote isn't matched, then just ignore it
        assert_equal(get_terms("'dog cat"), ['dog', 'cat'])

    def test_nested(self):
        # If a single quote is nested inside a double quote, then keep it (and
        # vice-versa)
        assert_equal(get_terms("'dog cat\"' \"'bear'\""), ['dog cat"', "'bear'"])
