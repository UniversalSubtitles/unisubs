# -*- coding: utf-8 -*-
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

# Django settings for unisubs project.
import os, sys
from datetime import datetime

from django.conf import global_settings
from unilangs import get_language_code_mapping

import optionalapps

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DEFAULT_PROTOCOL  = 'http'

def rel(*x):
    return os.path.join(PROJECT_ROOT, *x)

# Rebuild the language dicts to support more languages.

# We use a custom format for our language labels:
# Translated Language Name (Native Name)
#
# For example: if you are an English user you'll see something like:
# French (Français)
language_choices = [(code,
                     u'%s' % lc.name())
                    for code, lc in get_language_code_mapping('unisubs').items()]

global_settings.LANGUAGES = ALL_LANGUAGES = language_choices

# Languages representing metadata
METADATA_LANGUAGES = (
    ('meta-tw', 'Metadata: Twitter'),
    ('meta-geo', 'Metadata: Geo'),
    ('meta-wiki', 'Metadata: Wikipedia'),
)


DEBUG = True
TEMPLATE_DEBUG = DEBUG

PISTON_EMAIL_ERRORS = True
PISTON_DISPLAY_ERRORS = False

ADMINS = (
    # ('Your Name', 'your_email@domain.com'),
)

ALARM_EMAIL = None
MANAGERS = ADMINS

P3P_COMPACT = 'CP="CURa ADMa DEVa OUR IND DSP CAO COR"'

DEFAULT_FROM_EMAIL = '"Amara" <feedback@universalsubtitles.org>'
WIDGET_LOG_EMAIL = 'widget-logs@universalsubtitles.org'

BILLING_CUTOFF = datetime(2013, 3, 1, 0, 0, 0)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3', # Add 'postgresql_psycopg2', 'postgresql', 'mysql', 'sqlite3' or 'oracle'.
        'NAME': rel('unisubs.sqlite3'), # Or path to database file if using sqlite3.
        'USER': '',                      # Not used with sqlite3.
        'PASSWORD': '',                  # Not used with sqlite3.
        'HOST': '',                      # Set to empty string for localhost. Not used with sqlite3.
        'PORT': '',                      # Set to empty string for default. Not used with sqlite3.
    }
}

CSS_USE_COMPILED = True

COMPRESS_YUI_BINARY = "java -jar ./css-compression/yuicompressor-2.4.6.jar"
COMPRESS_OUTPUT_DIRNAME = "static-cache"


USER_LANGUAGES_COOKIE_NAME = 'unisub-languages-cookie'

# Local time zone for this installation. Choices can be found here:
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# although not all choices may be available on all operating systems.
# If running in a Windows environment this must be set to the same as your
# system time zone.
TIME_ZONE = 'America/Chicago'

# Language code for this installation. All choices can be found here:
# http://www.i18nguy.com/unicode/language-identifiers.html
LANGUAGE_CODE = 'en-us'

SITE_ID = 1

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery.
USE_I18N = True

# Absolute path to the directory that holds media.
# Example: "/home/media/media.lawrence.com/"
STATIC_ROOT = rel('media')+'/'
MEDIA_ROOT  = rel('user-data')+'/'

# List of callables that know how to import templates from various sources.
TEMPLATE_LOADERS = (
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader'
)


MIDDLEWARE_CLASSES = (
    'middleware.StripGoogleAnalyticsCookieMiddleware',
    'utils.ajaxmiddleware.AjaxErrorMiddleware',
    'localeurl.middleware.LocaleURLMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'auth.middleware.AmaraAuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'openid_consumer.middleware.OpenIDMiddleware',
    'middleware.P3PHeaderMiddleware',
    'middleware.UserUUIDMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'api.middleware.CORSMiddleware',
)

ROOT_URLCONF = 'urls'

