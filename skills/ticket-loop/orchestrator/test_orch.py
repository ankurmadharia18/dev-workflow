#!/usr/bin/env python3
"""Stdlib unittests for orch.py — the orchestrator's scheduling brain (no network,
no docker). Import idiom mirrors test_telegram.py.

Run: python3 skills/ticket-loop/orchestrator/test_orch.py
(PyYAML must be importable — it is in the container image and on any machine that
can already run dev-workflow/test_validate.py.)
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("orch_mod", HERE / "orch.py")
orch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orch)

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


def make_roster_dir(tmp, projects_yaml=None, top_yaml=""):
    """Write a roster + valid work trees under tmp; returns the roster path.
    Default: one project `alpha` with the marker file present."""
    root = Path(tmp)
    wt = root / "alpha"
    wt.mkdir(exist_ok=True)
    (wt / ".dw-agent-clone").touch()
    (root / "alpha.env").touch()
    if projects_yaml is None:
        projects_yaml = f"""
projects:
  - name: alpha
    work_tree: {wt}
    env_file: {root}/alpha.env
    state_dir: {root}/state-alpha
"""
    roster = root / "roster.yml"
    roster.write_text(f"root: {root}\n{top_yaml}\n{projects_yaml}")
    return roster


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(orch.parse_duration("10m"), 600)
        self.assertEqual(orch.parse_duration("90m"), 5400)
        self.assertEqual(orch.parse_duration("8h"), 28800)
        self.assertEqual(orch.parse_duration("45s"), 45)
        self.assertEqual(orch.parse_duration("1d"), 86400)

    def test_bare_int_is_seconds(self):
        self.assertEqual(orch.parse_duration(300), 300)
        self.assertEqual(orch.parse_duration("300"), 300)

    def test_junk_raises(self):
        with self.assertRaises(orch.RosterError):
            orch.parse_duration("soon")
        with self.assertRaises(orch.RosterError):
            orch.parse_duration("10 minutes")


class TestLoadRoster(unittest.TestCase):
    def test_defaults_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = orch.load_roster(make_roster_dir(tmp))
        self.assertEqual(roster["cfg"]["ladder_s"], [600, 1200, 2400, 3600])
        self.assertEqual(roster["cfg"]["waiting_interval_s"], 1200)
        self.assertEqual(roster["cfg"]["force_full_every_s"], 28800)
        self.assertEqual(roster["cfg"]["pass_timeout_s"], 5400)
        self.assertEqual(roster["cfg"]["mem_floor_mb"], 2560)
        self.assertEqual(roster["cfg"]["error_escalate_after"], 3)
        self.assertEqual(roster["cfg"]["crash_park_after"], 3)
        p = roster["projects"][0]
        self.assertEqual(p["name"], "alpha")
        self.assertEqual(p["cadence"], "adaptive")
        self.assertIsNone(p["model"])
        self.assertIsNone(p["window"])

    def test_per_project_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "alpha"; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            (root / "alpha.env").touch()
            roster_path = root / "roster.yml"
            roster_path.write_text(f"""
root: {root}
cadence: adaptive
projects:
  - name: alpha
    work_tree: {wt}
    env_file: {root}/alpha.env
    state_dir: {root}/state-alpha
    cadence: fixed
    interval: 45m
    model: sonnet
    tz: Asia/Kolkata
    window: "09:00-20:00"
""")
            roster = orch.load_roster(roster_path)
        p = roster["projects"][0]
        self.assertEqual(p["cadence"], "fixed")
        self.assertEqual(p["interval_s"], 2700)
        self.assertEqual(p["model"], "sonnet")
        self.assertEqual(p["tz"], "Asia/Kolkata")
        self.assertEqual(p["window"], "09:00-20:00")

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = "projects:\n  - name: alpha\n    work_tree: /x\n"
            with self.assertRaises(orch.RosterError):
                orch.load_roster(make_roster_dir(tmp, projects_yaml=bad))

    def test_empty_or_duplicate_projects_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(orch.RosterError):
                orch.load_roster(make_roster_dir(tmp, projects_yaml="projects: []\n"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "alpha"; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            dup = f"""
projects:
  - {{name: alpha, work_tree: {wt}, env_file: {root}/e, state_dir: {root}/s}}
  - {{name: alpha, work_tree: {wt}, env_file: {root}/e, state_dir: {root}/s2}}
"""
            with self.assertRaises(orch.RosterError):
                orch.load_roster(make_roster_dir(tmp, projects_yaml=dup))

    def test_bad_cadence_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "alpha"; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            bad = f"""
projects:
  - {{name: alpha, work_tree: {wt}, env_file: {root}/e, state_dir: {root}/s, cadence: sometimes}}
