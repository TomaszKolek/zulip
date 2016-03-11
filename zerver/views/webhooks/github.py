from __future__ import absolute_import

from django.conf import settings
from zerver.models import get_client
from zerver.lib.response import json_success
from zerver.lib.validator import check_dict
from zerver.decorator import authenticated_api_view, REQ, has_request_variables, to_non_negative_int, flexible_boolean
from zerver.views.messages import send_message_backend

import logging
import re
import ujson


class GitHubWebhookHandler(object):
    COMMITS_IN_LIST_LIMIT = 10
    ZULIP_TEST_REPO_NAME = 'zulip-test'
    ZULIP_TEST_REPO_ID = 6893087

    def handle(self, *args, **kwargs):
        self.api_github_landing(*args, **kwargs)

    def api_github_v1(self, user_profile, event, payload, branches, stream, **kwargs):
        """
        processes github payload with version 1 field specification
        `payload` comes in unmodified from github
        `stream` is set to 'commits' if otherwise unset
        """
        commit_stream = stream
        issue_stream = 'issues'
        return self.api_github_v2(user_profile,
                                  event,
                                  payload,
                                  branches,
                                  stream,
                                  commit_stream,
                                  issue_stream,
                                  **kwargs)

    def api_github_v2(self,
                      user_profile,
                      event,
                      payload,
                      branches,
                      default_stream,
                      commit_stream,
                      issue_stream,
                      topic_focus=None):
        """
        processes github payload with version 2 field specification
        `payload` comes in unmodified from github
        `default_stream` is set to what `stream` is in v1 above
        `commit_stream` and `issue_stream` fall back to `default_stream` if they are empty
        This and allowing alternative endpoints is what distinguishes v1 from v2 of the github configuration
        """
        commit_stream = commit_stream if commit_stream else default_stream
        issue_stream = issue_stream if issue_stream else default_stream
        target_stream = commit_stream
        repository = payload.get('repository')
        topic_focus = topic_focus if topic_focus else repository.get('name')

        # Event Handlers
        if event == 'pull_request':
            pull_req = payload['pull_request']
            subject = self.github_generic_subject('pull request', topic_focus, pull_req)
            content = self.github_generic_content('pull request', payload, pull_req)

        elif event == 'issues':
            # in v1, we assume that this stream exists since it is
            # deprecated and the few realms that use it already have the
            # stream
            target_stream = issue_stream
            issue = payload.get('issue')
            subject = self.github_generic_subject('issue', topic_focus, issue)
            content = self.github_generic_content('issue', payload, issue)

        elif event == 'issue_comment':
            # Comments on both issues and pull requests come in as issue_comment events
            issue = payload.get('issue')
            if 'pull_request' not in issue or issue.get('pull_request').get('diff_url') is None:
                # It's an issues comment
                target_stream = issue_stream
                noun = 'issue'
            else:
                # It's a pull request comment
                noun = 'pull request'

            subject = self.github_generic_subject(noun, topic_focus, issue)
            comment = payload.get('comment')
            content = "{} [commented]({}) on [{} {}]({})\n\n~~~ quote\n{}\n~~~".format(comment.get('user').get('login'),
                                                                                       comment.get('html_url'),
                                                                                       noun,
                                                                                       issue.get('number'),
                                                                                       issue.get('html_url'),
                                                                                       comment.get('body'))

        elif event == 'push':
            subject, content = self.build_message_from_gitlog(payload, topic_focus)

        elif event == 'commit_comment':
            comment = payload.get('comment')
            subject = "{}: commit {}".format(topic_focus, comment.get('commit_id'))

            content = "{} [commented]({})".format(comment.get('user').get('login'), comment.get('html_url'))

            if comment['line'] is not None:
                content += " on `{}`, line {}".format(comment.get('path'), comment('line'))

            content += "\n\n~~~ quote\n{}\n~~~".format(comment.get('body'))
        else:
            raise Exception("Unknown event type") # todo make custom exception

        return target_stream, subject, content

    def api_github_landing(self,
                           request,
                           user_profile,
                           event=REQ,
                           payload=REQ(validator=check_dict([])),
                           branches=REQ(default=''),
                           stream=REQ(default=''),
                           version=REQ(converter=to_non_negative_int, default=1),
                           commit_stream=REQ(default=''),
                           issue_stream=REQ(default=''),
                           exclude_pull_requests=REQ(converter=flexible_boolean, default=False),
                           exclude_issues=REQ(converter=flexible_boolean, default=False),
                           exclude_commits=REQ(converter=flexible_boolean, default=False),
                           emphasize_branch_in_topic=REQ(converter=flexible_boolean, default=False),
                           ):

        repository = payload.get('repository')

        # Special hook for capturing event data. If we see our special test repo, log the payload from github.
        try:
            if self.is_repository_zulip_test(repository) and settings.PRODUCTION:
                self.log_test_info(event,
                                   payload,
                                   branches,
                                   stream,
                                   version,
                                   commit_stream,
                                   issue_stream,
                                   exclude_pull_requests,
                                   exclude_issues,
                                   exclude_commits,
                                   emphasize_branch_in_topic)
        except Exception:
            logging.exception("Error while capturing Github event")

        stream = stream if stream else'commits'

        short_ref = re.sub(r'^refs/heads/', '', payload.get('ref', ""))
        kwargs = dict()

        if emphasize_branch_in_topic and short_ref:
            kwargs['topic_focus'] = short_ref

        allowed_events = set()
        if not exclude_pull_requests:
            allowed_events.add('pull_request')

        if not exclude_issues:
            allowed_events.add("issues")
            allowed_events.add("issue_comment")

        if not exclude_commits:
            allowed_events.add("push")
            allowed_events.add("commit_comment")

        if event not in allowed_events:
            return json_success()

        # We filter issue_comment events for issue creation events
        if event == 'issue_comment' and payload.get('action') != 'created':
            return json_success()

        if event == 'push':
            # If we are given a whitelist of branches, then we silently ignore
            # any push notification on a branch that is not in our whitelist.
            if branches and short_ref not in re.split('[\s,;|]+', branches):
                return json_success()

        # Map payload to the handler with the right version
        if version == 2:
            target_stream, subject, content = self.api_github_v2(user_profile, event, payload, branches, stream, commit_stream, issue_stream, **kwargs)
        else:
            target_stream, subject, content = self.api_github_v1(user_profile, event, payload, branches, stream, **kwargs)
        request.client = get_client("ZulipGitHubWebhook")
        return send_message_backend(request,
                                    user_profile,
                                    message_type_name="stream",
                                    message_to=[target_stream],
                                    forged=False, subject_name=subject,
                                    message_content=content)

    @staticmethod
    def build_commit_list_content(commits, branch, compare_url, pusher):
        push_text = "pushed" if compare_url is None else "[pushed]({})".format(compare_url)
        content = "{} {} to branch {}\n\n".format(pusher, push_text, branch)
        num_commits = len(commits)
        truncated_commits = commits[:GitHubWebhookHandler.COMMITS_IN_LIST_LIMIT]

        for commit in truncated_commits:
            short_id = commit['id'][:7]
            short_commit_msg = commit['message'].partition("\n")[0]
            content += "* [{}]({}): {}\n".format(short_id, commit['url'], short_commit_msg)

        if num_commits > GitHubWebhookHandler.COMMITS_IN_LIST_LIMIT:
            content += "\n[and {} more commits]".format(num_commits - GitHubWebhookHandler.COMMITS_IN_LIST_LIMIT)

        return content

    @staticmethod
    def build_message_from_gitlog(payload, name):
        ref = payload.get('ref')
        commits = payload.get('commits')
        after = payload.get('after')
        url = payload.get('compare')
        pusher = payload.get('pusher').get('name')
        forced = payload.get('forced', None)
        created = payload.get('created', None)
        short_ref = re.sub(r'^refs/heads/', '', ref)
        subject = name

        if re.match(r'^0+$', after):
            content = "{} deleted branch {}".format(pusher, short_ref)
        # 'created' and 'forced' are github flags; the second check is for beanstalk
        elif (forced and not created) or (forced is None and len(commits) == 0):
            content = "{} [force pushed]({}) to branch {}.  Head is now {}".format(pusher, url, short_ref, after[:7])
        else:
            content = GitHubWebhookHandler.build_commit_list_content(commits, short_ref, url, pusher)

        return subject, content

    def github_generic_subject(self, noun, topic_focus, blob):
        """issue and pull_request objects have the same fields we're interested in"""
        return "{}: {} {}: {}".format(topic_focus, noun, blob['number'], blob['title'])

    def github_generic_content(self, noun, payload, blob):
        action = payload.get('action')
        action = 'synchronized' if action == 'synchronize' else action

        # issue and pull_request objects have the same fields we're interested in
        content = "{} {} [{} {}]({})".format(payload['sender']['login'],
                                             action,
                                             noun,
                                             blob['number'],
                                             blob['html_url'])

        if payload['action'] in ('opened', 'reopened'):
            content += "\n\n~~~ quote\n{}\n~~~" .format(blob['body'])
        return content

    def is_repository_zulip_test(self, repository):
        return repository.get('name') == self.ZULIP_TEST_REPO_NAME and repository.get('id') == self.ZULIP_TEST_REPO_ID

    def log_test_info(self,
                      event,
                      payload,
                      branches,
                      stream,
                      version,
                      commit_stream,
                      issue_stream,
                      exclude_pull_requests,
                      exclude_issues,
                      exclude_commits,
                      emphasize_branch_in_topic):
        with open('/var/log/zulip/github-payloads', 'a') as f:
            f.write(ujson.dumps({'event': event,
                                 'payload': payload,
                                 'branches': branches,
                                 'stream': stream,
                                 'version': version,
                                 'commit_stream': commit_stream,
                                 'issue_stream': issue_stream,
                                 'exclude_pull_requests': exclude_pull_requests,
                                 'exclude_issues': exclude_issues,
                                 'exclude_commits': exclude_commits,
                                 'emphasize_branch_in_topic': emphasize_branch_in_topic,
                                 }))
            f.write("\n")