TEMPLATE_DIRS = (
    # Put strings here, like "/home/html/django_templates" or "C:/www/django/templates".
    # Always use forward slashes, even on Windows.
    # Don't forget to use absolute paths, not relative paths.
   rel('templates'),
)

TEMPLATE_CONTEXT_PROCESSORS = (
    'django.contrib.auth.context_processors.auth',
    'django.core.context_processors.debug',
    'django.core.context_processors.request',
    'utils.context_processors.current_site',
    'utils.context_processors.current_commit',
    'utils.context_processors.custom',
    'utils.context_processors.user_languages',
    'utils.context_processors.run_locally',
    'utils.context_processors.experiments',
    'django.contrib.messages.context_processors.messages',
    'django.core.context_processors.i18n',
    'staticmedia.context_processors.staticmedia',
)

INSTALLED_APPS = (
    # this needs to be first, yay for app model loading mess
    'auth',
    # django stock apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.sitemaps',
    'django.contrib.sites',
    'django.contrib.webdesign',
    # third party apps
    'djcelery',
    'raven.contrib.django.raven_compat',
    'south',
    'rest_framework',
    'tastypie',
    # third party apps forked on our repo
    'localeurl',
    'openid_consumer',
    'socialauth',
    # our apps
    'accountlinker',
    'amaradotorg',
    'amaracelery',
    'api',
    'caching',
    'comments',
    'externalsites',
    'messages',
    'profiles',
    'search',
    'staticmedia',
    'teams',
    'testhelpers',
    'thirdpartyaccounts',
    'unisubs_compressor',
    'utils',
    'videos',
    'widget',
    'subtitles',
)

STARTUP_MODULES = [
    'externalsites.signalhandlers',
]

# Celery settings

# import djcelery
# djcelery.setup_loader()

# For running worker use: python manage.py celeryd -E --concurrency=10 -n worker1.localhost
# Run event cather for monitoring workers: python manage.py celerycam --frequency=5.0
# This allow know are workers online or not: python manage.py celerybeat

CELERY_IGNORE_RESULT = True
CELERY_SEND_EVENTS = False
CELERY_SEND_TASK_ERROR_EMAILS = True
BROKER_POOL_LIMIT = 10

REST_FRAMEWORK = {
    'DEFAULT_PARSER_CLASSES': (
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.YAMLParser',
        'rest_framework.parsers.XMLParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.MultiPartParser',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.YAMLRenderer',
        'api.renderers.AmaraBrowsableAPIRenderer',
        'rest_framework.renderers.XMLRenderer',
    ),
    'URL_FORMAT_OVERRIDE': 'format',
    'DEFAULT_CONTENT_NEGOTIATION_CLASS':
        'api.negotiation.AmaraContentNegotiation',
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'api.auth.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_SERIALIZER_CLASS':
        'api.pagination.AmaraPaginationSerializer',
    'ORDERING_PARAM': 'order_by',
    'VIEW_NAME_FUNCTION': 'api.viewdocs.amara_get_view_name',
    'VIEW_DESCRIPTION_FUNCTION': 'api.viewdocs.amara_get_view_description',
}

#################

import re
LOCALE_INDEPENDENT_PATHS = [
    re.compile('^/media/'),
    re.compile('^/widget/'),
    re.compile('^/api/'),
    re.compile('^/api2/'),
    re.compile('^/jstest/'),
    re.compile('^/sitemap.*.xml'),
    re.compile('^/externalsites/youtube-callback'),
    re.compile('^/crossdomain.xml'),
    re.compile('^/embedder-widget-iframe/'),
]

# socialauth-related
OPENID_REDIRECT_NEXT = '/socialauth/openid/done/'

OPENID_SREG = {"required": "nickname, email", "optional":"postcode, country", "policy_url": ""}
OPENID_AX = [{"type_uri": "http://axschema.org/contact/email", "count": 1, "required": True, "alias": "email"},
             {"type_uri": "fullname", "count": 1 , "required": False, "alias": "fullname"}]

