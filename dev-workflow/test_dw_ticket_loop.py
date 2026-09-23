#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dw_ticket_loop
from github_issues import GitHubIssue, GitHubPullRequest


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
    def test_completion_message_includes_draft_pr_summary_and_url(self):
        progress = {
            "issues": {
                "449": {
                    "phase": "pr_opened",
                    "detail": "Company-level sharing settings implemented and verified.",
                    "pr_url": "https://github.com/acme/repo/pull/10",
                }
            }
        }
        message = dw_ticket_loop._render_run_completion(
            progress, [449], success=True
        )
        self.assertIn("✅ #449 — draft PR ready for review", message)
        self.assertIn("Company-level sharing settings implemented", message)
        self.assertIn("https://github.com/acme/repo/pull/10", message)
        self.assertIn("\n", message)
        self.assertNotIn("\\n", message)

    def test_completion_message_distinguishes_waiting_and_failure(self):
        waiting = {
            "issues": {
                "449": {
                    "phase": "needs_input",
                    "detail": "Choose the company override behavior.",
                }
            }
        }
        self.assertIn(
            "⏸️ #449 — waiting for your answer",
            dw_ticket_loop._render_run_completion(waiting, [449], success=True),
        )
        failed = {
            "issues": {
                "449": {
                    "phase": "testing",
                    "detail": "Typecheck could not complete.",
                }
            }
        }
        message = dw_ticket_loop._render_run_completion(failed, [449], success=False)
        self.assertIn("⚠️ #449 — run failed", message)
        self.assertIn("Typecheck could not complete.", message)

    def test_review_evidence_question_is_left_for_natural_language_router(self):
        progress = {"issues": {"449": {"phase": "pr_opened"}}}
        self.assertIsNone(
            dw_ticket_loop._natural_status_numbers(
                "Have you run the tests? The reviewer could not run them.", progress
            )
        )

    def test_plain_english_status_question_honors_explicit_issue(self):
        progress = {"issues": {"449": {"phase": "pr_opened"}}}
        self.assertEqual(
            dw_ticket_loop._natural_status_numbers(
                "Was review done for #997?", progress
            ),
            [997],
        )

    @patch.object(dw_ticket_loop.subprocess, "run")
    def test_listener_router_is_restricted_toolless_and_structured(self, run):
        payload = {
            "action": "review_feedback",
            "issues": [449],
            "reply": "",
            "worker_model": "",
        }
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"structured_output": payload}),
        )
        route = dw_ticket_loop._route_listener_message(
            {"text": "Please handle the review comments"},
            {"coordinator": {"model": "fable"}, "issues": {"449": {}}},
            CONFIG,
            Path("/repo"),
        )
        self.assertEqual(route, payload)
        command = run.call_args.args[0]
        self.assertIn("--restricted", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("Please handle the review comments", command[2])
        self.assertIn('"issue": "449"', command[2])
        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")

    def test_router_distinguishes_independent_review_and_direct_answers(self):
        schema = dw_ticket_loop._listener_route_schema()
        actions = schema["properties"]["action"]["enum"]
        self.assertIn("independent_review", actions)
        self.assertIn("answer", actions)
        prompt = dw_ticket_loop._listener_router_prompt(
            {"text": "Launch a separate fresh Opus reviewer agent."},
            {
                "issues": {
                    "449": {
                        "phase": "pr_opened",
                        "detail": "101 affected tests pass.",
                    }
                }
            },
        )
        self.assertIn("fresh independent reviewer is never", prompt)
        self.assertIn("101 affected tests pass", prompt)
        self.assertIn("worker_model", prompt)

    def test_unrelated_chatter_is_not_misclassified_as_status(self):
        progress = {"issues": {"449": {"phase": "pr_opened"}}}
        self.assertIsNone(
            dw_ticket_loop._natural_status_numbers("Lunch is ready", progress)
        )

    def test_orchestrated_outcome_is_atomic_and_uses_external_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            payload = {
                "picked": 1,
                "pr_opened": 1,
                "asked": 0,
                "blocked": 0,
                "progressed": True,
                "error": None,
            }
            with patch.dict(
                dw_ticket_loop.os.environ,
                {"DW_ORCHESTRATED": "1", "TICKET_LOOP_STATE_DIR": str(state)},
            ):
                dw_ticket_loop._write_pass_outcome(root, CONFIG, payload)
            self.assertEqual(json.loads((state / "outcome.json").read_text()), payload)
            self.assertFalse((state / "outcome.tmp").exists())

    def test_non_orchestrated_run_does_not_write_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(dw_ticket_loop.os.environ, {}, clear=True):
                dw_ticket_loop._write_pass_outcome(root, CONFIG, {"picked": 0})
            self.assertFalse((root / ".agent-loop" / "outcome.json").exists())

    @patch.object(dw_ticket_loop, "_keychain_value")
    def test_container_telegram_runtime_prefers_environment(self, keychain):
        config = {
            "chat": {"provider": "telegram"},
            "runtime": {"state_dir": ".local/agent-loop"},
        }
        with patch.dict(
            dw_ticket_loop.os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "from-env",
                "AGENT_TELEGRAM_CHAT_ID": "-100123",
                "TICKET_LOOP_STATE_DIR": "/home/agent/state/widgets",
            },
            clear=True,
        ):
            _bridge, env = dw_ticket_loop._telegram_runtime(Path("/repo"), config)
        keychain.assert_not_called()
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "from-env")
        self.assertEqual(env["AGENT_TELEGRAM_CHAT_ID"], "-100123")
        self.assertEqual(env["TICKET_LOOP_STATE_DIR"], "/home/agent/state/widgets")

    @patch.object(dw_ticket_loop, "_keychain_value")
    def test_container_telegram_runtime_falls_back_to_packaged_bridge(self, keychain):
        config = {"chat": {"provider": "telegram"}}
        with patch.dict(
            dw_ticket_loop.os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "from-env",
                "AGENT_TELEGRAM_CHAT_ID": "-100123",
            },
            clear=True,
        ), patch.object(Path, "is_file", side_effect=[False, True]):
            bridge, _env = dw_ticket_loop._telegram_runtime(Path("/repo"), config)
        keychain.assert_not_called()
        self.assertEqual(Path(bridge[1]).name, "telegram.py")
        self.assertEqual(Path(bridge[1]).parent, Path(dw_ticket_loop.__file__).parent)

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

    def test_prompt_requires_live_coordinator_and_implementer_progress(self):
        prompt = dw_ticket_loop._manual_prompt(
            Path("/tmp/repo"), "acme/repo", [ISSUE], CONFIG
        )
        self.assertIn("Live progress reporting is mandatory", prompt)
        self.assertIn("--actor <coordinator|implementer|reviewer>", prompt)
        self.assertIn("this exact progress command and requirement", prompt)

    def test_independent_review_prompt_requires_fresh_read_only_reviewer(self):
        prompt = dw_ticket_loop._manual_prompt(
            Path("/tmp/repo"),
            "acme/repo",
            [ISSUE],
            CONFIG,
            intent="independent-review",
        )
        self.assertIn("FRESH INDEPENDENT REVIEW", prompt)
        self.assertIn("has not participated in implementation", prompt)
        self.assertIn("run the relevant tests/lint/typecheck", prompt)
        self.assertIn("Do not edit application code", prompt)
        self.assertIn("EXISTING draft", prompt)

    def test_independent_review_completion_is_not_called_implementation(self):
        progress = {
            "intent": "independent-review",
            "issues": {
                "449": {
                    "phase": "pr_opened",
                    "detail": "Fresh review found no blocking issues.",
                    "pr_url": "https://github.com/acme/repo/pull/10",
                }
            },
        }
        message = dw_ticket_loop._render_run_completion(
            progress, [449], success=True
        )
        self.assertIn("independent review completed", message)
        self.assertNotIn("draft PR ready", message)

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
                            "options": ["Keep the current behavior", "Use the new behavior"],
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
                    "options": [],
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

    @patch.object(
        dw_ticket_loop,
        "_telegram_runtime",
        return_value=(["python3", "telegram.py"], {}),
    )
    def test_telegram_question_has_paragraphs_and_bulleted_options(self, _runtime):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            dw_ticket_loop.subprocess, "run", return_value=completed
        ) as runner:
            dw_ticket_loop._send_telegram_question(
                Path("/tmp/repo"),
                CONFIG,
                ISSUE,
                "The existing behavior has two safe alternatives.",
                "Which behavior should win?",
                ["Keep the current behavior", "Use the new behavior"],
            )

        message = runner.call_args.args[0][-1]
        self.assertIn("\n\nDecision needed:\nWhich behavior should win?", message)
        self.assertIn(
            "\n\nOptions:\n• A — Keep the current behavior\n"
            "• B — Use the new behavior",
            message,
        )
        self.assertTrue(message.endswith("choice or answer."))

    @patch.object(
        dw_ticket_loop,
        "_telegram_runtime",
        return_value=(["python3", "telegram.py"], {}),
    )
    def test_telegram_question_removes_duplicate_option_letters(self, _runtime):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            dw_ticket_loop.subprocess, "run", return_value=completed
        ) as runner:
            dw_ticket_loop._send_telegram_question(
                Path("/tmp/repo"),
                CONFIG,
                ISSUE,
                "Summary.",
                "Choose one.",
                ["A) First", "B. Second"],
            )
        message = runner.call_args.args[0][-1]
        self.assertIn("• A — First", message)
        self.assertIn("• B — Second", message)
        self.assertNotIn("A — A)", message)

    def test_reply_to_hash_issue_acknowledgement_is_routable(self):
        message = {
            "ticket": None,
            "reply_to_text": "⏸️ #865 remains on hold — understood.",
        }
        self.assertEqual(dw_ticket_loop._issue_number_from_message(message), 865)

    def test_deferrals_are_detected(self):
        self.assertTrue(dw_ticket_loop._is_deferral("I dont know yet. Wait."))
        self.assertTrue(dw_ticket_loop._is_deferral("We will do it next week"))
        self.assertFalse(dw_ticket_loop._is_deferral("Choose option A"))

    def test_transient_telegram_timeouts_are_retryable(self):
        self.assertTrue(
            dw_ticket_loop._is_transient_telegram_error(
                RuntimeError(
                    "Telegram command failed: telegram getUpdates unreachable: "
                    "The read operation timed out"
                )
            )
        )
        self.assertFalse(
            dw_ticket_loop._is_transient_telegram_error(
                RuntimeError("missing macOS Keychain item")
            )
        )

    @patch.object(dw_ticket_loop, "_send_telegram_text")
    @patch.object(dw_ticket_loop, "set_blocked")
    @patch.object(dw_ticket_loop, "comment_issue")
    @patch.object(
        dw_ticket_loop,
        "_telegram_runtime",
        return_value=(["python3", "telegram.py"], {}),
    )
    @patch.object(
        dw_ticket_loop,
        "_open_telegram_questions",
        return_value=[{"message_id": "10", "ticket": "SS-865"}],
    )
    def test_single_open_question_routes_standalone_deferral_but_keeps_blocked(
        self,
        _questions,
        _runtime,
        comment_mock,
        blocked_mock,
        send_mock,
    ):
        poll = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "message_id": 11,
                    "text": "I dont know yet. Wait.",
                    "ticket": None,
                    "reply_to_text": None,
                }
            )
            + "\n",
            "",
        )
        cleared = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            dw_ticket_loop,
            "_telegram_command",
            side_effect=[poll, cleared],
        ):
            unhandled = dw_ticket_loop._poll_telegram_answers(
                Path("/tmp/repo"), CONFIG, "acme/repo", "agent-blocked"
            )

        self.assertEqual(unhandled, [])
        comment_mock.assert_called_once_with(
            "acme/repo", 865, "📩 Answer via Telegram: I dont know yet. Wait."
        )
        blocked_mock.assert_called_once_with(
            "acme/repo", 865, "agent-blocked", blocked=True
        )
        self.assertEqual(send_mock.call_args.kwargs["ticket"], "SS-865")

    @patch.object(dw_ticket_loop, "_send_telegram_text")
    @patch.object(dw_ticket_loop, "set_blocked")
    @patch.object(dw_ticket_loop, "comment_issue")
    @patch.object(
        dw_ticket_loop,
        "_telegram_runtime",
        return_value=(["python3", "telegram.py"], {}),
    )
    @patch.object(dw_ticket_loop, "_open_telegram_questions", return_value=[])
    def test_reply_to_hash_issue_routes_answer_and_unblocks(
        self,
        _questions,
        _runtime,
        comment_mock,
        blocked_mock,
        send_mock,
    ):
        poll = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "message_id": 13,
                    "text": "Choose option A",
                    "ticket": None,
                    "reply_to_text": "⏸️ #865 remains on hold",
                }
            )
            + "\n",
            "",
        )
        cleared = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            dw_ticket_loop,
            "_telegram_command",
            side_effect=[poll, cleared],
        ):
            dw_ticket_loop._poll_telegram_answers(
                Path("/tmp/repo"), CONFIG, "acme/repo", "agent-blocked"
            )
        blocked_mock.assert_called_once_with(
            "acme/repo", 865, "agent-blocked", blocked=False
        )
        self.assertNotIn("ticket", send_mock.call_args.kwargs)

    def test_start_command_accepts_three_issues_and_model_overrides(self):
        parsed = dw_ticket_loop._parse_start_command(
            "start 995 #996 997 coordinator=opus implementer=sonnet"
        )
        self.assertEqual(parsed, ([995, 996, 997], "opus", "sonnet"))

    def test_models_command_supports_defaults_and_explicit_models(self):
        self.assertEqual(
            dw_ticket_loop._parse_models_command("models default"), ("", "")
        )
        self.assertEqual(
            dw_ticket_loop._parse_models_command("models opus opus"),
            ("opus", "opus"),
        )

    def test_status_command_accepts_issue_numbers_and_rejects_bad_syntax(self):
        self.assertEqual(dw_ticket_loop._parse_status_command("Status"), [])
        self.assertEqual(
            dw_ticket_loop._parse_status_command("status #995 996"), [995, 996]
        )
        with self.assertRaisesRegex(ValueError, "Use: status"):
            dw_ticket_loop._parse_status_command("status agents")

    def test_progress_ledger_persists_implementer_phase_and_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            config = {"runtime": {"state_dir": ".local/agent-loop"}}
            run_id = dw_ticket_loop._begin_progress_run(
                repo, config, [ISSUE], "opus", "opus"
            )
            self.assertTrue(
                dw_ticket_loop._update_progress(
                    repo,
                    config,
                    run_id=run_id,
                    issue_number=42,
                    phase="testing",
                    actor="implementer",
                    detail="Running the focused widget tests",
                )
            )
            dw_ticket_loop._finish_progress_run(
                repo, config, run_id, success=True
            )
            progress = dw_ticket_loop._read_progress(repo, config)

        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["coordinator"]["status"], "completed")
        self.assertEqual(progress["issues"]["42"]["phase"], "testing")
        self.assertEqual(progress["issues"]["42"]["actor"], "implementer")

    @patch.object(dw_ticket_loop, "find_issue_pull_request")
    @patch.object(dw_ticket_loop, "get_issue")
    def test_issue_status_renders_ticket_pr_and_checks(self, issue_mock, pull_mock):
        issue_mock.return_value = ISSUE
        pull_mock.return_value = GitHubPullRequest(
            number=77,
            title="Fix the widget",
            url="https://github.com/acme/repo/pull/77",
            state="OPEN",
            is_draft=True,
            merge_state_status="CLEAN",
            checks_passed=2,
            checks_pending=1,
        )
        rendered = dw_ticket_loop._github_issue_status("acme/repo", 42)
        self.assertIn("OPEN · agent-ready", rendered)
        self.assertIn("Draft PR #77: OPEN · CLEAN", rendered)
        self.assertIn("1 pending, 2 passed", rendered)

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
                "summary": "Retry behavior needs a product decision.",
                "question": "Should retries remain manual?",
                "options": ["Keep retries manual", "Automate retries"],
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
