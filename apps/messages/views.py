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

import time
from datetime import datetime
from cgi import escape

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponseForbidden
from django.utils.http import cookie_date
from django.utils.translation import ugettext_lazy as _
from django.db.models import Q

from auth.models import CustomUser as User
from auth.models import UserLanguage
from messages.forms import SendMessageForm, NewMessageForm
from messages.models import Message
from messages.rpc import MessagesApiClass
from messages.tasks import send_new_message_notification, send_new_messages_notifications
from utils import render_to_json, render_to
from utils.objectlist import object_list
from utils.rpc import RpcRouter

rpc_router = RpcRouter('messages:rpc_router', {
    'MessagesApi': MessagesApiClass()
})

MAX_MEMBER_SEARCH_RESULTS = 40
MESSAGES_ON_PAGE = getattr(settings, 'MESSAGES_ON_PAGE', 30)


@login_required
def message(request, message_id):
    user = request.user
    messages = Message.objects.for_user_or_author(user).filter(id=message_id)
    if len(messages) != 1:
        return HttpResponseForbidden("Not allowed")
    hide_thread = request.GET.get('hide_thread')
    message_thread = Message.objects.thread(messages[0], user)
    message_thread_length = message_thread.count()
    if not hide_thread:
        messages = message_thread

    reply = request.GET.get('reply')

    if reply:
        try:
            reply_msg = Message.objects.get(pk=reply, user=user)
            reply_msg.read = True
            reply_msg.save()
            extra_context['reply_msg'] = reply_msg
        except (Message.DoesNotExist, ValueError):
            pass

    messages.filter(user=user).update(read=True)
    
    extra_context = {
        'send_message_form': SendMessageForm(request.user, auto_id='message_form_id_%s'),
        'messages_display': True,
        'user_info': user,
        'subject': messages[0].subject,
        'mid': message_id,
        'thread_length': message_thread_length
    }

    response = object_list(request, queryset=messages,
                       paginate_by=MESSAGES_ON_PAGE,
                       template_name='messages/message.html',
                       template_object_name='message',
                       extra_context=extra_context)
    try:
        last_message = messages[0]
        max_age = 60*60*24*365
        expires = cookie_date(time.time()+max_age)
        response.set_cookie(Message.hide_cookie_name, last_message.pk, max_age, expires)
    except Message.DoesNotExist:
        pass
    return response

@login_required
def inbox(request, message_pk=None):
    user = request.user
    qs = Message.objects.for_user(user)

    extra_context = {
        'send_message_form': SendMessageForm(request.user, auto_id='message_form_id_%s'),
        'messages_display': True,
        'user_info': user
    }

    type_filter = request.GET.get('type')
    if type_filter:
        if type_filter != 'any':
            qs = qs.filter(message_type = type_filter)

    reply = request.GET.get('reply')

    if reply:
        try:
            reply_msg = Message.objects.get(pk=reply, user=user)
            reply_msg.read = True
            reply_msg.save()
            extra_context['reply_msg'] = reply_msg
        except (Message.DoesNotExist, ValueError):
            pass
    filtered = bool(set(request.GET.keys()).intersection([
        'type']))

    extra_context['type_filter'] = type_filter
    extra_context['filtered'] = filtered

    response = object_list(request, queryset=qs,
                       paginate_by=MESSAGES_ON_PAGE,
                       template_name='messages/inbox.html',
                       template_object_name='message',
                       extra_context=extra_context)
    try:
        last_message = qs[:1].get()
        max_age = 60*60*24*365
        expires = cookie_date(time.time()+max_age)
        response.set_cookie(Message.hide_cookie_name, last_message.pk, max_age, expires)
    except Message.DoesNotExist:
        pass

    return response

@login_required
def sent(request):
    user = request.user
    qs = Message.objects.for_author(request.user)
    extra_context = {
        'send_message_form': SendMessageForm(request.user, auto_id='message_form_id_%s'),
        'messages_display': True,
        'user_info': user
    }
    return object_list(request, queryset=qs,
                       paginate_by=MESSAGES_ON_PAGE,
                       template_name='messages/sent.html',
                       template_object_name='message',
                       extra_context=extra_context)

@login_required
@render_to('messages/new.html')
def new(request):
    selected_user = None

    if request.POST:
        form = NewMessageForm(request.user, request.POST)

        if form.is_valid():
            if form.cleaned_data['user']:
                m = Message(user=form.cleaned_data['user'], author=request.user,
                        message_type='M',
                        content=form.cleaned_data['content'],
                        subject=form.cleaned_data['subject'])
                m.save()
                send_new_message_notification.delay(m.pk)
            elif form.cleaned_data['team']:
                now = datetime.now()
                # TODO: Move this into a task for performance?
                language = form.cleaned_data['language']
                # We create messages using bulk_create, so that only one transaction is needed
                # But that means that we need to sort out the pk of newly created messages to
                # be able to send the notifications
                message_list = []
                members = []
                if len(language) == 0:
                    members = map(lambda member: member.user, form.cleaned_data['team'].members.all().exclude(user__exact=request.user).select_related('user'))
                else:
                    members = map(lambda member: member.user, UserLanguage.objects.filter(user__in=form.cleaned_data['team'].members.values('user')).filter(language__exact=language).exclude(user__exact=request.user).select_related('user'))
                for member in members:
                    message_list.append(Message(user=member, author=request.user,
                                                message_type='M',
                                                content=form.cleaned_data['content'],
                                                subject=form.cleaned_data['subject']))
                Message.objects.bulk_create(message_list, batch_size=500);
                new_messages_ids = Message.objects.filter(created__gt=now).values_list('pk', flat=True)
                # Creating a bunch of reasonably-sized tasks
                batch = 0
                batch_size = 1000
                while batch < len(new_messages_ids):
                    send_new_messages_notifications.delay(new_messages_ids[batch:batch+batch_size])
                    batch += batch_size

            messages.success(request, _(u'Message sent.'))
            return HttpResponseRedirect(reverse('messages:inbox'))
        else:
            if request.GET.get('user'):
                selected_user = User.objects.get(username=request.GET['user'])
    else:
        form = NewMessageForm(request.user)

        if request.GET.get('user'):
            selected_user = User.objects.get(username=request.GET['user'])

    return {
        'selected_user': selected_user,
        'user_info': request.user,
        'form': form,
    }

@render_to_json
def search_users(request):
    users = User.objects.all()
    q = request.GET.get('term')
    search_in_fields = Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
    results = [[u.id, escape(u.username), escape(unicode(u))]
               for u in users.filter(search_in_fields,
                                            is_active=True)]
    results = results[:MAX_MEMBER_SEARCH_RESULTS]

    return { 'results': results }