"""
            with self.assertRaises(orch.RosterError):
                orch.load_roster(make_roster_dir(tmp, projects_yaml=bad))


class TestWorkTreeGuard(unittest.TestCase):
    """Spec §8: positive allowlist — under root AND carrying .dw-agent-clone."""

    def test_valid_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "proj"; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            orch.check_work_tree({"name": "p", "work_tree": str(wt)}, tmp)  # no raise

    def test_missing_marker_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "prod-checkout"; wt.mkdir()   # a live checkout: NO marker
            with self.assertRaises(orch.RosterError):
                orch.check_work_tree({"name": "p", "work_tree": str(wt)}, tmp)

    def test_outside_root_refused_even_with_marker(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            wt = Path(other) / "proj"; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            with self.assertRaises(orch.RosterError):
                orch.check_work_tree({"name": "p", "work_tree": str(wt)}, tmp)

    def test_root_itself_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".dw-agent-clone").touch()
            with self.assertRaises(orch.RosterError):
                orch.check_work_tree({"name": "p", "work_tree": tmp}, tmp)


class TestStateIO(unittest.TestCase):
    def test_roundtrip_and_ensure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orch-state.json"
            st = orch.load_state(path)
            self.assertEqual(st, {})
            orch.ensure_projects(st, [{"name": "a"}, {"name": "b"}])
            self.assertIn("a", st["projects"])
            self.assertEqual(st["projects"]["a"]["dry_streak"], 0)
            self.assertEqual(st["rr_next"], 0)
            orch.save_state(path, st)
            st2 = orch.load_state(path)
            self.assertEqual(st, st2)

    def test_ensure_drops_removed_projects(self):
        st = {"projects": {"gone": orch.default_pstate()}, "rr_next": 5}
        orch.ensure_projects(st, [{"name": "kept"}])
        self.assertEqual(set(st["projects"]), {"kept"})

    def test_corrupt_state_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orch-state.json"
            path.write_text("{not json")
            st = orch.load_state(path)
            self.assertEqual(st, {})
            self.assertTrue(path.with_suffix(".json.corrupt").exists())


class TestMemAvailable(unittest.TestCase):
    def test_parses_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "meminfo"
            f.write_text("MemTotal:  8000000 kB\nMemAvailable:  3072000 kB\n")
            self.assertEqual(orch.mem_available_mb(str(f)), 3000)

    def test_missing_file_is_none(self):
        self.assertIsNone(orch.mem_available_mb("/nonexistent/meminfo"))


class TestWindows(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(orch.parse_window("09:00-20:00"), (540, 1200))
        with self.assertRaises(orch.RosterError):
            orch.parse_window("9am-8pm")
        with self.assertRaises(orch.RosterError):
            orch.parse_window("25:00-26:00")

    def test_in_window_plain_and_wrap(self):
        w = (540, 1200)                       # 09:00-20:00
        self.assertTrue(orch.minute_in_window(540, w))    # inclusive start
        self.assertTrue(orch.minute_in_window(1199, w))
        self.assertFalse(orch.minute_in_window(1200, w))  # exclusive end
        self.assertFalse(orch.minute_in_window(300, w))
        night = (1320, 360)                   # 22:00-06:00 overnight
        self.assertTrue(orch.minute_in_window(1380, night))
        self.assertTrue(orch.minute_in_window(120, night))
        self.assertFalse(orch.minute_in_window(720, night))
        self.assertTrue(orch.minute_in_window(0, (0, 0)))  # equal ends = always open

    def test_seconds_until_open_intersection(self):
        # NOW is 12:00 UTC. Intersection of 09:00-20:00 and 13:00-18:00 opens at 13:00.
        wins = [(540, 1200), (780, 1080)]
        self.assertEqual(orch.seconds_until_open(wins, "UTC", NOW), 3600)
        # Inside both → 0.
        self.assertEqual(orch.seconds_until_open([(540, 1200)], "UTC", NOW), 0)
        # Empty intersection → None.
        self.assertIsNone(orch.seconds_until_open([(0, 60), (120, 180)], "UTC", NOW))

    def test_repo_schedule_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "dev-workflow.yml").write_text(
                "schedule:\n  window: \"09:00-20:00\"\n  tz: UTC\n")
            sched = orch.read_repo_schedule(tmp)
            self.assertEqual(sched["window"], "09:00-20:00")
            self.assertEqual(sched["tz"], "UTC")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(orch.read_repo_schedule(tmp), {})


class TestPickNext(unittest.TestCase):
    def roster2(self, tmp, window_b=None):
        """Two adaptive projects a, b (b optionally windowed)."""
        root = Path(tmp)
        entries = []
        for name in ("a", "b"):
            wt = root / name; wt.mkdir(); (wt / ".dw-agent-clone").touch()
            e = {"name": name, "work_tree": str(wt), "env_file": str(root / f"{name}.env"),
                 "state_dir": str(root / f"state-{name}"), "model": None, "tz": "UTC",
                 "window": window_b if name == "b" else None,
                 "cadence": "adaptive", "interval_s": 1800}
            entries.append(e)
        cfg = {k: v for k, v in orch.DEFAULTS.items()}
        cfg["ladder_s"] = [600, 1200, 2400, 3600]
        for k in ("interval", "waiting_interval", "force_full_every",
                  "pass_timeout", "requeue_delay", "crash_park_for"):
            cfg[k + "_s"] = orch.parse_duration(cfg[k])
        for k in ("mem_floor_mb", "error_escalate_after", "crash_park_after"):
            cfg[k] = int(cfg[k])
        return {"root": str(root), "cfg": cfg, "projects": entries}

    def fresh_state(self, roster):
        st = {}
        orch.ensure_projects(st, roster["projects"])
        return st

    def test_round_robin_order_and_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["project"]["name"], "a")
            st["rr_next"] = 1          # record() advances this; simulate
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["project"]["name"], "b")

    def test_first_ever_run_is_forced_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW)
            self.assertTrue(d["force_full"])
            self.assertFalse(d["precheck"])   # forced-full pass skips the pre-check

    def test_recent_full_pass_enables_precheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["a"]["last_full_pass"] = orch.to_iso(
                NOW - datetime.timedelta(hours=1))
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["project"]["name"], "a")
            self.assertFalse(d["force_full"])
            self.assertTrue(d["precheck"])

    def test_stale_full_pass_forces_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["a"]["last_full_pass"] = orch.to_iso(
                NOW - datetime.timedelta(hours=9))   # > 8h default
            d = orch.pick_next(roster, st, NOW)
            self.assertTrue(d["force_full"])

    def test_fixed_cadence_never_prechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            roster["projects"][0]["cadence"] = "fixed"
            st = self.fresh_state(roster)
            st["projects"]["a"]["last_full_pass"] = orch.to_iso(NOW)
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["project"]["name"], "a")
            self.assertFalse(d["precheck"])

    def test_ineligible_projects_produce_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            for name in ("a", "b"):
                st["projects"][name]["next_eligible"] = orch.to_iso(
                    NOW + datetime.timedelta(minutes=7))
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["action"], "sleep")
            self.assertEqual(d["sleep_seconds"], 420)   # sleep to min(next_eligible)

    def test_window_skip_does_not_touch_ladder(self):
        # b is windowed out (window opens at 13:00, NOW=12:00); a is not yet eligible.
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp, window_b="13:00-18:00")
            st = self.fresh_state(roster)
            st["rr_next"] = 1                                  # b's turn
            st["projects"]["a"]["next_eligible"] = orch.to_iso(
                NOW + datetime.timedelta(hours=2))
            before = dict(st["projects"]["b"])
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["action"], "sleep")
            self.assertEqual(d["sleep_seconds"], 3600)         # until b's window opens
            self.assertEqual(st["projects"]["b"], before)      # no ladder advance

    def test_parked_project_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["a"]["parked_until"] = orch.to_iso(
                NOW + datetime.timedelta(hours=6))
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["project"]["name"], "b")

    def test_memory_gate_sleeps_short_no_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            before = json.dumps(st["projects"])
            d = orch.pick_next(roster, st, NOW, mem_mb=2000)   # < 2560 floor
            self.assertEqual(d["action"], "sleep")
            self.assertEqual(d["sleep_seconds"], 300)          # requeue_delay, not ladder
            self.assertIn("memory", d["reason"])
            self.assertEqual(json.dumps(st["projects"]), before)

    def test_run_now_bypasses_eligibility_and_precheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["b"]["next_eligible"] = orch.to_iso(
                NOW + datetime.timedelta(hours=1))
            d = orch.pick_next(roster, st, NOW, run_now="b")
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["project"]["name"], "b")
            self.assertTrue(d["force_full"])
            self.assertTrue(d["consume_run_now"])

    def test_memory_gate_with_stale_run_now_still_consumes(self):
        # An unknown-name run-now file + low memory must still be consumed,
        # or the driver busy-loops (sleep wakes instantly on the file).
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW, mem_mb=2000, run_now="nope")
            self.assertEqual(d["action"], "sleep")
            self.assertTrue(d["consume_run_now"])

    # ── ingress wakes: eligibility signals, not force-runs ──
    def test_inbox_wake_skips_ladder_but_not_precheck_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["b"]["next_eligible"] = orch.to_iso(
                NOW + datetime.timedelta(hours=2))               # ladder says wait
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b"])
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["project"]["name"], "b")
            self.assertFalse(d["precheck"])                      # the wake IS the signal
            self.assertFalse(d["force_full"])                    # not a human force
            self.assertEqual((d["wake_kind"], d["wake_name"]), ("inbox", "b"))
            self.assertEqual(d["drop_wakes"], [])
            self.assertFalse(d["consume_run_now"])               # no human file involved

    def test_inbox_wake_honors_memory_floor_and_keeps_wakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW, mem_mb=2000, inbox_wakes=["b", "a"])
            self.assertEqual(d["action"], "sleep")
            self.assertEqual(d["sleep_seconds"], 300)
            self.assertEqual(d["drop_wakes"], [])                # both kept

    def test_inbox_wake_honors_window_and_keeps_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp, window_b="13:00-18:00")  # NOW is 12:00
            st = self.fresh_state(roster)
            before = json.dumps(st["projects"])
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b"])
            self.assertEqual(d["action"], "sleep")
            self.assertEqual(d["sleep_seconds"], 3600)
            self.assertEqual(d["drop_wakes"], [])
            self.assertEqual(json.dumps(st["projects"]), before)
            # a human force ignores the window (unchanged behaviour)
            d = orch.pick_next(roster, st, NOW, run_now="b")
            self.assertEqual(d["action"], "run")

    def test_deferred_wake_does_not_starve_a_runnable_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp, window_b="13:00-18:00")  # b closed, a open
            st = self.fresh_state(roster)
            st["projects"]["a"]["next_eligible"] = orch.to_iso(NOW + datetime.timedelta(hours=1))
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b", "a"])   # b is OLDER
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["wake_name"], "a")
            self.assertEqual(d["drop_wakes"], [])                # b stays pending

    def test_lock_held_wake_kept_and_others_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            lock = Path(roster["projects"][1]["state_dir"]) / "loop.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(str(os.getpid()))          # live owner: us
            self.assertTrue(orch.lock_held(roster["projects"][1]["state_dir"]))
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b", "a"])
            self.assertEqual((d["action"], d["wake_name"]), ("run", "a"))
            (lock / "pid").write_text("999999999")               # dead owner → not held
            self.assertFalse(orch.lock_held(roster["projects"][1]["state_dir"]))
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b", "a"])
            self.assertEqual(d["wake_name"], "b")

    def test_inbox_wake_for_paused_or_parked_or_unknown_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            st["projects"]["b"]["parked_until"] = orch.to_iso(
                NOW + datetime.timedelta(hours=6))
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b", "zzz"])
            self.assertEqual(d["project"]["name"], "a")          # normal round-robin
            self.assertEqual(d["drop_wakes"], ["b", "zzz"])      # wake files deleted
            self.assertNotIn("wake_kind", d)
            roster["projects"][1]["enabled"] = False
            st["projects"]["b"].pop("parked_until")
            d = orch.pick_next(roster, st, NOW, inbox_wakes=["b"])
            self.assertEqual((d["project"]["name"], d["drop_wakes"]), ("a", ["b"]))

    def test_human_run_now_beats_inbox_wakes(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW, run_now="a", inbox_wakes=["b"])
            self.assertEqual((d["project"]["name"], d["force_full"], d["consume_run_now"]), ("a", True, True))
            self.assertEqual(d["drop_wakes"], [])                # b's wake untouched

    def test_run_now_unknown_name_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            st = self.fresh_state(roster)
            d = orch.pick_next(roster, st, NOW, run_now="nope")
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["project"]["name"], "a")   # normal round-robin
            self.assertTrue(d["consume_run_now"])          # still consume the file


class TestClassify(unittest.TestCase):
    OK = {"picked": 0, "pr_opened": 0, "asked": 0, "blocked": 0,
          "progressed": False, "error": None}

    def c(self, **kw):
        base = dict(rc=0, timed_out=False, outcome=dict(self.OK),
                    log_tail=[], questions_count=0)
        base.update(kw)
        return orch.classify_pass(**base)[0]

    def test_timeout_and_nonzero_are_error(self):
        self.assertEqual(self.c(timed_out=True), "error")
        self.assertEqual(self.c(rc=7), "error")

    def test_guillotine_warn_is_error(self):
        tail = ["[ts] WARN: pass terminated background task(s) at the -p ceiling — "
                "a build was likely killed mid-flight ... Background tasks still running"]
        self.assertEqual(self.c(log_tail=tail), "error")

    def test_outcome_error_field_is_error(self):
        o = dict(self.OK); o["error"] = "tracker MCP down"
        self.assertEqual(self.c(outcome=o), "error")

    def test_pr_opened_or_progressed_is_productive(self):
        o = dict(self.OK); o["pr_opened"] = 1
        self.assertEqual(self.c(outcome=o), "productive")
        o = dict(self.OK); o["progressed"] = True
        self.assertEqual(self.c(outcome=o), "productive")

    def test_asked_blocked_or_open_questions_is_waiting(self):
        o = dict(self.OK); o["asked"] = 1
        self.assertEqual(self.c(outcome=o), "waiting")
        o = dict(self.OK); o["blocked"] = 1
        self.assertEqual(self.c(outcome=o), "waiting")
        self.assertEqual(self.c(questions_count=2), "waiting")

    def test_nothing_is_dry(self):
        self.assertEqual(self.c(), "dry")

    def test_missing_outcome_lock_yield_is_skipped_lock(self):
        tail = ["[2026-07-11 12:00:00 UTC] skip: held by interactive pid 4242"]
        self.assertEqual(self.c(outcome=None, log_tail=tail), "skipped-lock")

    def test_missing_outcome_otherwise_error(self):
        # rc=0 but no outcome.json (not a lock-yield) = the skill did not finish
        # (config-read failure / early exit) — escalate as error, not fake-dry.
        self.assertEqual(self.c(outcome=None), "error")


class TestCurrentPassSegment(unittest.TestCase):
    """Finding 1: classify only from the pass that just finished, so a previous
    pass's guillotine WARN in the append-only log can't misclassify a clean pass."""

    START = "[2026-07-11 12:00:00 UTC] === ticket-loop — start (HEAD abc123) ==="
    DONE = "[2026-07-11 12:05:00 UTC] === ticket-loop — done (exit 0) ==="
    PREV_GUILLOTINE = ("[2026-07-11 11:30:00 UTC] WARN: pass terminated background "
                       "task(s) ... Background tasks still running")
    PREV_DONE = "[2026-07-11 11:35:00 UTC] === ticket-loop — done (exit 0) ==="

    def test_returns_lines_from_last_start_marker(self):
        prev_start = "[2026-07-11 11:00:00 UTC] === ticket-loop — start (HEAD old) ==="
        lines = [prev_start, self.PREV_DONE, self.START, self.DONE]
        self.assertEqual(orch.current_pass_segment(lines), [self.START, self.DONE])

    def test_no_start_marker_returns_all_unchanged(self):
        lines = [self.PREV_GUILLOTINE, self.PREV_DONE, "[ts] skip: held"]
        self.assertEqual(orch.current_pass_segment(lines), lines)

    def test_previous_guillotine_above_start_does_not_error(self):
        ok = {"picked": 0, "pr_opened": 0, "asked": 0, "blocked": 0,
              "progressed": False, "error": None}
        seg = orch.current_pass_segment(
            [self.PREV_GUILLOTINE, self.PREV_DONE, self.START, self.DONE])
        cls, _ = orch.classify_pass(0, False, ok, seg, 0)
        self.assertEqual(cls, "dry")

    def test_lock_yield_with_prev_guillotine_is_skipped_lock(self):
        tail = [self.PREV_GUILLOTINE, self.PREV_DONE,
                "[2026-07-11 12:00:00 UTC] skip: held by interactive pid 4242"]
        cls, _ = orch.classify_pass(0, False, None, tail, 0)
        self.assertEqual(cls, "skipped-lock")

    def test_cmd_classify_ignores_previous_pass_guillotine(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp) / "state-alpha"
            (sd / "logs").mkdir(parents=True)
            (sd / "logs" / "ticket-loop-cron.log").write_text("\n".join([
                self.PREV_GUILLOTINE, self.PREV_DONE, self.START, self.DONE]) + "\n")
            (sd / "outcome.json").write_text(json.dumps(
                {"picked": 0, "pr_opened": 1, "asked": 0, "blocked": 0,
                 "progressed": False, "error": None}))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = orch.main(["classify", "--state-dir", str(sd), "--rc", "0"])
            self.assertEqual(rc, 0)
            self.assertTrue(buf.getvalue().startswith("productive"), buf.getvalue())


class TestApplyOutcome(unittest.TestCase):
    def cfg(self):
        cfg = dict(orch.DEFAULTS)
        cfg["ladder_s"] = [600, 1200, 2400, 3600]
        for k in ("interval", "waiting_interval", "force_full_every",
                  "pass_timeout", "requeue_delay", "crash_park_for"):
            cfg[k + "_s"] = orch.parse_duration(cfg[k])
        for k in ("mem_floor_mb", "error_escalate_after", "crash_park_after"):
            cfg[k] = int(cfg[k])
        return cfg

    def test_productive_resets_everything_to_fast(self):
        ps = orch.default_pstate()
        ps.update(dry_streak=3, error_streak=2, crash_streak=1)
        delay, esc = orch.apply_outcome(ps, "productive", self.cfg(), NOW)
        self.assertEqual(delay, 600)
        self.assertEqual((ps["dry_streak"], ps["error_streak"], ps["crash_streak"]),
                         (0, 0, 0))
        self.assertEqual(ps["next_eligible"], orch.to_iso(
            NOW + datetime.timedelta(seconds=600)))
        self.assertEqual(ps["last_outcome"], "productive")
        self.assertEqual(ps["last_full_pass"], orch.to_iso(NOW))
        self.assertEqual(esc, [])

    def test_dry_ladder_advances_and_caps(self):
        ps = orch.default_pstate()
        cfg = self.cfg()
        delays = [orch.apply_outcome(ps, "dry", cfg, NOW)[0] for _ in range(5)]
        self.assertEqual(delays, [1200, 2400, 3600, 3600, 3600])

    def test_precheck_idle_advances_ladder_but_no_full_pass(self):
        ps = orch.default_pstate()
        delay, _ = orch.apply_outcome(ps, "precheck-idle", self.cfg(), NOW)
        self.assertEqual(delay, 1200)
        self.assertIsNone(ps["last_full_pass"])   # no pass actually ran

    def test_waiting_is_fixed_interval_not_ladder(self):
        ps = orch.default_pstate()
        ps["dry_streak"] = 3
        delay, _ = orch.apply_outcome(ps, "waiting", self.cfg(), NOW)
        self.assertEqual(delay, 1200)              # waiting_interval 20m
        self.assertEqual(ps["dry_streak"], 3)      # ladder untouched

    def test_error_streak_escalates_at_threshold(self):
        ps = orch.default_pstate()
        cfg = self.cfg()
        _, e1 = orch.apply_outcome(ps, "error", cfg, NOW)
        _, e2 = orch.apply_outcome(ps, "error", cfg, NOW)
        self.assertEqual(e1, []); self.assertEqual(e2, [])
        _, e3 = orch.apply_outcome(ps, "error", cfg, NOW)
        self.assertEqual(ps["error_streak"], 3)
        self.assertTrue(any(level == "project" for level, _ in e3))
        self.assertTrue(any(level == "ops" for level, _ in e3))
        # 4th error: streak keeps counting but no repeat spam at the threshold
        _, e4 = orch.apply_outcome(ps, "error", cfg, NOW)
        self.assertEqual(e4, [])

    def test_crash_parks_after_k(self):
        ps = orch.default_pstate()
        cfg = self.cfg()
        orch.apply_outcome(ps, "crash", cfg, NOW)
        orch.apply_outcome(ps, "crash", cfg, NOW)
        self.assertIsNone(ps["parked_until"])
        _, esc = orch.apply_outcome(ps, "crash", cfg, NOW)
        self.assertEqual(ps["crash_streak"], 3)
        self.assertEqual(ps["parked_until"], orch.to_iso(
            NOW + datetime.timedelta(hours=12)))
        self.assertTrue(any(level == "ops" for level, _ in esc))

    def test_skipped_lock_short_requeue_no_streaks(self):
        ps = orch.default_pstate()
        ps["dry_streak"] = 2
        delay, esc = orch.apply_outcome(ps, "skipped-lock", self.cfg(), NOW)
        self.assertEqual(delay, 300)               # requeue_delay 5m
        self.assertEqual(ps["dry_streak"], 2)
        self.assertEqual(esc, [])

    def test_fixed_cadence_uses_interval_for_real_passes(self):
        ps = orch.default_pstate()
        for cls in ("productive", "dry", "waiting"):
            delay, _ = orch.apply_outcome(ps, cls, self.cfg(), NOW,
                                          cadence="fixed", interval_s=2700)
            self.assertEqual(delay, 2700, cls)


class TestCmdRecord(unittest.TestCase):
    """record end-to-end against scratch roster + state files."""

    def run_record(self, tmp, outcome, project="alpha"):
        roster_path = make_roster_dir(tmp)
        state_path = Path(tmp) / "orch-state.json"
        rc = orch.main(["record", "--roster", str(roster_path),
                        "--state", str(state_path), "--project", project,
                        "--outcome", outcome, "--now", "2026-07-11T12:00:00Z"])
        return rc, json.loads(state_path.read_text())

    def test_record_persists_and_advances_rr(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, st = self.run_record(tmp, "productive")
        self.assertEqual(rc, 0)
        ps = st["projects"]["alpha"]
        self.assertEqual(ps["last_outcome"], "productive")
        self.assertEqual(st["rr_next"], 1 % 1)   # single project → wraps to 0
        self.assertNotIn("pass_started", st)

    def test_record_clears_write_ahead(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = make_roster_dir(tmp)
            state_path = Path(tmp) / "orch-state.json"
            st = {"pass_started": {"project": "alpha", "ts": "2026-07-11T11:00:00Z"}}
            orch.save_state(state_path, st)
            orch.main(["record", "--roster", str(roster_path),
                       "--state", str(state_path), "--project", "alpha",
                       "--outcome", "dry", "--now", "2026-07-11T12:00:00Z"])
            st = json.loads(state_path.read_text())
        self.assertNotIn("pass_started", st)

    def test_unknown_outcome_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.run_record(tmp, "meh")


class TestSharedAuthEscalation(unittest.TestCase):
    """Finding 2: the shared ~/.claude means one expired OAuth token errors every
    project at once — cmd_record escalates loudly, once, when ALL projects error."""

    def make_two_project_roster(self, tmp):
        root = Path(tmp)
        entries = []
        for name in ("alpha", "beta"):
            wt = root / name
            wt.mkdir(exist_ok=True)
            (wt / ".dw-agent-clone").touch()
            (root / f"{name}.env").touch()
            entries.append(f"""
  - name: {name}
    work_tree: {wt}
    env_file: {root}/{name}.env
    state_dir: {root}/state-{name}""")
        roster = root / "roster.yml"
        roster.write_text(f"root: {root}\nprojects:{''.join(entries)}\n")
        return roster

    def record(self, roster_path, state_path, project, outcome):
        """Run record capturing the JSON stdout; return (state, emitted-dict)."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            orch.main(["record", "--roster", str(roster_path),
                       "--state", str(state_path), "--project", project,
                       "--outcome", outcome, "--now", "2026-07-11T12:00:00Z"])
        return json.loads(state_path.read_text()), json.loads(buf.getvalue())

    def test_all_error_latches_once_and_resets(self):
        MSG = "EVERY roster project is erroring"
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"

            # 1. alpha errors — beta still clean, no shared-auth escalation.
            st, out = self.record(roster, state, "alpha", "error")
            self.assertNotIn(MSG, out["ESCALATE_OPS"])
            self.assertFalse(st.get("all_error_alerted"))

            # 2. beta errors — every project now erroring: escalate + latch.
            st, out = self.record(roster, state, "beta", "error")
            self.assertIn(MSG, out["ESCALATE_OPS"])
            self.assertTrue(st["all_error_alerted"])

            # 3. alpha errors again — latch holds, no repeat of the shared message.
            st, out = self.record(roster, state, "alpha", "error")
            self.assertNotIn(MSG, out["ESCALATE_OPS"])
            self.assertTrue(st["all_error_alerted"])

            # 4. beta goes productive — any non-error resets the latch.
            st, out = self.record(roster, state, "beta", "productive")
            self.assertFalse(st["all_error_alerted"])

    def _write_usage_limit(self, tmp, name, limit, reset="", kind=""):
        sd = Path(tmp) / f"state-{name}"
        sd.mkdir(parents=True, exist_ok=True)
        rec = {"ts": "2026-07-11T12:00:00", "tenant": name, "rc": 1,
               "limit": limit}
        if kind:
            rec["kind"] = kind
        if reset:
            rec["reset"] = reset
        (sd / "usage.jsonl").write_text(json.dumps(rec) + "\n")

    def test_all_error_says_session_limit_when_usage_shows_limit(self):
        """A shared session-limit exhaustion must NOT read as an auth failure —
        it errors every project at once but is benign + self-resolving."""
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"
            self._write_usage_limit(tmp, "alpha", True, "resets 2pm")
            self._write_usage_limit(tmp, "beta", True)
            self.record(roster, state, "alpha", "error")
            _st, out = self.record(roster, state, "beta", "error")
            self.assertIn("SESSION LIMIT", out["ESCALATE_OPS"])
            self.assertIn("NOT an auth", out["ESCALATE_OPS"])
            self.assertIn("resets 2pm", out["ESCALATE_OPS"])
            self.assertNotIn("expired CLAUDE_CODE_OAUTH_TOKEN",
                             out["ESCALATE_OPS"])

    def test_all_error_says_spend_limit_when_kind_is_spend(self):
        """The org monthly SPEND cap is a distinct incident: it does not reset
        in hours — the alert must say who fixes it (an admin) and where."""
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"
            self._write_usage_limit(tmp, "alpha", True, kind="spend")
            self._write_usage_limit(tmp, "beta", True, kind="spend")
            self.record(roster, state, "alpha", "error")
            _st, out = self.record(roster, state, "beta", "error")
            self.assertIn("SPEND LIMIT", out["ESCALATE_OPS"])
            # No raw URL in the alert (Telegram auto-links them) — point at the
            # settings page in words instead.
            self.assertIn("Claude settings", out["ESCALATE_OPS"])
            self.assertNotIn("claude.ai/", out["ESCALATE_OPS"])
            self.assertNotIn("SESSION LIMIT", out["ESCALATE_OPS"])
            self.assertNotIn("expired CLAUDE_CODE_OAUTH_TOKEN",
                             out["ESCALATE_OPS"])

    def test_streak_alert_names_the_limit_cause(self):
        """The 3-consecutive-failures alert must name a KNOWN cause (the usage
        limit) instead of the generic 'check the loop log'."""
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"
            self._write_usage_limit(tmp, "alpha", True, kind="spend")
            self.record(roster, state, "alpha", "error")
            self.record(roster, state, "alpha", "error")
            _st, out = self.record(roster, state, "alpha", "error")
            self.assertIn("3 consecutive failed passes", out["ESCALATE_PROJECT"])
            self.assertIn("SPEND limit", out["ESCALATE_PROJECT"])
            self.assertIn("Claude settings", out["ESCALATE_PROJECT"])
            self.assertNotIn("claude.ai/", out["ESCALATE_PROJECT"])
            self.assertNotIn("check the loop log", out["ESCALATE_PROJECT"])
            # The ops copy names the cause too, in place of its log-tail.
            self.assertIn("SPEND limit", out["ESCALATE_OPS"])
            self.assertNotIn("check the loop log", out["ESCALATE_OPS"])

    def test_streak_alert_stays_generic_without_a_limit(self):
        """No known cause: ops gets the technical line; the project group gets
        the plain-language copy (no 'check the loop log' — nobody there can),
        with what it means for them."""
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"
            self.record(roster, state, "alpha", "error")
            self.record(roster, state, "alpha", "error")
            _st, out = self.record(roster, state, "alpha", "error")
            self.assertIn("3 consecutive failed passes", out["ESCALATE_OPS"])
            self.assertIn("check the loop log", out["ESCALATE_OPS"])
            self.assertIn("3 consecutive failed passes", out["ESCALATE_PROJECT"])
            self.assertNotIn("check the loop log", out["ESCALATE_PROJECT"])
            self.assertIn("retrying on its schedule", out["ESCALATE_PROJECT"])
            self.assertIn("stay queued", out["ESCALATE_PROJECT"])

    def test_all_error_says_auth_when_no_usage_limit(self):
        """No limit=true in any usage.jsonl -> a genuine auth failure wording."""
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.make_two_project_roster(tmp)
            state = Path(tmp) / "orch-state.json"
            self.record(roster, state, "alpha", "error")
            _st, out = self.record(roster, state, "beta", "error")
            self.assertIn("expired CLAUDE_CODE_OAUTH_TOKEN", out["ESCALATE_OPS"])
            self.assertNotIn("SESSION LIMIT", out["ESCALATE_OPS"])


