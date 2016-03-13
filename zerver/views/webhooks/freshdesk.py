# Webhooks for external integrations.
from __future__ import absolute_import
from zerver.models import get_client
from zerver.lib.actions import check_send_message
from zerver.lib.response import json_success, json_error
from zerver.lib.notifications import convert_html_to_markdown
from zerver.decorator import REQ, has_request_variables, authenticated_rest_api_view

import logging
import ujson


REQUIRED_KEYS = [
    "triggered_event",
    "ticket_id",
    "ticket_url",
    "ticket_type",
    "ticket_subject",
    "ticket_description",
    "ticket_status",
    "ticket_priority",
    "requester_name",
    "requester_email",
]

STATUSES = [
    "",
    "",
    "Open",
    "Pending",
    "Resolved",
    "Closed",
    "Waiting on Customer",
    "Job Application",
    "Monthly"
]


PRIORITIES = [
    "",
    "Low",
    "Medium",
    "High",
    "Urgent"
]


class TicketDict(dict):
    """
    A helper class to turn a dictionary with ticket information into
    an object where each of the keys is an attribute for easy access.
    """
    def __getattr__(self, field):
        if "_" in field:
            return self.get(field)
        else:
            return self.get("ticket_" + field)

def property_name(property, index):
    # The Freshdesk API is currently pretty broken: statuses are customizable
    # but the API will only tell you the number associated with the status, not
    # the name. While we engage the Freshdesk developers about exposing this
    # information through the API, since only FlightCar uses this integration,
    # hardcode their statuses.
    if property == "status":
        return STATUSES[index] if index < len(STATUSES) else str(index)
    elif property == "priority":
        return PRIORITIES[index] if index < len(PRIORITIES) else str(index)
    else:
        raise ValueError("Unknown property")

def parse_freshdesk_event(event_string):
    # These are always of the form "{ticket_action:created}" or
    # "{status:{from:4,to:6}}". Note the lack of string quoting: this isn't
    # valid JSON so we have to parse it ourselves.
    data = event_string.replace("{", "").replace("}", "").replace(",", ":").split(":")

    if len(data) == 2:
        # This is a simple ticket action event, like
        # {ticket_action:created}.
        return data
    else:
        # This is a property change event, like {status:{from:4,to:6}}. Pull out
        # the property, from, and to states.
        property, _, from_state, _, to_state = data
        return property, property_name(property, int(from_state)), property_name(property, int(to_state))


def format_freshdesk_note_message(ticket, event_info):
    # There are public (visible to customers) and private note types.
    note_type = event_info[1]
    return "{} <{}> added a {} note to [ticket #{}]({}).".format(
        ticket.requester_name,
        ticket.requester_email,
        note_type,
        ticket.id,
        ticket.url)


def format_freshdesk_property_change_message(ticket, event_info):
    # Freshdesk will only tell us the first event to match our webhook
    # configuration, so if we change multiple properties, we only get the before
    # and after data for the first one.
    content = "{} <{}> updated [ticket #{}]({}):\n\n".format(
        ticket.requester_name,
        ticket.requester_email,
        ticket.id,
        ticket.url
    )
    content += "{}: **{}** => **{}**".format(event_info[0].capitalize(), event_info[1], event_info[2])

    return content


def format_freshdesk_ticket_creation_message(ticket):
    # They send us the description as HTML.
    cleaned_description = convert_html_to_markdown(ticket.description)
    content = u"{} <{}> created [ticket #{}]({}):\n\n".format(
        ticket.requester_name,
        ticket.requester_email,
        ticket.id,
        ticket.url
    )
    content += u"""~~~ quote
{}
~~~\n
""".format(cleaned_description,)
    content += u"Type: **{}**\nPriority: **{}**\nStatus: **{}**".format(
        ticket.type,
        ticket.priority,
        ticket.status
    )

    return content


@authenticated_rest_api_view
@has_request_variables
def api_freshdesk_webhook(request, user_profile, stream=REQ(default='')):
    try:
        payload = ujson.loads(request.body)
        ticket_data = payload["freshdesk_webhook"]
    except ValueError:
        return json_error("Malformed JSON input")

    for key in REQUIRED_KEYS:
        if ticket_data.get(key) is None:
            logging.warning("Freshdesk webhook error. Payload was:")
            logging.warning(request.body)
            return json_error("Missing key {} in JSON".format(key))

    try:
        stream = request.GET['stream']
    except (AttributeError, KeyError):
        stream = 'freshdesk'

    ticket = TicketDict(ticket_data)

    subject = u"#{}: {}".format(ticket.id, ticket.subject)

    try:
        event_info = parse_freshdesk_event(ticket.triggered_event)
    except ValueError:
        return json_error("Malformed event {}".format(ticket.triggered_event))

    if event_info[1] == "created":
        content = format_freshdesk_ticket_creation_message(ticket)
    elif event_info[0] == "note_type":
        content = format_freshdesk_note_message(ticket, event_info)
    elif event_info[0] in ("status", "priority"):
        content = format_freshdesk_property_change_message(ticket, event_info)
    else:
        # Not an event we know handle; do nothing.
        return json_success()

    check_send_message(user_profile, get_client("ZulipFreshdeskWebhook"), "stream",
                       [stream], subject, content)
    return json_success()