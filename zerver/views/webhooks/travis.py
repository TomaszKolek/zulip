# Webhooks for external integrations.
from __future__ import absolute_import
from zerver.models import get_client
from zerver.lib.actions import check_send_message
from zerver.lib.response import json_success
from zerver.decorator import REQ, has_request_variables, api_key_only_webhook_view

import ujson

GOOD_STATUES = ['Passed', 'Fixed']
BAD_STATUSES = ['Failed', 'Broken', 'Still Failing']
THUMBS_UP_EMOJI = ':thumbsup:'
THUMBS_DOWN_EMOJI = ':thumbsdown:'


@api_key_only_webhook_view
@has_request_variables
def api_travis_webhook(request, user_profile, stream=REQ(default='travis'), topic=REQ(default=None)):
    message = ujson.loads(request.POST['payload'])

    author = message['author_name']
    message_type = message['status_message']
    changes = message['compare_url']

    if message_type in GOOD_STATUES:
        emoji = THUMBS_UP_EMOJI
    elif message_type in BAD_STATUSES:
        emoji = THUMBS_DOWN_EMOJI
    else:
        emoji = "(No emoji specified for status '{}'.)".format(message_type)

    build_url = message['build_url']

    template = (
        u'Author: {}\n'
        u'Build status: {} {}\n'
        u'Details: [changes]({}), [build log]({})')

    body = template.format(author, message_type, emoji, changes, build_url)

    check_send_message(user_profile, get_client('ZulipTravisWebhook'), 'stream', [stream], topic, body)
    return json_success()