class TestStartup(unittest.TestCase):
    def test_crash_recovery_and_lock_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = make_roster_dir(tmp)
            state_path = Path(tmp) / "orch-state.json"
            lock = Path(tmp) / "state-alpha" / "loop.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text("42")
            orch.save_state(state_path, {
                "pass_started": {"project": "alpha", "ts": "2026-07-11T11:00:00Z"}})
            rc = orch.main(["startup", "--roster", str(roster_path),
                            "--state", str(state_path),
                            "--now", "2026-07-11T12:00:00Z"])
            self.assertEqual(rc, 0)
            self.assertFalse(lock.exists())          # lock-clear on boot
            st = json.loads(state_path.read_text())
            self.assertNotIn("pass_started", st)
            self.assertEqual(st["projects"]["alpha"]["crash_streak"], 1)
            self.assertEqual(st["projects"]["alpha"]["last_outcome"], "crash")

    def test_guard_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = make_roster_dir(tmp)
            os.remove(Path(tmp) / "alpha" / ".dw-agent-clone")   # break the guard
            with self.assertRaises(SystemExit):
                orch.main(["startup", "--roster", str(roster_path),
                           "--state", str(Path(tmp) / "orch-state.json")])

    def test_clean_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = make_roster_dir(tmp)
            state_path = Path(tmp) / "orch-state.json"
            rc = orch.main(["startup", "--roster", str(roster_path),
                            "--state", str(state_path)])
            self.assertEqual(rc, 0)
            st = json.loads(state_path.read_text())
            self.assertIn("alpha", st["projects"])


