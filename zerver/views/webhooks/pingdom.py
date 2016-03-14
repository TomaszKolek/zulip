# Webhooks for external integrations.
from __future__ import absolute_import
from zerver.models import get_client
from zerver.lib.actions import check_send_message
from zerver.lib.response import json_success, json_error
from zerver.decorator import REQ, has_request_variables, api_key_only_webhook_view

import ujson


@api_key_only_webhook_view
@has_request_variables
def api_pingdom_webhook(request, user_profile, stream=REQ(default='pingdom')):

    payload = ujson.loads(request.body)
    check_type = payload['check_type']
    name = payload['check_name']

    if check_type == 'HTTP':
        subject = get_subject_for_http_request(name)
        body = get_body_for_http_request(payload)
    else:
        return json_error('Unsupported check_type: {check_type}'.format(check_type=check_type))

    check_send_message(user_profile, get_client('ZulipPingdomWebhook'), 'stream', [stream], subject, body)
    return json_success()


def get_subject_for_http_request(name):
    return "Your Pingdom {name} descries something important".format(name=name)


def get_body_for_http_request(payload):
    data = {
        'service_url': payload['check_params']['full_url'],
        'previous_state': payload['previous_state'],
        'current_state': payload['current_state'],
    }
    return "Service {service_url} changed it's status from {previous_state} to {current_state}".format(**data)