# Webhooks for external integrations.
from __future__ import absolute_import
from zerver.models import get_client
from zerver.lib.actions import check_send_message
from zerver.lib.response import json_success, json_error
from zerver.decorator import api_key_only_webhook_view

from defusedxml.ElementTree import fromstring as xml_fromstring

import logging
import re
import ujson


PIVOTAL_URL_TEMPLATE = "https://www.pivotaltracker.com/s/projects/{}/stories/{}"


def get_text(payload, attrs):
    start = payload
    try:
        for attr in attrs:
            start = start.find(attr)
        return start.text
    except AttributeError:
        return ""


def extract_comment(change):
    return change.get("new_values", {}).get("text", None) if change.get("kind") == "comment" else None


def api_pivotal_webhook_v3(request, user_profile, stream):
    payload = xml_fromstring(request.body)

    event_type = get_text(payload, ['event_type'])
    description = get_text(payload, ['description'])
    project_id = get_text(payload, ['project_id'])
    story_id = get_text(payload, ['stories', 'story', 'id'])

    # Pivotal doesn't tell us the name of the story, but it's usually in the
    # description in quotes as the first quoted string
    name_re = re.compile(r'[^"]+"([^"]+)".*')
    match = name_re.match(description)
    if match and len(match.groups()):
        name = match.group(1)
    else:
        name = "Story changed"  # Failed for an unknown reason, show something
    more_info = " [(view)]({})".format(PIVOTAL_URL_TEMPLATE.format(project_id, story_id),)

    if event_type == 'story_update':
        subject = name
        content = description + more_info
        
    elif event_type == 'note_create':
        subject = "Comment added"
        content = description +  more_info
        
    elif event_type == 'story_create':
        issue_desc = get_text(payload, ['stories', 'story', 'description'])
        issue_type = get_text(payload, ['stories', 'story', 'story_type'])
        issue_status = get_text(payload, ['stories', 'story', 'current_state'])
        estimate = get_text(payload, ['stories', 'story', 'estimate'])
        estimate = " worth {} story points".format(estimate) if estimate != '' else estimate
        
        subject = name
        content = "{} ({} {}{}):\n\n~~~ quote\n{}\n~~~\n\n{}".format(description,
                                                                     issue_status,
                                                                     issue_type,
                                                                     estimate,
                                                                     issue_desc,
                                                                     more_info)
    else:
        raise Exception("Unknown event type")  # todo create custom exception
    return subject, content


def api_pivotal_webhook_v5(request, user_profile, stream):
    payload = ujson.loads(request.body)

    event_type = payload["kind"]

    project_name = payload["project"]["name"]
    project_id = payload["project"]["id"]

    primary_resources = payload["primary_resources"][0]
    story_url = primary_resources["url"]
    story_type = primary_resources["story_type"]
    story_id = primary_resources["id"]
    story_name = primary_resources["name"]

    performed_by = payload.get("performed_by", {}).get("name", "")

    story_info = "[{}](https://www.pivotaltracker.com/s/projects/{}): [{}]({})".format(project_name,
                                                                                       project_id,
                                                                                       story_name,
                                                                                       story_url)

    changes = payload.get("changes", [])

    content = ""
    subject = "#{}: {}".format(story_id, story_name)

    if event_type == "story_update_activity":
        # Find the changed valued and build a message
        content += "{} updated {}:\n".format(performed_by, story_info)
        for change in changes:
            old_values = change.get("original_values", {})
            new_values = change["new_values"]

            if "current_state" in old_values and "current_state" in new_values:
                content += "* state changed from **{}** to **{}**\n".format(old_values["current_state"],
                                                                            new_values["current_state"])

            if "estimate" in old_values and "estimate" in new_values:
                old_estimate = old_values.get("estimate", None)
                estimate = "is now" if old_estimate is None else "changed from {} to".format(old_estimate)
                new_estimate = new_values["estimate"] if new_values["estimate"] is not None else "0"
                content += "* estimate {} **{} points**\n".format(estimate, new_estimate)

            if "story_type" in old_values and "story_type" in new_values:
                content += "* type changed from **{}** to **{}**\n".format(old_values["story_type"],
                                                                           new_values["story_type"])

            comment = extract_comment(change)
            if comment is not None:
                content += "* Comment added:\n~~~quote\n{}\n~~~\n".format(comment)

    elif event_type == "comment_create_activity":
        for change in changes:
            comment = extract_comment(change)
            if comment is not None:
                content += "{} added a comment to {}:\n~~~quote\n{}\n~~~".format(performed_by, story_info, comment)

    elif event_type == "story_create_activity":
        content += "{} created {}: {}\n".format(performed_by, story_type, story_info)
        for change in changes:
            new_values = change.get("new_values", {})
            if "current_state" in new_values:
                content += "* State is **{}**\n".format(new_values["current_state"],)
            if "description" in new_values:
                content += "* Description is\n\n> {}".format(new_values["description"],)

    elif event_type == "story_move_activity":
        content = "{} moved {}".format(performed_by, story_info)
        for change in changes:
            old_values = change.get("original_values", {})
            new_values = change["new_values"]
            if "current_state" in old_values and "current_state" in new_values:
                content += " from **{}** to **{}**".format(old_values["current_state"], new_values["current_state"])

    elif event_type in ["task_create_activity", "comment_delete_activity",
                        "task_delete_activity", "task_update_activity",
                        "story_move_from_project_activity", "story_delete_activity",
                        "story_move_into_project_activity"]:
        # Known but unsupported Pivotal event types
        pass
    else:
        logging.warning("Unknown Pivotal event type: {}".format(event_type,))

    return subject, content


@api_key_only_webhook_view
def api_pivotal_webhook(request, user_profile):
    try:
        stream = request.GET['stream']
    except (AttributeError, KeyError):
        return json_error("Missing stream parameter.")

    try:
        subject, content = api_pivotal_webhook_v3(request, user_profile, stream)
    except AttributeError:
        return json_error("Failed to extract data from Pivotal XML response")
    except:
        # Attempt to parse v5 JSON payload
        try:
            subject, content = api_pivotal_webhook_v5(request, user_profile, stream)
        except AttributeError:
            return json_error("Failed to extract data from Pivotal V5 JSON response")

    if subject is None or content is None:
        return json_error("Unable to handle Pivotal payload")

    check_send_message(user_profile,
                       get_client("ZulipPivotalWebhook"),
                       "stream",
                       [stream],
                       subject,
                       content)
    return json_success()
