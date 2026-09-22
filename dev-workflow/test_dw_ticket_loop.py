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
    def test_interactive_claude_model_selection_uses_answers(self):
        answers = iter(["sonnet", "opus"])
        coordinator, implementer = dw_ticket_loop._resolve_models(
            CONFIG,
            "claude",
            None,
            None,
            ask=True,
            input_fn=lambda _prompt: next(answers),
        )
        self.assertEqual(coordinator, "sonnet")
        self.assertEqual(implementer, "opus")

    def test_interactive_claude_model_selection_keeps_defaults_on_enter(self):
        coordinator, implementer = dw_ticket_loop._resolve_models(
            CONFIG,
            "claude",
            None,
            None,
            ask=True,
            input_fn=lambda _prompt: "",
        )
        self.assertEqual(coordinator, "claude-fable-5-1")
        self.assertEqual(implementer, "claude-opus-5")

    def test_model_flags_override_prompts(self):
        coordinator, implementer = dw_ticket_loop._resolve_models(
            CONFIG,
            "claude",
            "sonnet",
            "sonnet",
            ask=True,
            input_fn=lambda _prompt: self.fail("override should skip prompt"),
        )
        self.assertEqual((coordinator, implementer), ("sonnet", "sonnet"))

    def test_prompt_allows_draft_pr_but_forbids_merge_and_deploy(self):
        prompt = dw_ticket_loop._manual_prompt(
            Path("/tmp/repo"), "acme/repo", [ISSUE], CONFIG
        )
        self.assertIn("open a DRAFT pull request", prompt)
        self.assertIn("Do NOT merge a PR, deploy", prompt)
        self.assertIn("directly to `main`", prompt)

    def test_claude_outcomes_are_read_from_structured_output(self):
        output = json.dumps(
            {
                "structured_output": {
                    "issues": [
                        {
                            "number": 42,
                            "status": "needs_input",
                            "summary": "A decision is required.",
                            "question": "Which behavior should win?",
                            "pr_url": "",
                        }
                    ]
                }
            }
        )
        outcomes = dw_ticket_loop._load_claude_outcomes(output)
        self.assertEqual(outcomes[42]["status"], "needs_input")

    def test_claude_outcomes_fall_back_to_json_result(self):
        payload = {
            "issues": [
                {
                    "number": 42,
                    "status": "pr_opened",
                    "summary": "Draft PR opened.",
                    "question": "",
                    "pr_url": "https://github.com/acme/repo/pull/7",
                }
            ]
        }
        output = json.dumps(
            {
                "result": "```json\n%s\n```" % json.dumps(payload),
                "structured_output": None,
            }
        )
        outcomes = dw_ticket_loop._load_claude_outcomes(output)
        self.assertEqual(outcomes[42]["pr_url"], payload["issues"][0]["pr_url"])

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
    @patch.object(
        dw_ticket_loop,
        "_load_claude_outcomes",
        return_value={42: {"status": "pr_opened"}},
    )
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    @patch.object(dw_ticket_loop, "set_claimed")
    def test_manual_run_uses_fable_coordinator_and_opus_implementer(
        self, set_claimed_mock, _auth, _outcomes, _selection
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
    @patch.object(
        dw_ticket_loop,
        "_load_claude_outcomes",
        return_value={
            42: {
                "status": "needs_input",
                "question": "Should retries remain manual?",
            }
        },
    )
    @patch.object(dw_ticket_loop, "comment_issue")
    @patch.object(dw_ticket_loop, "_send_telegram_question")
    @patch.object(dw_ticket_loop, "_engine_is_authenticated", return_value=True)
    @patch.object(dw_ticket_loop, "set_blocked")
    @patch.object(dw_ticket_loop, "set_claimed")
    def test_needs_input_restores_ready_blocks_and_sends_question(
        self,
        set_claimed_mock,
        set_blocked_mock,
        _auth,
        send_question_mock,
        comment_issue_mock,
        _outcomes,
        _selection,
    ):
        events = []
        set_claimed_mock.side_effect = lambda *args, **kwargs: events.append(
            "claim" if kwargs["claimed"] else "restore"
        )
        send_question_mock.side_effect = lambda *args, **kwargs: events.append("send")
        comment_issue_mock.side_effect = lambda *args, **kwargs: events.append("comment")
        set_blocked_mock.side_effect = lambda *args, **kwargs: events.append("block")
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(dw_ticket_loop.subprocess, "run", return_value=completed):
            self.assertEqual(
                dw_ticket_loop._run_manual(Path("/tmp/repo"), CONFIG, 1), 0
            )

        self.assertEqual(set_claimed_mock.call_count, 2)
        self.assertTrue(set_claimed_mock.call_args_list[0].kwargs["claimed"])
        self.assertFalse(set_claimed_mock.call_args_list[1].kwargs["claimed"])
        set_blocked_mock.assert_called_once_with(
            "acme/repo", 42, "agent-blocked", blocked=True
        )
        send_question_mock.assert_called_once()
        comment_issue_mock.assert_called_once()
        self.assertEqual(events, ["claim", "send", "comment", "block", "restore"])

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
