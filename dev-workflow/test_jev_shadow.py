import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import jev_shadow


MESSAGE = {
    "message_id": 42,
    "text": "Do not fix that finding on #449; it is intended behavior.",
    "reply_to_text": "Review comment about company sharing",
}
PROGRESS = {
    "issues": {
        "449": {
            "phase": "pr_opened",
            "title": "Private issue title",
            "detail": "Private implementation details",
        }
    }
}


class JevShadowTests(unittest.TestCase):
    def test_state_contains_only_bounded_routing_context(self):
        state = jev_shadow._state(MESSAGE, PROGRESS)
        self.assertEqual(state["active_issues"], [{"issue": "449", "phase": "pr_opened"}])
        self.assertNotIn("Private issue title", json.dumps(state))
        self.assertNotIn("Private implementation details", json.dumps(state))
        self.assertEqual(len(jev_shadow._state({"text": "x" * 5000}, {})["message"]), 2000)

    @patch.object(jev_shadow.request, "urlopen")
    def test_evaluate_uses_typed_questions_and_checks_result(self, urlopen):
        result = {
            "answers": {
                "intent": {"choice": "review_feedback", "confidence": 0.93},
                "rejects_review_finding": {"noul": 0.97},
            }
        }
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
        answer = jev_shadow.evaluate(MESSAGE, PROGRESS, "test-key")
        self.assertEqual(answer["action"], "review_feedback")
        self.assertEqual(answer["rejects_review_finding"], 0.97)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["model"], "jev-1.13.0")
        self.assertEqual(sent["questions"]["intent"]["type"], "choice")
        self.assertEqual(sent["questions"]["rejects_review_finding"]["type"], "noul")
        self.assertNotIn("Private issue title", json.dumps(sent))

    @patch.object(jev_shadow.request, "urlopen")
    def test_invalid_result_is_rejected(self, urlopen):
        result = {
            "answers": {
                "intent": {"choice": "merge_pr", "confidence": 0.99},
                "rejects_review_finding": {"noul": 0.01},
            }
        }
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
        with self.assertRaises(ValueError):
            jev_shadow.evaluate(MESSAGE, PROGRESS, "test-key")

    @patch.object(jev_shadow.request, "urlopen")
    def test_disabled_shadow_never_calls_external_api(self, urlopen):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(jev_shadow.os.environ, {"DW_JEV_SHADOW": "0", "TYPESAFE_API_KEY": "test"}):
                self.assertFalse(
                    jev_shadow.observe_async(MESSAGE, PROGRESS, "review_feedback", Path(directory))
                )
            urlopen.assert_not_called()
            self.assertFalse((Path(directory) / "jev-shadow.jsonl").exists())

    @patch.object(jev_shadow, "evaluate")
    def test_observation_records_comparison_without_message_text(self, evaluate):
        evaluate.return_value = {
            "action": "review_feedback",
            "confidence": 0.9,
            "rejects_review_finding": 0.95,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev-shadow.jsonl"
            self.assertTrue(jev_shadow._SLOTS.acquire(blocking=False))
            jev_shadow._observe(MESSAGE, PROGRESS, "status", "test", path)
            record = json.loads(path.read_text())
            self.assertEqual(record["live_action"], "status")
            self.assertFalse(record["agrees"])
            self.assertNotIn(MESSAGE["text"], path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @patch.object(jev_shadow, "evaluate", side_effect=RuntimeError("secret in error"))
    def test_observation_does_not_log_error_details(self, _evaluate):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev-shadow.jsonl"
            self.assertTrue(jev_shadow._SLOTS.acquire(blocking=False))
            jev_shadow._observe(MESSAGE, PROGRESS, "status", "test", path)
            self.assertIn('"error_type":"RuntimeError"', path.read_text())
            self.assertNotIn("secret in error", path.read_text())

    def test_summary_reports_disagreements_without_message_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev-shadow.jsonl"
            jev_shadow._append_result(
                path,
                {
                    "message_id": 42,
                    "live_action": "status",
                    "jev": {"action": "answer", "confidence": 0.8},
                    "agrees": False,
                },
            )
            jev_shadow._append_result(
                path, {"message_id": 43, "live_action": "status", "agrees": True}
            )
            report = jev_shadow.summarize(path)
            self.assertEqual(report["counts"], {"observations": 2, "disagreements": 1, "agreements": 1})
            self.assertEqual(report["disagreements"][0]["message_id"], 42)

    def test_live_decision_log_contains_metadata_only(self):
        with tempfile.TemporaryDirectory() as directory:
            jev_shadow.record_live_decision(
                Path(directory), 42,
                {"action": "review_feedback", "confidence": 0.9,
                 "rejects_review_finding": 0.01},
                "review_feedback", "clarify", None,
            )
            path = Path(directory) / "jev-live.jsonl"
            record = json.loads(path.read_text())
            self.assertEqual(record["final_action"], "clarify")
            self.assertNotIn(MESSAGE["text"], path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
