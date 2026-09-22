#!/usr/bin/env python3
"""Unit tests for validate.py.

The dev-workflow/ directory has a hyphen, so it is not an importable
package — we insert this file's own directory on sys.path and import the
sibling `validate` module directly. Run with:

    python3 dev-workflow/test_validate.py
"""
import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402  (import after sys.path insert)

import validate  # noqa: E402


# A minimal config that satisfies every REQUIRED field.
MINIMAL = {
    "repo": {"base_branch": "dev", "prod_branch": "main"},
    "tracker": {"provider": "linear", "team": "Acme", "ticket_prefix": "ABC"},
}


def _errors_for(config):
    """Dump `config` to a temp file and return validate_file's error list."""
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        yaml.safe_dump(config, fh)
        path = fh.name
    try:
        return validate.validate_file(path)
    finally:
        os.unlink(path)


class ValidateTests(unittest.TestCase):
    def test_minimal_valid_config_passes(self):
        self.assertEqual(_errors_for(copy.deepcopy(MINIMAL)), [])

    def test_unknown_top_key_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["bogus"] = {"x": 1}
        errors = _errors_for(cfg)
        self.assertTrue(any("bogus" in e for e in errors), errors)

    def test_cap_per_pass_over_ceiling_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["build"] = {"cap_per_pass": 4}
        errors = _errors_for(cfg)
        self.assertTrue(any("cap_per_pass" in e for e in errors), errors)

    def test_cap_per_pass_three_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["build"] = {"cap_per_pass": 3}
        self.assertEqual(_errors_for(cfg), [])

    def test_build_model_fields_accept_strings(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["build"] = {"model": "opus", "subagent_model": "sonnet"}
        self.assertEqual(_errors_for(cfg), [])

    def test_build_model_fields_reject_non_strings(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["build"] = {"model": "", "subagent_model": 5}
        errors = _errors_for(cfg)
        self.assertTrue(any("build.model" in e for e in errors), errors)
        self.assertTrue(any("build.subagent_model" in e for e in errors), errors)

    def test_max_lines_over_ceiling_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["guardrails"] = {"diff_budget": {"max_lines": 900}}
        errors = _errors_for(cfg)
        self.assertTrue(any("max_lines" in e for e in errors), errors)

    def test_missing_tracker_team_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        del cfg["tracker"]["team"]
        errors = _errors_for(cfg)
        self.assertTrue(any("tracker.team" in e for e in errors), errors)

    def test_github_tracker_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"] = {
            "provider": "github",
            "repo": "ankurmadharia18/super-singularity",
        }
        self.assertEqual(_errors_for(cfg), [])

    def test_github_tracker_requires_repo(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"] = {"provider": "github"}
        errors = _errors_for(cfg)
        self.assertTrue(any("tracker.repo" in e for e in errors), errors)

    def test_github_tracker_does_not_require_linear_fields(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"] = {
            "provider": "github",
            "repo": "ankurmadharia18/super-singularity",
        }
        self.assertEqual(_errors_for(cfg), [])

    def test_tracker_project_optional_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["project"] = "acme-api"
        self.assertEqual(_errors_for(cfg), [])

    def test_tracker_project_empty_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["project"] = ""
        errors = _errors_for(cfg)
        self.assertTrue(any("tracker.project" in e for e in errors), errors)

    def test_agent_skill_and_manager_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"enabled": True, "skill": "ticket-loop-parent", "manager": True}
        self.assertEqual(_errors_for(cfg), [])

    def test_agent_skill_empty_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"skill": ""}
        errors = _errors_for(cfg)
        self.assertTrue(any("agent.skill" in e for e in errors), errors)

    def test_agent_manager_non_bool_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"manager": "yes"}
        errors = _errors_for(cfg)
        self.assertTrue(any("agent.manager" in e for e in errors), errors)

    def test_repos_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt-api", "path": "pt-api", "url": "https://x/y.git"}]
        self.assertEqual(_errors_for(cfg), [])

    def test_repos_missing_project_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"path": "pt-api"}]
        errors = _errors_for(cfg)
        self.assertTrue(any("repos[0].project" in e for e in errors), errors)

    def test_repos_empty_list_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = []
        errors = _errors_for(cfg)
        self.assertTrue(any("repos must be a non-empty list" in e for e in errors), errors)

    def test_agent_skill_with_colon_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"skill": "dev-workflow:ticket-loop-parent"}
        errors = _errors_for(cfg)
        self.assertTrue(any("bare skill name" in e for e in errors), errors)

    def test_repos_duplicate_project_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt", "path": "a"}, {"project": "pt", "path": "b"}]
        errors = _errors_for(cfg)
        self.assertTrue(any("duplicate project" in e for e in errors), errors)

    def test_parent_repos_with_tracker_project_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt-api", "path": "pt-api"}]
        cfg["tracker"]["project"] = "pt-api"     # a parent must NOT scope to one repo
        errors = _errors_for(cfg)
        self.assertTrue(any("tracker.project must NOT be set when repos" in e for e in errors), errors)

    def test_intake_project_valid_parent_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt-api", "path": "pt-api"}]
        cfg["tracker"].pop("project", None)   # a parent must NOT scope to one repo
        cfg["tracker"]["intake_project"] = "Intake"   # distinct from any repos project
        self.assertEqual(_errors_for(cfg), [])

    def test_intake_project_equal_to_repo_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt-api", "path": "pt-api"}]
        cfg["tracker"]["intake_project"] = "pt-api"   # collides with a repos project
        errors = _errors_for(cfg)
        self.assertTrue(any("must NOT be one of the repos: projects" in e for e in errors), errors)

    def test_intake_project_without_repos_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["intake_project"] = "Intake"   # no repos: present
        errors = _errors_for(cfg)
        self.assertTrue(any("only meaningful in a parent config" in e for e in errors), errors)

    def test_intake_project_empty_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["repos"] = [{"project": "pt-api", "path": "pt-api"}]
        cfg["tracker"]["intake_project"] = ""
        errors = _errors_for(cfg)
        self.assertTrue(any("intake_project must be a non-empty string" in e for e in errors), errors)

    def test_off_limits_non_list_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["guardrails"] = {"off_limits": "*.pem"}
        errors = _errors_for(cfg)
        self.assertTrue(any("off_limits" in e for e in errors), errors)

    def test_blog_non_mapping_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["blog"] = "docs/blog"
        errors = _errors_for(cfg)
        self.assertTrue(any("blog" in e for e in errors), errors)

    def test_valid_blog_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["blog"] = {"skill": "blog-from-session", "posts_dir": "docs/blog"}
        self.assertEqual(_errors_for(cfg), [])

    # ── board (Epic A) + optional roles (Epics C/D) ──────────────────────────
    def test_board_gates_and_prune_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {
            "views": ".local/board",
            "gates": ["publifai", "launch", "migrate"],
            "prune": {"allow_delete": False, "threshold_days": 7},
        }
        self.assertEqual(_errors_for(cfg), [])

    def test_board_omitted_still_validates(self):
        # A repo without any board keys is still valid (all optional).
        self.assertEqual(_errors_for(copy.deepcopy(MINIMAL)), [])

    def test_board_gates_non_list_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"gates": "publifai"}
        errors = _errors_for(cfg)
        self.assertTrue(any("board.gates" in e for e in errors), errors)

    def test_board_gates_non_string_item_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"gates": ["publifai", 3]}
        errors = _errors_for(cfg)
        self.assertTrue(any("board.gates" in e for e in errors), errors)

    def test_prune_allow_delete_non_bool_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"prune": {"allow_delete": "yes"}}
        errors = _errors_for(cfg)
        self.assertTrue(any("allow_delete" in e for e in errors), errors)

    def test_prune_threshold_zero_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"prune": {"threshold_days": 0}}
        errors = _errors_for(cfg)
        self.assertTrue(any("threshold_days" in e for e in errors), errors)

    def test_prune_threshold_non_int_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"prune": {"threshold_days": True}}
        errors = _errors_for(cfg)
        self.assertTrue(any("threshold_days" in e for e in errors), errors)

    def test_prune_non_mapping_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["board"] = {"prune": "soon"}
        errors = _errors_for(cfg)
        self.assertTrue(any("board.prune" in e for e in errors), errors)

    def test_roles_flagged_and_dep_blocked_valid_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["roles"] = {
            "queue": {"label": "agent", "states": ["Todo"]},
            "blocked": {"label": "agent-blocked"},
            "done": {"state": "Done"},
            "flagged": {"label": "flagged"},
            "dep_blocked": {"label": "dep-blocked"},
        }
        self.assertEqual(_errors_for(cfg), [])

    def test_roles_flagged_missing_label_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["roles"] = {
            "queue": {"label": "agent", "states": ["Todo"]},
            "blocked": {"label": "agent-blocked"},
            "done": {"state": "Done"},
            "flagged": {"note": "oops"},
        }
        errors = _errors_for(cfg)
        self.assertTrue(any("flagged" in e for e in errors), errors)

    def test_roles_dep_blocked_empty_label_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["tracker"]["roles"] = {
            "queue": {"label": "agent", "states": ["Todo"]},
            "blocked": {"label": "agent-blocked"},
            "done": {"state": "Done"},
            "dep_blocked": {"label": "   "},
        }
        errors = _errors_for(cfg)
        self.assertTrue(any("dep_blocked" in e for e in errors), errors)


    # ── agent (v2 local-agent opt-in) ────────────────────────────────────────
    def test_agent_absent_still_validates(self):
        # No agent section at all → valid (feature defaults OFF).
        cfg = copy.deepcopy(MINIMAL)
        self.assertEqual(_errors_for(cfg), [])

    def test_agent_enabled_true_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"enabled": True}
        self.assertEqual(_errors_for(cfg), [])

    def test_agent_enabled_false_passes(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"enabled": False}
        self.assertEqual(_errors_for(cfg), [])

    def test_agent_enabled_non_bool_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"enabled": "yes"}
        errors = _errors_for(cfg)
        self.assertTrue(any("agent.enabled" in e for e in errors), errors)

    def test_agent_enabled_int_fails(self):
        # bool is a subclass of int; a plain int must still be rejected.
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = {"enabled": 1}
        errors = _errors_for(cfg)
        self.assertTrue(any("agent.enabled" in e for e in errors), errors)

    def test_agent_non_mapping_fails(self):
        cfg = copy.deepcopy(MINIMAL)
        cfg["agent"] = "true"
        errors = _errors_for(cfg)
        self.assertTrue(any("agent" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
