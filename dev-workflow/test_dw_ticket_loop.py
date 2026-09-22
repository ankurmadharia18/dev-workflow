#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import dw_ticket_loop
from github_issues import GitHubIssue


CONFIG = {
    "repo": {"base_branch": "main"},
    "tracker": {
        "provider": "github",
        "repo": "acme/repo",
        "roles": {
            "queue": {"label": "agent-ready"},
            "claimed": {"label": "agent-claimed"},
        },
    },
    "build": {
        "model": "claude-fable-5-1",
        "subagent_model": "claude-opus-5",
        "codex_model": "gpt-6-astra",
        "codex_model_reasoning_effort": "low",
        "codex_subagent_model": "gpt-5.6-sol",
        "codex_subagent_model_reasoning_effort": "medium",
    },
    "agent": {"enabled": True},
}

ISSUE = GitHubIssue(
    number=42,
    title="Fix the widget",
    url="https://github.com/acme/repo/issues/42",
    created_at="2026-09-22T10:00:00Z",
    labels=("agent-ready",),
)


class ManualRunTests(unittest.TestCase):
    def test_prompt_allows_draft_pr_but_forbids_merge_and_deploy(self):
        prompt = dw_ticket_loop._manual_prompt(
            Path("/tmp/repo"), "acme/repo", [ISSUE], CONFIG
        )
        self.assertIn("open a DRAFT pull request", prompt)
        self.assertIn("Do NOT merge a PR, deploy", prompt)
        self.assertIn("directly to `main`", prompt)

    def test_disabled_agent_refuses_before_auth_or_github_mutation(self):
        config = dict(CONFIG)
        config["agent"] = {"enabled": False}
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            dw_ticket_loop._run_manual(Path("/tmp/repo"), config, 1)

    @patch.object(dw_ticket_loop, "_selection", return_value=("acme/repo", "agent-ready", []))
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    def test_empty_queue_does_not_start_claude(self, _auth, _selection):
        with patch.object(dw_ticket_loop.subprocess, "run") as runner:
            self.assertEqual(
                dw_ticket_loop._run_manual(Path("/tmp/repo"), CONFIG, 1), 0
            )
            runner.assert_not_called()

    @patch.object(
        dw_ticket_loop,
        "_selection",
        return_value=("acme/repo", "agent-ready", [ISSUE]),
    )
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    @patch.object(dw_ticket_loop, "set_claimed")
    def test_manual_run_uses_fable_coordinator_and_opus_implementer(
        self, set_claimed_mock, _auth, _selection
    ):
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(dw_ticket_loop.subprocess, "run", return_value=completed) as runner:
            self.assertEqual(
                dw_ticket_loop._run_manual(Path("/tmp/repo"), CONFIG, 1), 0
            )

        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "claude-fable-5-1")
        agents = json.loads(command[command.index("--agents") + 1])
        self.assertEqual(agents["implementer"]["model"], "claude-opus-5")
        set_claimed_mock.assert_called_once_with(
            "acme/repo", 42, "agent-ready", "agent-claimed", claimed=True
        )

    @patch.object(
        dw_ticket_loop,
        "_selection",
        return_value=("acme/repo", "agent-ready", [ISSUE]),
    )
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    @patch.object(dw_ticket_loop, "set_claimed")
    def test_codex_run_uses_astra_low_and_requests_sol_medium_worker(
        self, _set_claimed, _auth, _selection
    ):
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(dw_ticket_loop.subprocess, "run", return_value=completed) as runner:
            self.assertEqual(
                dw_ticket_loop._run_manual(
                    Path("/tmp/repo"), CONFIG, 1, engine="codex"
                ),
                0,
            )

        command = runner.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="low"', command)
        prompt = command[-1]
        self.assertIn("`gpt-5.6-sol` at `medium` reasoning", prompt)
        self.assertIn("If exact-model worker delegation is unavailable", prompt)

    @patch.object(
        dw_ticket_loop,
        "_selection",
        return_value=("acme/repo", "agent-ready", [ISSUE]),
    )
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    @patch.object(dw_ticket_loop, "set_claimed")
    def test_failed_claude_run_restores_ready_label(
        self, set_claimed_mock, _auth, _selection
    ):
        completed = subprocess.CompletedProcess([], 7)
        with patch.object(dw_ticket_loop.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "status 7"):
                dw_ticket_loop._run_manual(Path("/tmp/repo"), CONFIG, 1)

        self.assertEqual(set_claimed_mock.call_count, 2)
        self.assertTrue(set_claimed_mock.call_args_list[0].kwargs["claimed"])
        self.assertFalse(set_claimed_mock.call_args_list[1].kwargs["claimed"])


if __name__ == "__main__":
    unittest.main()
