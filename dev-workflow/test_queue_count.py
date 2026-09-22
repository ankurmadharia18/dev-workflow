#!/usr/bin/env python3
"""Offline unittests for queue-count.py — query construction + response parsing
only; the single urllib call in main() is never exercised here.

Run: python3 dev-workflow/test_queue_count.py
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("queue_count", HERE / "queue-count.py")
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)


CONFIG = """\
tracker:
  provider: linear
  team: Acme
  roles:
    queue:   { label: agent, states: [Todo, In Progress] }
    blocked: { label: agent-blocked }
    exclude: { labels: [manual, gated] }
    done:    { state: Done }
"""


def load_cfg(text=CONFIG):
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "dev-workflow.yml"
        p.write_text(text)
        return qc.load_config(str(p))


class TestReadRoles(unittest.TestCase):
    def test_reads_team_queue_and_excludes(self):
        data, mod = load_cfg()
        team, label, states, excludes, project = qc.read_roles(data, mod)
        self.assertEqual(team, "Acme")
        self.assertEqual(label, "agent")
        self.assertEqual(states, ["Todo", "In Progress"])
        self.assertEqual(excludes, ["manual", "gated"])
        self.assertIsNone(project)          # no tracker.project in CONFIG

    def test_missing_queue_role_errors(self):
        data, mod = load_cfg("tracker:\n  team: Acme\n")
        with self.assertRaises(SystemExit):
            qc.read_roles(data, mod)

    def test_no_exclude_role_is_empty_list(self):
        data, mod = load_cfg(
            "tracker:\n  team: Acme\n  roles:\n"
            "    queue: { label: agent, states: [Todo] }\n")
        _, _, _, excludes, _ = qc.read_roles(data, mod)
        self.assertEqual(excludes, [])

    def test_reads_optional_project(self):
        data, mod = load_cfg(
            "tracker:\n  team: Paytunes\n  project: paytunes-api\n  roles:\n"
            "    queue: { label: agent, states: [Todo] }\n")
        team, _, _, _, project = qc.read_roles(data, mod)
        self.assertEqual(team, "Paytunes")
        self.assertEqual(project, "paytunes-api")


class TestBuildPayload(unittest.TestCase):
    def test_filter_shape(self):
        payload = qc.build_payload("Acme", "agent", ["Todo", "In Progress"])
        f = payload["variables"]["filter"]
        self.assertEqual(f["team"]["name"]["eq"], "Acme")
        self.assertEqual(f["labels"]["name"]["eq"], "agent")
        self.assertEqual(f["state"]["name"]["in"], ["Todo", "In Progress"])
        self.assertIn("issues(filter: $filter", payload["query"])
        self.assertNotIn("project", f)      # no project → team-only, as before

    def test_project_scopes_filter(self):
        f = qc.build_payload("Paytunes", "agent", ["Todo"], project="paytunes-api")["variables"]["filter"]
        self.assertEqual(f["project"]["name"]["eq"], "paytunes-api")
        self.assertEqual(f["team"]["name"]["eq"], "Paytunes")  # team still scopes too

    def test_empty_project_omitted(self):
        f = qc.build_payload("Acme", "agent", ["Todo"], project=None)["variables"]["filter"]
        self.assertNotIn("project", f)


class TestCountEligible(unittest.TestCase):
    def body(self, nodes):
        return {"data": {"issues": {"nodes": nodes}}}

    def node(self, key, labels):
        return {"identifier": key,
                "labels": {"nodes": [{"name": n} for n in labels]}}

    def test_counts_and_drops_excluded(self):
        body = self.body([
            self.node("ABC-1", ["agent"]),
            self.node("ABC-2", ["agent", "manual"]),      # excluded
            self.node("ABC-3", ["agent", "Bug"]),
            self.node("ABC-4", ["agent", "GATED"]),       # excluded, case-insensitive
        ])
        self.assertEqual(qc.count_eligible(body, ["manual", "gated"]), 2)

    def test_empty(self):
        self.assertEqual(qc.count_eligible(self.body([]), ["manual"]), 0)


class TestProviderDispatch(unittest.TestCase):
    def test_missing_provider_preserves_linear_default(self):
        data, mod = load_cfg()
        self.assertEqual(qc.read_provider(data, mod), "linear")

    def test_reads_github_roles(self):
        data, mod = load_cfg(
            "tracker:\n"
            "  provider: github\n"
            "  repo: acme/widgets\n"
            "  roles:\n"
            "    queue: { label: agent-ready, states: [open] }\n"
            "    exclude: { labels: [agent-claimed, agent-blocked] }\n"
        )
        self.assertEqual(qc.read_provider(data, mod), "github")
        self.assertEqual(
            qc.read_github_roles(data, mod),
            ("acme/widgets", "agent-ready", ["agent-claimed", "agent-blocked"]),
        )

    def test_github_count_reuses_actionable_adapter(self):
        data, mod = load_cfg(
            "tracker:\n"
            "  provider: github\n"
            "  repo: acme/widgets\n"
            "  roles:\n"
            "    queue: { label: agent-ready, states: [open] }\n"
            "    exclude: { labels: [agent-claimed] }\n"
        )
        with patch.object(qc, "list_actionable", return_value=[object(), object()]) as lookup:
            self.assertEqual(qc.count_github(data, mod), 2)
        lookup.assert_called_once_with("acme/widgets", "agent-ready", ["agent-claimed"])


if __name__ == "__main__":
    unittest.main()
