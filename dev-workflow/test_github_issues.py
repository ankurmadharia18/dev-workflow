#!/usr/bin/env python3
import json
import subprocess
import unittest

from github_issues import GitHubAdapterError, list_actionable, set_claimed


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


if __name__ == "__main__":
    unittest.main()