FACEBOOK_API_KEY = ''
FACEBOOK_SECRET_KEY = ''

VIMEO_API_KEY = None
VIMEO_API_SECRET = None

# NOTE: all of these backends store the User.id value in the session data,
# which we rely on in AmaraAuthenticationMiddleware.  Other backends should
# use the same system.
AUTHENTICATION_BACKENDS = (
   'auth.backends.CustomUserBackend',
   'externalsites.auth_backends.OpenIDConnectBackend',
   'thirdpartyaccounts.auth_backends.TwitterAuthBackend',
   'thirdpartyaccounts.auth_backends.FacebookAuthBackend',
   'auth.backends.OpenIdBackend',
   'django.contrib.auth.backends.ModelBackend',
)

SKIP_SOUTH_TESTS = True
SOUTH_TESTS_MIGRATE = False

LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/'

AUTH_PROFILE_MODULE = 'profiles.Profile'
ACCOUNT_ACTIVATION_DAYS = 9999 # we are using registration only to verify emails
SESSION_COOKIE_AGE = 2419200 # 4 weeks

SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'
SESSION_COOKIE_HTTPONLY = False

RECENT_ACTIVITIES_ONPAGE = 10
ACTIVITIES_ONPAGE = 20
REVISIONS_ONPAGE = 20

FEEDBACK_EMAIL = 'socmedia@pculture.org'
FEEDBACK_EMAILS = [FEEDBACK_EMAIL]
FEEDBACK_ERROR_EMAIL = 'universalsubtitles-errors@pculture.org'
FEEDBACK_SUBJECT = 'Amara Feedback'
FEEDBACK_RESPONSE_SUBJECT = 'Thanks for trying Amara'
FEEDBACK_RESPONSE_EMAIL = 'universalsubtitles@pculture.org'
FEEDBACK_RESPONSE_TEMPLATE = 'feedback_response.html'

#teams
TEAMS_ON_PAGE = 12

PROJECT_VERSION = '0.5'

EDIT_END_THRESHOLD = 120

ANONYMOUS_USER_ID = 10000

#Use on production
GOOGLE_ANALYTICS_NUMBER = 'UA-163840-22'
EXPERIMENTS_CODE = "QL2-1BUpSyeABVHp9b6G8w"
MIXPANEL_TOKEN = '44205f56e929f08b602ccc9b4605edc3'

try:
    from commit import LAST_COMMIT_GUID
except ImportError:
    sys.stderr.write("deploy/create_commit_file must be ran before boostrapping django")
    LAST_COMMIT_GUID = "dev"

AWS_ACCESS_KEY_ID = ''
AWS_SECRET_ACCESS_KEY = ''
DEFAULT_BUCKET = ''
AWS_USER_DATA_BUCKET_NAME  = ''
STATIC_MEDIA_USES_S3 = USE_AMAZON_S3 = False
STATIC_MEDIA_COMPRESSED = True

AVATAR_MAX_SIZE = 500*1024
THUMBNAILS_SIZE = (
    (100, 100),
    (50, 50),
    (120, 90),
    (240, 240)
)

EMAIL_BCC_LIST = []

CACHE_BACKEND = 'locmem://'

#for unisubs.example.com
RECAPTCHA_PUBLIC = '6LdoScUSAAAAANmmrD7ALuV6Gqncu0iJk7ks7jZ0'
RECAPTCHA_SECRET = ' 6LdoScUSAAAAALvQj3aI1dRL9mHgh85Ks2xZH1qc'

ROSETTA_EXCLUDED_APPLICATIONS = (
    'openid_consumer',
    'rosetta'
)

INSTALLED_APPS += optionalapps.get_apps()

