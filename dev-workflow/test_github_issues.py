#!/usr/bin/env python3
import json
import subprocess
import unittest

from github_issues import (
    GitHubAdapterError,
    add_label,
    comment_issue,
    find_issue_pull_request,
    get_issue,
    list_actionable,
    set_blocked,
    set_claimed,
)


class GitHubIssuesTests(unittest.TestCase):
    def test_lists_oldest_actionable_issues_and_excludes_claimed_work(self):
        payload = [
            {
                "number": 12,
                "title": "Newer ticket",
                "url": "https://github.com/acme/repo/issues/12",
                "createdAt": "2026-09-22T11:00:00Z",
                "labels": [{"name": "agent-ready"}],
            },
            {
                "number": 10,
                "title": "Already claimed",
                "url": "https://github.com/acme/repo/issues/10",
                "createdAt": "2026-09-22T09:00:00Z",
                "labels": [
                    {"name": "agent-ready"},
                    {"name": "agent-claimed"},
                ],
            },
            {
                "number": 11,
                "title": "Older ticket",
                "url": "https://github.com/acme/repo/issues/11",
                "createdAt": "2026-09-22T10:00:00Z",
                "labels": [{"name": "agent-ready"}],
            },
        ]

        def runner(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "issue", "list"])
            self.assertIn("agent-ready", command)
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        issues = list_actionable(
            "acme/repo", "agent-ready", ["agent-claimed"], runner=runner
        )
        self.assertEqual([issue.number for issue in issues], [11, 12])

    def test_surfaces_gh_failure_without_mutating_any_issue(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "authentication failed")

        with self.assertRaisesRegex(GitHubAdapterError, "authentication failed"):
            list_actionable("acme/repo", "agent-ready", [], runner=runner)

    def test_get_issue_reads_state_and_labels(self):
        payload = {
            "number": 42,
            "title": "Fix it",
            "url": "https://github.com/acme/repo/issues/42",
            "createdAt": "2026-09-22T10:00:00Z",
            "labels": [{"name": "security"}],
            "state": "OPEN",
        }

        def runner(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "issue", "view"])
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        issue = get_issue("acme/repo", 42, runner=runner)
        self.assertEqual(issue.number, 42)
        self.assertEqual(issue.labels, ("security",))
        self.assertEqual(issue.state, "OPEN")

    def test_finds_issue_pr_and_summarizes_checks(self):
        payload = [
            {
                "number": 77,
                "title": "Fix it",
                "url": "https://github.com/acme/repo/pull/77",
                "state": "OPEN",
                "isDraft": True,
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"state": "PENDING"},
                    {"conclusion": "FAILURE"},
                ],
            }
        ]

        def runner(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "pr", "list"])
            self.assertEqual(command[command.index("--head") + 1], "codex/issue-42")
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        pull = find_issue_pull_request("acme/repo", 42, runner=runner)
        self.assertIsNotNone(pull)
        self.assertEqual(pull.number, 77)
        self.assertTrue(pull.is_draft)
        self.assertEqual(
            (pull.checks_passed, pull.checks_pending, pull.checks_failed),
            (1, 1, 1),
        )

    def test_missing_issue_pr_returns_none(self):
        completed = subprocess.CompletedProcess([], 0, "[]", "")
        self.assertIsNone(
            find_issue_pull_request(
                "acme/repo", 42, runner=lambda *_args, **_kwargs: completed
            )
        )

    def test_add_label_uses_configured_label(self):
        captured = []

        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        add_label("acme/repo", 42, "agent-ready", runner=runner)
        self.assertEqual(captured[0][:3], ["gh", "issue", "edit"])
        self.assertEqual(
            captured[0][captured[0].index("--add-label") + 1], "agent-ready"
        )

    def test_rejects_non_list_payload(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "{}", "")

        with self.assertRaisesRegex(GitHubAdapterError, "unexpected payload"):
            list_actionable("acme/repo", "agent-ready", [], runner=runner)

    def test_claim_moves_ready_issue_to_claimed_label(self):
        captured = []

        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        set_claimed(
            "acme/repo",
            42,
            "agent-ready",
            "agent-claimed",
            claimed=True,
            runner=runner,
        )
        self.assertIn("--add-label", captured[0])
        self.assertEqual(captured[0][captured[0].index("--add-label") + 1], "agent-claimed")
        self.assertEqual(
            captured[0][captured[0].index("--remove-label") + 1], "agent-ready"
        )

    def test_release_restores_ready_label(self):
        captured = []

        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        set_claimed(
            "acme/repo",
            42,
            "agent-ready",
            "agent-claimed",
            claimed=False,
            runner=runner,
        )
        self.assertEqual(captured[0][captured[0].index("--add-label") + 1], "agent-ready")
        self.assertEqual(
            captured[0][captured[0].index("--remove-label") + 1], "agent-claimed"
        )

    def test_block_and_unblock_use_configured_label(self):
        captured = []

        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        set_blocked(
            "acme/repo", 42, "agent-blocked", blocked=True, runner=runner
        )
        set_blocked(
            "acme/repo", 42, "agent-blocked", blocked=False, runner=runner
        )

        self.assertIn("--add-label", captured[0])
        self.assertIn("agent-blocked", captured[0])
        self.assertIn("--remove-label", captured[1])
        self.assertIn("agent-blocked", captured[1])

    def test_comment_persists_question_or_answer_on_issue(self):
        captured = []

        def runner(command, **kwargs):
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        comment_issue("acme/repo", 42, "Answer from Telegram", runner=runner)

        self.assertEqual(captured[0][:3], ["gh", "issue", "comment"])
        self.assertEqual(captured[0][captured[0].index("--body") + 1], "Answer from Telegram")


if __name__ == "__main__":
    unittest.main()
