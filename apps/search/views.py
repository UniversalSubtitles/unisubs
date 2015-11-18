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
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.utils.http import urlencode

from search.forms import SearchForm
from search.rpc import SearchApiClass
from utils import render_to
from utils.context_processors import current_site
from utils.rpc import RpcRouter


rpc_router = RpcRouter('search:rpc_router', {
    'SearchApi': SearchApiClass()
})

@render_to('search/search.html')
def index(request):
    if request.GET:
        site = current_site(request)
        query = {}
        for k,v in request.GET.items():
            query[k] = v
        # If we're at a URL with query params we just got here from a search
        # form on another page.  If that's the case, we'll redirect to the
        # AJAX-style URL with the params in the hash.  Then that page will take
        # the other branch of this if, and the search form will work its
        # frontend magic.
        url = '%s%s#/?%s' % (
            # Safari/WebKit seem to need this to work properly when redirecting
            # over HTTPS.  See commit 92de5dd6c4969c4c4a3d5d1422fb9caf5e42f345.
            site['BASE_URL'],
            reverse('search:index'),
            urlencode(query)
        )

        return HttpResponseRedirect(url)
    else:
        return {
            'form': SearchForm(),
        }