class TestPassStart(unittest.TestCase):
    def test_write_ahead_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_path = make_roster_dir(tmp)
            state_path = Path(tmp) / "orch-state.json"
            rc = orch.main(["pass-start", "--roster", str(roster_path),
                            "--state", str(state_path), "--project", "alpha",
                            "--now", "2026-07-11T12:00:00Z"])
            self.assertEqual(rc, 0)
            st = json.loads(state_path.read_text())
            self.assertEqual(st["pass_started"],
                             {"project": "alpha", "ts": "2026-07-11T12:00:00Z"})


class TestEnabledAndRepo(unittest.TestCase):
    """`enabled` (pause switch) + `repo` (canonical repo record) roster fields."""

    def _load(self, tmp, projects_yaml):
        root = Path(tmp)
        for n in ("alpha", "beta"):
            wt = root / n
            wt.mkdir(exist_ok=True)
            (wt / ".dw-agent-clone").touch()
            (root / f"{n}.env").touch()
        roster = root / "roster.yml"
        roster.write_text(f"root: {root}\n{projects_yaml}")
        return orch.load_roster(roster)

    def test_enabled_defaults_true_and_repo_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._load(tmp, f"""
projects:
  - name: alpha
    work_tree: {tmp}/alpha
    env_file: {tmp}/alpha.env
    state_dir: {tmp}/s-alpha
""")
        p = r["projects"][0]
        self.assertTrue(p["enabled"])
        self.assertIsNone(p["repo"])

    def test_enabled_false_and_repo_mapping_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._load(tmp, f"""
projects:
  - name: alpha
    work_tree: {tmp}/alpha
    env_file: {tmp}/alpha.env
    state_dir: {tmp}/s-alpha
    repo: {{url: github.com/o/alpha, branch: dev}}
  - name: beta
    work_tree: {tmp}/beta
    env_file: {tmp}/beta.env
    state_dir: {tmp}/s-beta
    enabled: false
""")
        by = {p["name"]: p for p in r["projects"]}
        self.assertTrue(by["alpha"]["enabled"])
        self.assertEqual(by["alpha"]["repo"], {"url": "github.com/o/alpha", "branch": "dev"})
        self.assertFalse(by["beta"]["enabled"])

    def test_enabled_quoted_string_coerced(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._load(tmp, f"""
projects:
  - name: alpha
    work_tree: {tmp}/alpha
    env_file: {tmp}/alpha.env
    state_dir: {tmp}/s-alpha
    enabled: "no"
""")
        self.assertFalse(r["projects"][0]["enabled"])

    def test_skill_and_manager_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._load(tmp, f"""
projects:
  - name: alpha
    work_tree: {tmp}/alpha
    env_file: {tmp}/alpha.env
    state_dir: {tmp}/s-alpha
    skill: ticket-loop-parent
    manager: true
  - name: beta
    work_tree: {tmp}/beta
    env_file: {tmp}/beta.env
    state_dir: {tmp}/s-beta
""")
        by = {p["name"]: p for p in r["projects"]}
        self.assertEqual(by["alpha"]["skill"], "ticket-loop-parent")
        self.assertTrue(by["alpha"]["manager"])
        self.assertIsNone(by["beta"]["skill"])       # default: no skill override
        self.assertFalse(by["beta"]["manager"])      # default: not a manager entry

    def test_bad_skill_rejected(self):
        # a non-simple skill value would become a malformed "/…" invoke string
        for bad in ("rm -rf /", "dev-workflow:ticket-loop-parent"):  # spaces, ':' double-prefix
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(orch.RosterError):
                    self._load(tmp, f"""
projects:
  - name: alpha
    work_tree: {tmp}/alpha
    env_file: {tmp}/alpha.env
    state_dir: {tmp}/s-alpha
    skill: "{bad}"
""")

    def test_repo_url_branch_forms(self):
        self.assertEqual(orch._repo_url_branch("github.com/o/r"), ("github.com/o/r", "main"))
        self.assertEqual(orch._repo_url_branch({"url": "u", "branch": "b"}), ("u", "b"))
        self.assertEqual(orch._repo_url_branch({"url": "u"}), ("u", "main"))
        self.assertEqual(orch._repo_url_branch(None), (None, None))


class TestPauseScheduling(unittest.TestCase):
    """A paused project is preserved in state but never scheduled."""

    def roster2(self, tmp):
        root = Path(tmp)
        entries = []
        for name in ("a", "b"):
            wt = root / name
            wt.mkdir()
            (wt / ".dw-agent-clone").touch()
            entries.append({"name": name, "work_tree": str(wt),
                            "env_file": str(root / f"{name}.env"),
                            "state_dir": str(root / f"state-{name}"), "model": None,
                            "tz": "UTC", "window": None, "cadence": "adaptive",
                            "interval_s": 1800, "enabled": True, "repo": None})
        cfg = {k: v for k, v in orch.DEFAULTS.items()}
        cfg["ladder_s"] = [600, 1200, 2400, 3600]
        for k in ("interval", "waiting_interval", "force_full_every",
                  "pass_timeout", "requeue_delay", "crash_park_for"):
            cfg[k + "_s"] = orch.parse_duration(cfg[k])
        for k in ("mem_floor_mb", "error_escalate_after", "crash_park_after"):
            cfg[k] = int(cfg[k])
        return {"root": str(root), "cfg": cfg, "projects": entries}

    def test_paused_skipped_picks_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            roster["projects"][0]["enabled"] = False   # pause 'a' (first in RR order)
            st = {}
            orch.ensure_projects(st, roster["projects"])
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["action"], "run")
            self.assertEqual(d["project"]["name"], "b")

    def test_all_paused_sleeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            for p in roster["projects"]:
                p["enabled"] = False
            st = {}
            orch.ensure_projects(st, roster["projects"])
            d = orch.pick_next(roster, st, NOW)
            self.assertEqual(d["action"], "sleep")

    def test_run_now_on_paused_does_not_run_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster = self.roster2(tmp)
            roster["projects"][1]["enabled"] = False   # pause 'b', target it
            st = {}
            orch.ensure_projects(st, roster["projects"])
            d = orch.pick_next(roster, st, NOW, run_now="b")
            self.assertTrue(d["consume_run_now"])       # file still consumed
            if d["action"] == "run":
                self.assertNotEqual(d["project"]["name"], "b")


class TestSeedPlan(unittest.TestCase):
    def test_emits_tsv_only_for_repo_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for n in ("alpha", "beta"):
                wt = root / n
                wt.mkdir()
                (wt / ".dw-agent-clone").touch()
                (root / f"{n}.env").touch()
            roster = root / "roster.yml"
            roster.write_text(f"""root: {root}
projects:
  - name: alpha
    work_tree: {root}/alpha
    env_file: {root}/alpha.env
    state_dir: {root}/s-alpha
    repo: {{url: https://github.com/o/alpha.git, branch: main}}
  - name: beta
    work_tree: {root}/beta
    env_file: {root}/beta.env
    state_dir: {root}/s-beta
""")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                orch.main(["seed-plan", "--roster", str(roster)])
        lines = [ln for ln in buf.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 1)                       # beta has no repo → skipped
        cols = lines[0].split("\t")
        self.assertEqual(cols[0], "alpha")
        self.assertEqual(cols[1], "https://github.com/o/alpha.git")
        self.assertEqual(cols[2], "main")
        self.assertEqual(cols[3], f"{root}/alpha")
        self.assertEqual(cols[4], f"{root}/alpha.env")


if __name__ == "__main__":
    unittest.main()
