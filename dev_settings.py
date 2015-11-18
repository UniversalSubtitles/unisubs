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

from datetime import timedelta
from settings import *
import logging
import os

SITE_ID = 1
SITE_NAME = 'unisubs-dev'

INSTALLED_APPS += (
    'sslserver',
)

BROKER_BACKEND = 'amqplib'
BROKER_HOST = os.environ.get('QUEUE_1_PORT_5672_TCP_ADDR')
BROKER_USER = 'guest'
BROKER_PASSWORD = 'guest'
BROKER_PORT = os.environ.get('QUEUE_1_PORT_5672_TCP_PORT')
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

JS_USE_COMPILED = True
RUN_LOCALLY = True

debug = True

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': "amara",
        'USER': "amara",
        'PASSWORD': "amara",
        'HOST': os.environ.get('DB_1_PORT_3306_TCP_ADDR'),
        'PORT': os.environ.get('DB_1_PORT_3306_TCP_PORT'),
        }
    }

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.memcached.MemcachedCache',
        'LOCATION': '{}:{}'.format(os.environ.get('CACHE_1_PORT_11211_TCP_ADDR'),
            os.environ.get('CACHE_1_PORT_11211_TCP_PORT')),
    }
}

# Make this unique, and don't share it with anybody.
SECRET_KEY = 'a9yr_yzp2vmj-2q1zq)d2+b^w(7fqu2o&jh18u9dozjbd@-$0!'

TWITTER_CONSUMER_KEY = '6lHYqtxzQBD3lQ55Chi6Zg'
TWITTER_CONSUMER_SECRET = 'ApkJPIIbBKp3Wph0JBoAg2Nsk1Z5EG6PFTevNpd5Y00'

MEDIA_URL = "http://unisubs.example.com:8000/user-data/"

FACEBOOK_APP_KEY = FACEBOOK_APP_ID = '255603057797860'
FACEBOOK_SECRET_KEY = '2a18604dac1ad7e9817f80f3aa3a69f2'

OAUTH_CALLBACK_PROTOCOL = 'http'
YOUTUBE_CLIENT_ID = None
YOUTUBE_CLIENT_SECRET = None
YOUTUBE_API_KEY = None

# Celery
CELERY_ALWAYS_EAGER = False
CELERY_TASK_RESULT_EXPIRES = timedelta(days=7)

# Or you can use redis as backend
#BROKER_BACKEND = 'redis'
#BROKER_HOST = "localhost"
#BROKER_VHOST = "/"

# 1. Run Redis
# 2. >>> python manage.py celeryd -E --concurrency=10 -n worker1.localhost
# 3. >>> ./dev-runserver
# 4. >>> python manage.py celerycam #this is optional. It allow see in admin-interface tasks running

CACHE_PREFIX = 'unisubsdevsettings'
CACHE_TIMEOUT = 0

COMPRESS_MEDIA = not DEBUG

# Log more more things to the console
LOGGING['root']['level'] = 'INFO'
# disable sentry logging
LOGGING['handlers']['sentry']['class'] = 'logging.NullHandler'

try:
    from dev_settings_local import *
except ImportError:
    pass