MEDIA_BUNDLES = {
    "base.css": {
        "files": (
            "css/jquery.jgrowl.css",
            "css/jquery.alerts.css",
            "css/960.css",
            "css/reset.css",
            "css/html.css",
            "css/about_faq.css",
            "css/breadcrumb.css",
            "css/buttons.css",
            "css/chosen.css",
            "css/classes.css",
            "css/forms.css",
            "css/index.css",
            "css/layout.css",
            "css/profile_pages.css",
            "css/revision_history.css",
            "css/teams.css",
            "css/transcripts.css",
            "css/background.css",
            "css/activity_stream.css",
            "css/settings.css",
            "css/feedback.css",
            "css/messages.css",
            "css/global.css",
            "css/top_user_panel.css",
            "css/services.css",
            "css/solutions.css",
            "css/watch.css",
            "css/v1.scss",
            "css/bootstrap.css",
        ),
    },
    "new-base.css": {
        "files": [
            'src/css/site/colors.scss',
            'src/css/site/layout.scss',
            'src/css/site/type.scss',
            'src/css/site/buttons.scss',
            'src/css/site/forms.scss',
            'src/css/site/links.scss',
            'src/css/site/lists.scss',
            'src/css/site/cards.scss',
            'src/css/site/tables.scss',
            'src/css/site/graphs.scss',
            'src/css/site/header.scss',
            'src/css/site/tabs.scss',
            'src/css/site/split-view.scss',
            'src/css/site/bottom-sheet.scss',
            'src/css/site/pagination.scss',
            'src/css/site/menus.scss',
            'src/css/site/modals.scss',
            'src/css/site/tooltips.scss',
            'src/css/site/banner.scss',
            'src/css/site/messages.scss',
            'src/css/site/footer.scss',
            'src/css/site/teams.scss',
            'src/css/third-party/jquery-ui-1.11.4.custom.css',
            'src/css/third-party/jquery-ui.theme-1.11.4.custom.css',
            'src/css/third-party/jquery-ui.structure-1.11.4.custom.css',
        ],
        "include_path": 'src/css/site',
    },
    "home.css": {
        "files": (
            "css/new_index.css",
        ),
    },
    "hands_home.css": {
        "files": (
            "css/hands-static.css",
            "css/hands-main.css",
         )
    },
    "api.css": {
        "files": (
            "src/css/api.css",
        ),
    },
    "hands_home.js": {
        "files": (
            "js/hands-plugins.js",
            "js/hands-modernizr-2.6.2.min.js",
         )
    },
    "site.js": {
        "files": (
            "js/jquery-1.4.3.js",
            "js/jquery-ui-1.8.16.custom.min.js",
            "js/jgrowl/jquery.jgrowl.js",
            "js/jalerts/jquery.alerts.js",
            "js/jquery.form.js",
            "js/jquery.metadata.js",
            "js/jquery.mod.js",
            "js/jquery.rpc.js",
            "js/jquery.input_replacement.min.js",
            "js/messages.js",
            "js/escape.js",
            "js/libs/chosen.jquery.min.js",
            "js/libs/chosen.ajax.jquery.js",
            "js/libs/jquery.cookie.js",
            "js/unisubs.site.js",
            "src/js/unisubs.variations.js",
        ),
    },
    "new-site.js": {
        "files": [
            'src/js/third-party/jquery-2.1.3.js',
            'src/js/third-party/jquery-ui-1.11.4.custom.js',
            'src/js/third-party/jquery.form.js',
            'src/js/third-party/behaviors.js',
            'src/js/site/menus.js',
            'src/js/site/modals.js',
            'src/js/site/querystring.js',
            'src/js/site/tooltips.js',
            'src/js/site/pagination.js',
            'src/js/site/autocomplete.js',
            'src/js/site/thumb-lists.js',
            'src/js/site/bottom-sheet.js',
            'src/js/site/team-videos.js',
            'src/js/site/team-bulk-move.js',
            'src/js/site/team-integration-settings.js',
            'src/js/site/dates.js',
        ],
    },
    "api.js": {
        "files": (
            "js/jquery-1.4.3.js",
            "src/js/api.js",
        ),
    },
    "graphs.js": {
        "files": (
            "js/libs/pygal-tooltips.js",
            "js/libs/svg.jquery.js",
        ),
    },
    "teams.js": {
        "files": (
            "js/libs/ICanHaz.js",
            "js/libs/classy.js",
            "js/libs/underscore.js",
            "js/libs/chosen.jquery.min.js",
            "js/libs/chosen.ajax.jquery.js",
            "js/jquery.mod.js",
            "js/teams/create-task.js",
         ),
    },
    'editor.js':  {
        'files': (
            'src/js/third-party/jquery-1.10.1.js',
            'js/jquery.form.js',
            'src/js/third-party/jquery.autosize.js',
            'src/js/third-party/angular.1.2.9.js',
            'src/js/third-party/angular-cookies.js',
            'src/js/third-party/underscore.1.4.4.js',
            'src/js/third-party/popcorn.js',
            'src/js/third-party/Blob.js',
            'src/js/third-party/FileSaver.js',
            'src/js/third-party/popcorn.brightcove.js',
            'src/js/third-party/modal-helper.js',
            'src/js/third-party/json2.min.js',
            'src/js/dfxp/dfxp.js',
            'src/js/uri.js',
            'src/js/popcorn/popcorn.flash-fallback.js',
            #'src/js/popcorn/popcorn.netflix.js',
            'src/js/subtitle-editor/app.js',
            'src/js/subtitle-editor/dom.js',
            'src/js/subtitle-editor/help.js',
            'src/js/subtitle-editor/lock.js',
            'src/js/subtitle-editor/preferences.js',
            'src/js/subtitle-editor/modal.js',
            'src/js/subtitle-editor/notes.js',
            'src/js/subtitle-editor/blob.js',
            'src/js/subtitle-editor/session.js',
            'src/js/subtitle-editor/workflow.js',
            'src/js/subtitle-editor/subtitles/controllers.js',
            'src/js/subtitle-editor/subtitles/directives.js',
            'src/js/subtitle-editor/subtitles/filters.js',
            'src/js/subtitle-editor/subtitles/models.js',
            'src/js/subtitle-editor/subtitles/services.js',
            'src/js/subtitle-editor/timeline/controllers.js',
            'src/js/subtitle-editor/timeline/directives.js',
            'src/js/subtitle-editor/video/controllers.js',
            'src/js/subtitle-editor/video/directives.js',
            'src/js/subtitle-editor/video/services.js',
        ),
    },
    'editor.css':  {
        'files': (
            'src/css/third-party/reset.css',
            'src/css/subtitle-editor/subtitle-editor.scss',
        ),
    },
    "embedder.js":{
        "files": (
            "src/js/third-party/json2.min.js",
            'src/js/third-party/underscore.min.js',
            'src/js/third-party/jquery-1.8.3.min.js',
            'src/js/third-party/backbone.min.js',
            'src/js/third-party/popcorn.js',
            'src/js/third-party/popcorn.brightcove.js',
            'src/js/popcorn/popcorn.flash-fallback.js',
            'src/js/third-party/jquery.mCustomScrollbar.concat.min.js',
            'src/js/popcorn/popcorn.amaratranscript.js',
            'src/js/popcorn/popcorn.amarasubtitle.js',
            'src/js/embedder/embedder.js'
        ),
        'add_amara_conf': True,
    },
    "embedder.css": {
        "files": (
            "src/css/embedder/jquery.mCustomScrollbar.css",
            "src/css/embedder/embedder.scss",
        ),
    },
    'ie8.css': {
        'files': (
            'css/ie8.css',
        ),
    },
    'ajax-paginator.js': {
        'files': (
            'js/jquery.address-1.4.fixed.js',
            'js/escape.js',
            'js/jquery.ajax-paginator.js',
        ),
    },
    'prepopulate.js': {
        'files': (
            'js/urlify.js',
            'js/prepopulate.min.js',
        ),
    },
    # used by the old editor
    'unisubs-api.js': {
        'files': (
            'legacy-js/unisubs-api.js',
        ),
    },
    # used by the old embedder -- hopefully going away soon
    'unisubs-offsite-compiled.js': {
        'files': (
            'legacy-js/unisubs-offsite-compiled.js',
        ),
    },
    # used by both the old embedder and old editor
    "widget.css": {
        "files": (
            "css/unisubs-widget.css",
        ),
    },
}

# Where we should tell OAuth providers to redirect the user to.  We want to
# use https for production to prevent attackers from seeing the access token.
# For development, we care less about that so we typically use http
OAUTH_CALLBACK_PROTOCOL = 'https'

EMAIL_BACKEND = "utils.safemail.InternalOnlyBackend"
EMAIL_FILE_PATH = '/tmp/unisubs-messages'
# on staging and dev only the emails listed bellow will receive actual mail
EMAIL_NOTIFICATION_RECEIVERS = ("arthur@stimuli.com.br", "steve@stevelosh.com", "@pculture.org")
# If True will not try to load media (e.g. javascript files) from third parties.
# If you're developing and have no net access, enable this setting on your
# settings_local.py
RUN_LOCALLY = False

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'root': {
        'level': 'WARNING',
        'handlers': ['console', 'sentry'],
    },
    'formatters': {
        'verbose': {
            'format': '%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s'
        },
    },
    'handlers': {
        'null': {
            'level':'DEBUG',
            'class':'django.utils.log.NullHandler',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose'
        },
        'sentry': {
            'level': 'WARN',
            'class': 'raven.contrib.django.handlers.SentryHandler',
        },
    },
    'loggers': {
        'django.db.backends': {
            'level': 'ERROR',
            'handlers': ['console'],
            'propagate': False,
        },
        'raven': {
            'level': 'DEBUG',
            'handlers': ['console'],
            'propagate': False,
        },
        'sentry.errors': {
            'level': 'DEBUG',
            'handlers': ['console'],
            'propagate': False,
        },
        'bleach': {
            'level': 'ERROR',
            'handlers': ['null'],
            'propagate': False,
        },
        'api': {
            'level': 'INFO',
            'handlers': ['sentry', 'console'],
            'propagate': False
        },
        'utils.youtube': {
            'level': 'INFO',
            'handlers': ['sentry', 'console'],
            'propagate': False
        },
        'timing': {
            'level': 'DEBUG',
            'handlers': ['console'],
            'propagate': False
        },
    },
}

TMP_FOLDER = "/tmp/"

from task_settings import *

if DEBUG:
    try:
        import debug_toolbar
    except ImportError:
        pass
    else:
        INSTALLED_APPS += ('debug_toolbar',)
        MIDDLEWARE_CLASSES = (
            ('debug_toolbar.middleware.DebugToolbarMiddleware',) +
            MIDDLEWARE_CLASSES
        )
        DEBUG_TOOLBAR_PATCH_SETTINGS = False

        DEBUG_TOOLBAR_PANELS = (
            'debug_toolbar.panels.timer.TimerPanel',
            'debug_toolbar.panels.request.RequestPanel',
            'debug_toolbar.panels.templates.TemplatesPanel',
            'debug_toolbar.panels.sql.SQLPanel',
            'caching.debug_toolbar_panels.CachePanel',
        )

        def custom_show_toolbar(request):
            return 'debug_toolbar' in request.GET

        DEBUG_TOOLBAR_CONFIG = {
            'INTERCEPT_REDIRECTS': False,
            'SHOW_TOOLBAR_CALLBACK': 'settings.custom_show_toolbar',
            'EXTRA_SIGNALS': [],
            'HIDE_DJANGO_SQL': False,
            'TAG': 'div',
        }

optionalapps.exec_repository_scripts('settings_extra.py', globals(), locals())
