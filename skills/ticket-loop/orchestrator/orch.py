#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Scheduling brain for the ticket-loop orchestrator (mode axis: multi-project).

orchestrator.sh (the PID-1 driver) shells into this for every decision; all
roster/backoff/window/classification state lives here so it is unit-testable
offline. Stdlib + PyYAML (python3-yaml in the container; `uv run` supplies it
on a laptop via the PEP 723 header above).

Subcommands (see cmd_* below; --sh prints shell-eval-able KEY=VALUE lines for
the driver, the default prints JSON for humans/tests):
  startup     validate roster (marker-file allowlist guard, §8), recover the
              crash write-ahead record, clear every project's stale loop.lock
  next        pick the next action: run one project, or sleep
  pass-start  persist the crash write-ahead record before a pass launches
  classify    classify a finished pass: productive|dry|waiting|error|skipped-lock
  record      apply a pass outcome to the project's backoff state
  status      human-readable status table

State file (orch-state.json, atomic writes):
  {"projects": {name: {dry_streak, error_streak, crash_streak, next_eligible,
                       last_pass, last_full_pass, last_outcome, parked_until}},
   "rr_next": int, "pass_started": {"project": name, "ts": iso} | absent,
   "all_error_alerted": bool}
"""

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # surfaced as a RosterError at load time, not an import crash
    yaml = None


class RosterError(Exception):
    """Invalid roster / config — the orchestrator must refuse to run."""


# ── time helpers ──────────────────────────────────────────────────────────────

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def to_iso(dt):
    return dt.replace(microsecond=0).astimezone(datetime.timezone.utc) \
             .isoformat().replace("+00:00", "Z")


def from_iso(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(v):
    """'10m' / '8h' / '45s' / '1d' / bare int (seconds) → seconds."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    m = _DUR_RE.match(str(v))
    if not m:
        raise RosterError(f"bad duration: {v!r} (want e.g. 45s, 10m, 8h, 1d)")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


# ── roster ────────────────────────────────────────────────────────────────────

DEFAULTS = {
    "cadence": "adaptive",
    "interval": "30m",              # fixed-cadence gap
    "ladder": ["10m", "20m", "40m", "60m"],
    "waiting_interval": "20m",      # waiting-on-human: neither ladder nor fast
    "force_full_every": "8h",       # unconditional safety pass (supervision §6)
    "pass_timeout": "90m",          # per-pass process-group timeout (blocker §1)
    "requeue_delay": "5m",          # skipped-lock / low-memory requeue
    "mem_floor_mb": 2560,           # host MemAvailable floor (§Capacity)
    "error_escalate_after": 3,      # consecutive errors → escalate (blocker §2)
    "crash_park_after": 3,          # consecutive crashes → park (blocker §3)
    "crash_park_for": "12h",
}

def _as_bool(v, default=False):
    """Coerce a YAML scalar to bool. Missing → default; a quoted string like
    "no"/"off"/"false"/"0"/"" → False, anything else truthy."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "off", "0", "")
    return bool(v)


_REQUIRED_PROJECT_KEYS = ("name", "work_tree", "env_file", "state_dir")
_DURATION_KEYS = ("interval", "waiting_interval", "force_full_every",
                  "pass_timeout", "requeue_delay", "crash_park_for")


def load_roster(path):
    if yaml is None:
        raise RosterError("PyYAML is required to read the roster "
                          "(apt: python3-yaml, or run via `uv run`)")
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RosterError(f"cannot read roster {path}: {exc}")
    projects = raw.get("projects")
    if not isinstance(projects, list) or not projects:
        raise RosterError(f"{path}: `projects` must be a non-empty list")
    cfg = {k: raw.get(k, d) for k, d in DEFAULTS.items()}
    if not isinstance(cfg["ladder"], list) or not cfg["ladder"]:
        raise RosterError("`ladder` must be a non-empty list of durations")
    cfg["ladder_s"] = [parse_duration(x) for x in cfg["ladder"]]
    for k in _DURATION_KEYS:
        cfg[k + "_s"] = parse_duration(cfg[k])
    for k in ("mem_floor_mb", "error_escalate_after", "crash_park_after"):
        cfg[k] = int(cfg[k])
    if cfg["cadence"] not in ("adaptive", "fixed"):
        raise RosterError(f"cadence must be adaptive|fixed, not {cfg['cadence']!r}")
    root = str(raw.get("root") or Path(path).resolve().parent)
    out, seen = [], set()
    for entry in projects:
        if not isinstance(entry, dict):
            raise RosterError(f"bad project entry (not a mapping): {entry!r}")
        for req in _REQUIRED_PROJECT_KEYS:
            if not entry.get(req):
                raise RosterError(f"project entry missing `{req}`: {entry!r}")
        name = str(entry["name"])
        if name in seen:
            raise RosterError(f"duplicate project name: {name}")
        seen.add(name)
        cadence = entry.get("cadence", cfg["cadence"])
        if cadence not in ("adaptive", "fixed"):
            raise RosterError(f"{name}: cadence must be adaptive|fixed")
        # `skill` must be a simple skill name — it becomes a `/skill` (or
        # `/dev-workflow:skill`) invocation string in the runner, so reject
        # anything a non-string YAML value or spaces would turn into a malformed
        # command (guards against `skill: {x: 1}` → "/{'x': 1}").
        sk = entry.get("skill")
        skill = None
        if sk not in (None, ""):
            sk = str(sk).strip()
            # BARE name only (no ':') — the runner namespaces it to /skill or
            # /dev-workflow:skill; a value with ':' would double-prefix.
            if not sk or not all(c.isalnum() or c in "_-" for c in sk):
                raise RosterError(f"{name}: skill must be a bare skill name "
                                  f"(letters/digits and _ -, no ':'), got {entry.get('skill')!r}")
            skill = sk
        # `enabled: false` pauses a project WITHOUT removing it: state is kept
        # (not pruned), the startup marker guard is skipped (so an entry can be
        # staged before its clone exists), and the scheduler never picks it.
        out.append({
            "name": name,
            "work_tree": str(entry["work_tree"]),
            "env_file": str(entry["env_file"]),
            "state_dir": str(entry["state_dir"]),
            "model": entry.get("model"),
            "tz": entry.get("tz"),
            "window": entry.get("window"),
            "cadence": cadence,
            "interval_s": parse_duration(entry.get("interval", cfg["interval"])),
            "enabled": _as_bool(entry.get("enabled", True), True),
            # A manual Telegram command listener is independent of scheduled
            # queue pickup. This lets `enabled: false` keep autonomous polling
            # paused while `start <issue>` remains available on demand.
            "command_listener": _as_bool(entry.get("command_listener", False)),
            # `skill` (optional): a bare skill NAME the runner namespaces (validated
            # above). Overrides the repo's agent.skill. None → default (ticket-loop).
            "skill": skill,
            # `manager` (optional): manager/parent mode — the runner must NOT
            # git-reset the work tree (a parent checkout holds child clones + docs,
            # it is not a disposable single-repo tree). Overrides repo agent.manager.
            "manager": _as_bool(entry.get("manager", False)),
            # `repo` (optional): the canonical record of WHICH repo this is — a
            # mapping {url, branch} (or a bare url string). The scheduler does not
            # use it; `seed-plan` reads it to clone the work tree from the roster
            # alone, so a roster entry + env file is enough to onboard a project.
            "repo": entry.get("repo"),
        })
    return {"root": root, "cfg": cfg, "projects": out}


def check_work_tree(project, root):
    """Spec §8 guard — a positive allowlist, not a path denylist: the work tree
    must resolve strictly under the orchestrator's own volume root AND carry the
    `.dw-agent-clone` marker written at seed time. Whitelists orchestrator-owned
    clones so a bind-mounted prod checkout can never be roster-driven."""
    wt = Path(project["work_tree"]).resolve()
    rootp = Path(root).resolve()
    try:
        rel = wt.relative_to(rootp)
    except ValueError:
        raise RosterError(f"{project['name']}: work tree {wt} is outside the "
                          f"roster root {rootp} — refusing")
    if str(rel) in ("", "."):
        raise RosterError(f"{project['name']}: work tree must be a directory "
                          f"UNDER the roster root, not the root itself")
    if not (wt / ".dw-agent-clone").exists():
        raise RosterError(f"{project['name']}: work tree {wt} has no "
                          ".dw-agent-clone marker — only dedicated agent clones "
                          "may be roster-driven (touch the marker at seed time)")


# ── orch-state ────────────────────────────────────────────────────────────────

def default_pstate():
    return {"dry_streak": 0, "error_streak": 0, "crash_streak": 0,
            "next_eligible": None, "last_pass": None, "last_full_pass": None,
            "last_outcome": None, "parked_until": None}


def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        # Corrupt mid-write state (should be impossible with atomic saves) —
        # quarantine and restart from scratch rather than dying forever.
        p.rename(p.with_suffix(".json.corrupt"))
        return {}


def save_state(path, st):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    tmp.replace(p)  # atomic — same pattern as telegram.py save_state()


def ensure_projects(st, projects):
    known = {p["name"] for p in projects}
    st.setdefault("projects", {})
    for p in projects:
        st["projects"].setdefault(p["name"], default_pstate())
    for name in list(st["projects"]):
        if name not in known:
            del st["projects"][name]
    st.setdefault("rr_next", 0)


# ── host memory gate ──────────────────────────────────────────────────────────

def mem_available_mb(path="/proc/meminfo"):
    """MemAvailable in MB, or None when unreadable (non-Linux → gate skipped)."""
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


# ── schedule windows (supervision §9: roster ∩ repo, tighten-only) ────────────

_WIN_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(s):
    m = _WIN_RE.match(str(s))
    if not m:
        raise RosterError(f"bad window: {s!r} (want HH:MM-HH:MM)")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise RosterError(f"bad window: {s!r} (hours 0-23, minutes 0-59)")
    return (h1 * 60 + m1, h2 * 60 + m2)


def minute_in_window(t, w):
    a, b = w
    if a == b:            # degenerate — treat as always open
        return True
    if a < b:
        return a <= t < b
    return t >= a or t < b  # overnight wrap, e.g. 22:00-06:00


def read_repo_schedule(work_tree):
    """The repo's own schedule gate: dev-workflow.yml schedule.{window,tz}.
    Missing file / unparseable → {} (no repo gate)."""
    f = Path(work_tree) / "dev-workflow.yml"
    if not f.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(f.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    sched = data.get("schedule") or {}
    out = {}
    if sched.get("window"):
        out["window"] = sched["window"]
    if sched.get("tz"):
        out["tz"] = sched["tz"]
    return out


def windows_for(project):
    """All gating windows for a project (roster entry + repo config — a time must
    be inside BOTH: the intersection, consistent with boundary rule 1), plus the
    tz they're evaluated in (roster tz beats repo schedule.tz)."""
    wins = []
    if project.get("window"):
        wins.append(parse_window(project["window"]))
    repo = read_repo_schedule(project["work_tree"])
    if repo.get("window"):
        wins.append(parse_window(repo["window"]))
    return wins, (project.get("tz") or repo.get("tz"))


def _local_minute(now, tz_name):
    if tz_name:
        from zoneinfo import ZoneInfo
        loc = now.astimezone(ZoneInfo(tz_name))
    else:
        loc = now.astimezone()
    return loc.hour * 60 + loc.minute


def seconds_until_open(wins, tz_name, now):
    """0 when `now` is inside every window; else seconds until the first minute
    inside all of them (scanned over 48h); None when the intersection is empty."""
    if not wins:
        return 0
    t0 = _local_minute(now, tz_name)
    for k in range(0, 2880):
        t = (t0 + k) % 1440
        if all(minute_in_window(t, w) for w in wins):
            return k * 60
    return None


# ── the `next` decision ───────────────────────────────────────────────────────

def _parked(ps, now):
    pu = ps.get("parked_until")
    return bool(pu) and now < from_iso(pu)


def lock_held(state_dir):
    """Is a ticket-loop singleton lock live in this state dir? Mirrors
    loop-lock.sh: the lock is a dir; a readable owner pid that is alive holds it,
    an unreadable owner is assumed live (conservative)."""
    lock = Path(state_dir) / "loop.lock"
    if not lock.is_dir():
        return False
    try:
        pid = int((lock / "pid").read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pick_next(roster, st, now, mem_mb=None, run_now=None, inbox_wakes=()):
    """Choose the next action. Read-only on `st` (record() owns all mutation) —
    EXCEPT nothing: window skips deliberately do not touch backoff state.

    run_now: a human touched the run-now file — run that project now, pre-check
    and eligibility bypassed (only enabled/parked are honored).
    inbox_wakes: tenants the Telegram ingress spooled a message for, oldest
    first. An ELIGIBILITY signal, not a force: it bypasses only the ladder
    (next_eligible); the memory floor, the work window, parking and a live
    singleton lock still apply — ordinary chatter must never launch a pass the
    policy would refuse. The first runnable wake runs; deferred ones (window,
    memory, lock) stay pending so they fire when the gate opens and can't
    starve the others; unknown/paused/parked ones are dropped (`drop_wakes`)."""
    cfg = roster["cfg"]
    projs = roster["projects"]
    drop = []

    def out(d):
        d["drop_wakes"] = drop
        return d

    if run_now is None and inbox_wakes:
        if mem_mb is not None and mem_mb < cfg["mem_floor_mb"]:
            return out({"action": "sleep", "sleep_seconds": cfg["requeue_delay_s"],
                        "reason": f"low memory: {mem_mb} MB available "
                                  f"< {cfg['mem_floor_mb']} MB floor (inbox wakes kept)",
                        "consume_run_now": False})
        waits = []
        for name in inbox_wakes:
            target = next((p for p in projs if p["name"] == name), None)
            if (target is None or not target.get("enabled", True)
                    or _parked(st["projects"][target["name"]], now)):
                drop.append(name)     # the spool keeps the message; the pre-check
                continue              # peek sees it when the project is back
            if lock_held(target["state_dir"]):
                continue              # interactive session: keep the wake, try the next
            wins, tz = windows_for(target)
            wait = seconds_until_open(wins, tz, now)
            if wait == 0:
                return out({"action": "run", "project": target, "force_full": False,
                            "precheck": False, "consume_run_now": False,
                            "wake_kind": "inbox", "wake_name": name})
            waits.append(86400 if wait is None else wait)
        if waits:
            return out({"action": "sleep", "sleep_seconds": max(30, min(int(min(waits)), 3600)),
                        "reason": "inbox wake(s) outside their window (kept)",
                        "consume_run_now": False})
        # every wake was dropped (or lock-held): ordinary scheduling below

    if run_now is not None:
        target = next((p for p in projs if p["name"] == run_now), None)
        if target is None and run_now == "":
            target = next((p for p in projs if p.get("enabled", True)), None)
        if (target is not None and target.get("enabled", True)
                and not _parked(st["projects"][target["name"]], now)):
            return out({"action": "run", "project": target, "force_full": True,
                        "precheck": False, "consume_run_now": True})
        # unknown/paused/parked name: consume the file (driver deletes it) and fall through

    if mem_mb is not None and mem_mb < cfg["mem_floor_mb"]:
        # consume_run_now here too: an unknown-name run-now file that fell
        # through must still be deleted, or the driver busy-loops on it.
        return out({"action": "sleep", "sleep_seconds": cfg["requeue_delay_s"],
                    "reason": f"low memory: {mem_mb} MB available "
                              f"< {cfg['mem_floor_mb']} MB floor",
                    "consume_run_now": run_now is not None})

    waits = []
    start = st.get("rr_next", 0) % len(projs)
    for i in range(len(projs)):
        p = projs[(start + i) % len(projs)]
        if not p.get("enabled", True):
            continue          # paused: never scheduled, contributes no wake time
        ps = st["projects"][p["name"]]
        if _parked(ps, now):
            waits.append((from_iso(ps["parked_until"]) - now).total_seconds())
            continue
        ne = ps.get("next_eligible")
        if ne and now < from_iso(ne):
            waits.append((from_iso(ne) - now).total_seconds())
            continue
        wins, tz = windows_for(p)
        wait = seconds_until_open(wins, tz, now)
        if wait is None:          # empty intersection — startup already warned
            waits.append(86400)
            continue
        if wait > 0:              # outside the window: skip, NO ladder advance
            waits.append(wait)
            continue
        lfp = ps.get("last_full_pass")
        force_full = (lfp is None or
                      (now - from_iso(lfp)).total_seconds() >= cfg["force_full_every_s"])
        precheck = p["cadence"] == "adaptive" and not force_full
        return out({"action": "run", "project": p, "force_full": force_full,
                    "precheck": precheck,
                    "consume_run_now": run_now is not None})
    sleep_s = int(min(waits)) if waits else 60
    return out({"action": "sleep", "sleep_seconds": max(30, min(sleep_s, 3600)),
                "reason": "no project eligible",
                "consume_run_now": run_now is not None})


def cmd_next(args):
    roster = load_roster(args.roster)
    st = load_state(args.state)
    ensure_projects(st, roster["projects"])
    now = from_iso(args.now) if args.now else now_utc()
    wakes = [w for w in (args.inbox_wakes or "").split(",") if w]
    d = pick_next(roster, st, now, mem_mb=mem_available_mb(),
                  run_now=args.run_now, inbox_wakes=wakes)
    save_state(args.state, st)   # persists newly-ensured project entries
    if d["action"] == "sleep":
        emit(args, {"ACTION": "sleep", "SLEEP_S": d["sleep_seconds"],
                    "REASON": d["reason"],
                    "DROP_WAKES": " ".join(d.get("drop_wakes") or []),
                    "CONSUME_RUN_NOW": 1 if d.get("consume_run_now") else 0})
        return 0
    p = d["project"]
    emit(args, {"ACTION": "run", "PROJECT": p["name"],
                "WORK_TREE": p["work_tree"], "ENV_FILE": p["env_file"],
                "STATE_DIR": p["state_dir"], "MODEL": p["model"] or "",
                "PROJECT_TZ": p["tz"] or "", "CADENCE": p["cadence"],
                "SKILL": p.get("skill") or "", "MANAGER": 1 if p.get("manager") else 0,
                "PRECHECK": 1 if d["precheck"] else 0,
                "FORCE_FULL": 1 if d["force_full"] else 0,
                "TIMEOUT_S": roster["cfg"]["pass_timeout_s"],
                "WAKE_KIND": d.get("wake_kind") or "",
                "WAKE_NAME": d.get("wake_name") or "",
                "DROP_WAKES": " ".join(d.get("drop_wakes") or []),
                "CONSUME_RUN_NOW": 1 if d.get("consume_run_now") else 0})
    return 0


# ── outcome classification (spec §4 — four classes, never silently dry) ──────

OUTCOME_CLASSES = ("productive", "dry", "precheck-idle", "waiting",
                   "error", "crash", "skipped-lock")


def current_pass_segment(lines):
    """The tail of the log belonging to the pass that just finished: everything
    from the last `— start (` marker on (cron-run.sh writes one per real pass).
    A lock-yield pass writes no start marker — callers still detect it via the
    `skip:` line at the very end of the raw tail."""
    for i in range(len(lines) - 1, -1, -1):
        if " — start (" in lines[i]:
            return lines[i:]
    return lines


def classify_pass(rc, timed_out, outcome, log_tail, questions_count):
    """Classify one finished pass. `outcome` is the skill-emitted outcome.json
    dict (None when the pass never wrote one — cron-run.sh deletes the stale
    file pre-pass, so a present file is always THIS pass's line)."""
    if timed_out:
        return "error", "pass hit the orchestrator timeout"
    if rc != 0:
        return "error", f"pass exited {rc}"
    if outcome is None and any("skip:" in line for line in log_tail[-3:]):
        # cron-run.sh yielded the singleton lock — actively worked, not dry.
        # Checked before the guillotine marker so a lock-yield whose log segment
        # still holds a previous pass's WARN classifies skipped-lock, not error.
        return "skipped-lock", "runner yielded the singleton lock"
    if any("Background tasks still running" in line for line in log_tail):
        return "error", "background-guillotine WARN (a build was likely killed)"
    if outcome is None:
        # A healthy pass ALWAYS writes outcome.json as its last act (skill
        # contract), so rc=0 with no outcome means the skill did NOT finish — a
        # config-read failure, early exit, or crash. Classify as error so a broken
        # deploy ESCALATES instead of silently backing off on the dry ladder (the
        # one legitimate no-outcome case, a lock-yield, is caught above).
        return "error", "pass exited 0 but wrote no outcome.json (skill did not finish)"
    if outcome.get("error"):
        return "error", f"pass reported: {outcome['error']}"
    if outcome.get("pr_opened", 0) > 0 or outcome.get("progressed"):
        return "productive", "PR opened / ticket advanced"
    if outcome.get("asked", 0) > 0 or outcome.get("blocked", 0) > 0:
        return "waiting", "question asked / ticket blocked on a human"
    if questions_count > 0:
        return "waiting", f"{questions_count} question(s) still open"
    return "dry", "ran, nothing to do"


def cmd_classify(args):
    sd = Path(args.state_dir)
    outcome = None
    f = sd / "outcome.json"
    if f.exists():
        try:
            outcome = json.loads(f.read_text())
        except json.JSONDecodeError:
            outcome = None
    tail = []
    log = sd / "logs" / "ticket-loop-cron.log"
    if log.exists():
        # Read a generous window, then keep only the current pass's segment so a
        # previous pass's guillotine WARN can't bleed into this classification.
        tail = current_pass_segment(log.read_text().splitlines()[-40:])
    questions = 0
    state_json = sd / "state.json"
    if state_json.exists():
        try:
            questions = len(json.loads(state_json.read_text()).get("questions") or {})
        except json.JSONDecodeError:
            pass
    cls, reason = classify_pass(args.rc, args.timed_out, outcome, tail, questions)
    print(f"{cls} {reason}")
    return 0


# ── backoff transitions (spec §4 + supervision §2/§3) ─────────────────────────

def apply_outcome(ps, cls, cfg, now, cadence="adaptive", interval_s=None):
    """Mutate one project's backoff state for a classified outcome; return
    (delay_seconds, [(level, message)]). Ladder indexes by streak so
    productive→10m, 1st dry→20m, 2nd→40m, then the 60m cap."""
    if cls not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class: {cls}")
    ladder = cfg["ladder_s"]
    esc = []

    def rung(streak):
        return ladder[min(streak, len(ladder) - 1)]

    real_pass = cls in ("productive", "dry", "waiting", "error")
    if cls == "productive":
        ps["dry_streak"] = ps["error_streak"] = ps["crash_streak"] = 0
        delay = ladder[0]
    elif cls in ("dry", "precheck-idle"):
        ps["dry_streak"] += 1
        ps["error_streak"] = 0
        delay = rung(ps["dry_streak"])
    elif cls == "waiting":
        # Neither the ladder nor "productive": polling faster doesn't make
        # humans answer faster, and an ignored question must not pin fast cadence.
        ps["error_streak"] = 0
        delay = cfg["waiting_interval_s"]
    elif cls == "error":
        ps["error_streak"] += 1
        delay = rung(ps["error_streak"])
        if ps["error_streak"] == cfg["error_escalate_after"]:
            n = ps["error_streak"]
            esc.append(("project", project_streak_alert(n)))
            esc.append(("ops", f"⚠️ ticket-loop: {n} consecutive failed passes — check the loop log"))
    elif cls == "crash":
        ps["crash_streak"] += 1
        ps["error_streak"] += 1
        delay = rung(ps["crash_streak"])
        if ps["crash_streak"] >= cfg["crash_park_after"]:
            ps["parked_until"] = to_iso(
                now + datetime.timedelta(seconds=cfg["crash_park_for_s"]))
            esc.append(("ops", f"🚨 parked after {ps['crash_streak']} consecutive "
                               f"crashes (until {ps['parked_until']}) — investigate "
                               "before it rejoins the roster"))
    else:  # skipped-lock — an interactive session is working the project
        delay = cfg["requeue_delay_s"]
    if cadence == "fixed" and real_pass and interval_s:
        delay = interval_s
    ps["next_eligible"] = to_iso(now + datetime.timedelta(seconds=delay))
    ps["last_outcome"] = cls
    if real_pass:
        ps["last_pass"] = to_iso(now)
        ps["last_full_pass"] = to_iso(now)
    return delay, esc


def _latest_usage_limit(state_dir):
    """(limit_hit, kind, reset_str) from the LAST record in
    <state_dir>/usage.jsonl. Tells a shared LIMIT exhaustion apart from an auth
    failure: a limit-hit pass records limit=true (usage-parse detects the limit
    text); an expired-token auth failure produces no usage envelope, so limit
    stays false. kind is 'session' (resets on its own in hours) or 'spend' (the
    org monthly spend cap — an admin must raise it); records written before the
    kind field existed default to 'session'.
    Returns (False, '', '') on any missing / unreadable / limitless file."""
    if not state_dir:
        return False, "", ""
    try:
        path = Path(state_dir) / "usage.jsonl"
        last = ""
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last:
            rec = json.loads(last)
            if isinstance(rec, dict) and rec.get("limit"):
                return (True, str(rec.get("kind") or "session"),
                        str(rec.get("reset") or ""))
    except (OSError, ValueError, TypeError):
        pass
    return False, "", ""


def project_streak_alert(n, cause=None):
    """The TEAM-GROUP copy of the failed-passes alert. Nobody in a project group
    can 'check the loop log' (that copy goes to ops), so this one says what it
    means for them: the loop keeps retrying on its schedule, ops is being alerted,
    and their messages are not lost. `cause` names a known reason (usage limit)."""
    why = f" Cause: {cause}." if cause else ""
    return (f"⏸️ ticket-loop: {n} consecutive failed passes — retrying on its schedule; "
            f"the operator channel is being alerted.{why} Messages sent here stay "
            "queued and are read once a pass succeeds.")


def _limit_cause(kind, reset):
    """Human tail for an alert whose failing pass hit a usage limit."""
    if kind == "spend":
        return ("Claude MONTHLY SPEND limit hit — an admin must raise it in "
                "the org's Claude settings → Usage (or wait for the "
                "billing-cycle reset); passes auto-resume after")
    tail = f" — {reset}" if reset else ""
    return f"Claude session limit hit (auto-resumes on reset){tail}"


def cmd_record(args):
    roster = load_roster(args.roster)
    st = load_state(args.state)
    ensure_projects(st, roster["projects"])
    now = from_iso(args.now) if args.now else now_utc()
    names = [p["name"] for p in roster["projects"]]
    if args.project not in names:
        sys.exit(f"error: unknown project {args.project!r}")
    if args.outcome not in OUTCOME_CLASSES:
        sys.exit(f"error: unknown outcome {args.outcome!r} "
                 f"(one of {', '.join(OUTCOME_CLASSES)})")
    proj = next(p for p in roster["projects"] if p["name"] == args.project)
    ps = st["projects"][args.project]
    _delay, esc = apply_outcome(ps, args.outcome, roster["cfg"], now,
                                cadence=proj["cadence"],
                                interval_s=proj["interval_s"])
    # A failed pass that hit a usage limit has a KNOWN cause — name it in the
    # streak alert instead of the generic "check the loop log" (the latest
    # usage.jsonl record is this pass's: cron-run.sh appends it pre-record).
    if args.outcome in ("error", "crash") and esc:
        hit, kind, reset = _latest_usage_limit(proj.get("state_dir"))
        if hit:
            cause = _limit_cause(kind, reset)
            # Two copies, two shapes: the ops line swaps its "check the loop
            # log" tail for the cause; the project line is regenerated whole.
            esc = [(lvl, (project_streak_alert(ps["error_streak"], cause) if lvl == "project"
                          else m.replace("check the loop log", cause))
                    if "consecutive failed passes" in m else m)
                   for lvl, m in esc]
    st.pop("pass_started", None)                       # write-ahead consumed
    st["rr_next"] = (names.index(args.project) + 1) % len(names)
    # Shared-failure heuristic (supervision §2): one ~/.claude for all projects,
    # so an expired OAuth token errors everyone at once — say so loudly, once.
    all_err = (len(names) > 1 and
               all(st["projects"][n]["error_streak"] >= 1 for n in names))
    if all_err and args.outcome in ("error", "crash") and not st.get("all_error_alerted"):
        st["all_error_alerted"] = True
        # The roster shares one token pool, so BOTH a session-limit exhaustion
        # (credits ran out) AND an expired-token auth failure error every project
        # at once — but they need different responses. A limit-hit is benign and
        # self-resolving (wait for the reset); an auth failure needs a new token.
        # Discriminate on the authoritative signal: a limit-hit leaves limit=true
        # in the tenant's latest usage.jsonl; auth failure writes no usage.
        sd_by_name = {p["name"]: p.get("state_dir") for p in roster["projects"]}
        reset_hint, limit_seen, spend_seen = "", False, False
        for n in names:
            hit, kind, reset = _latest_usage_limit(sd_by_name.get(n))
            if hit:
                limit_seen = True
                spend_seen = spend_seen or kind == "spend"
                reset_hint = reset_hint or reset
        if spend_seen:
            esc.append(("ops", "🛑 EVERY roster project is erroring — shared Claude "
                               "org MONTHLY SPEND LIMIT hit, NOT an auth problem; "
                               "an admin must raise it in the org's Claude settings "
                               "→ Usage (or wait for the billing-cycle reset); "
                               "passes auto-resume once lifted."))
        elif limit_seen:
            tail = f" — {reset_hint}" if reset_hint else ""
            esc.append(("ops", "🚦 EVERY roster project is erroring — shared Claude "
                               "SESSION LIMIT hit (credits exhausted), NOT an auth "
                               "problem; passes auto-resume once the pool "
                               f"resets{tail}."))
        else:
            esc.append(("ops", "🚨 EVERY roster project is erroring — shared-auth "
                               "failure likely (expired CLAUDE_CODE_OAUTH_TOKEN in "
                               "the shared ~/.claude?)"))
    if not all_err:
        st["all_error_alerted"] = False
    save_state(args.state, st)
    emit(args, {
        "NEXT_ELIGIBLE": ps["next_eligible"],
        "ESCALATE_PROJECT": "; ".join(m for lvl, m in esc if lvl == "project"),
        "ESCALATE_OPS": "; ".join(m for lvl, m in esc if lvl == "ops"),
    })
    return 0


# ── startup / write-ahead / status (supervision §3, §5, §8) ───────────────────

def cmd_startup(args):
    try:
        roster = load_roster(args.roster)
        for p in roster["projects"]:
            if p.get("enabled", True) or p.get("command_listener", False):
                check_work_tree(p, roster["root"])  # before their clone exists
    except RosterError as exc:
        sys.exit(f"FATAL: {exc}")
    now = from_iso(args.now) if args.now else now_utc()
    st = load_state(args.state)
    ensure_projects(st, roster["projects"])
    esc = []
    crash = ""
    pa = st.pop("pass_started", None)
    if pa and pa.get("project") in st["projects"]:
        # The container died mid-pass: count a crash for that project so an
        # OOM-on-A → restart → re-pick-A loop can't starve the rest (blocker §3).
        crash = pa["project"]
        _d, esc = apply_outcome(st["projects"][crash], "crash",
                                roster["cfg"], now)
    cleared = []
    for p in roster["projects"]:
        lock = Path(p["state_dir"]) / "loop.lock"
        if lock.exists():
            # PID namespaces reset on container restart, so a stale pid can
            # false-match a live process and silently skip every pass (§5).
            shutil.rmtree(lock, ignore_errors=True)
            cleared.append(str(lock))
        wins, tz = windows_for(p)
        if wins and seconds_until_open(wins, tz, now) is None:
            print(f"WARN: {p['name']}: roster window ∩ repo schedule.window is "
                  "empty — this project will never run", file=sys.stderr)
    save_state(args.state, st)
    emit(args, {"PROJECTS": " ".join(p["name"] + ("" if p.get("enabled", True) else "(paused)")
                                     for p in roster["projects"]),
                "CRASH_RECOVERED": crash,
                "LOCKS_CLEARED": " ".join(cleared),
                "ESCALATE_OPS": "; ".join(m for lvl, m in esc if lvl == "ops")})
    return 0


def cmd_pass_start(args):
    roster = load_roster(args.roster)
    st = load_state(args.state)
    ensure_projects(st, roster["projects"])
    now = from_iso(args.now) if args.now else now_utc()
    st["pass_started"] = {"project": args.project, "ts": to_iso(now)}
    save_state(args.state, st)
    return 0


def cmd_status(args):
    roster = load_roster(args.roster)
    st = load_state(args.state)
    ensure_projects(st, roster["projects"])
    now = now_utc()
    pa = st.get("pass_started")
    print(f"orchestrator: {len(roster['projects'])} project(s), "
          f"{'PASS RUNNING: ' + pa['project'] + ' since ' + pa['ts'] if pa else 'between passes'}")
    for p in roster["projects"]:
        ps = st["projects"][p["name"]]
        ne = ps.get("next_eligible")
        due = "now"
        if ne:
            secs = int((from_iso(ne) - now).total_seconds())
            due = f"in {secs // 60}m" if secs > 0 else "now"
        parked = f"  PARKED until {ps['parked_until']}" if ps.get("parked_until") else ""
        if not p.get("enabled", True):
            suffix = "; Telegram command listener active" if p.get("command_listener") else ""
            print(f"  {p['name']:<20} PAUSED (enabled: false) — state preserved, not scheduled{suffix}")
            continue
        print(f"  {p['name']:<20} last={ps.get('last_outcome') or '—':<14} "
              f"next={due:<8} dry={ps['dry_streak']} err={ps['error_streak']} "
              f"crash={ps['crash_streak']}  cadence={p['cadence']}{parked}")
    return 0


def _repo_url_branch(repo):
    """Normalize a roster `repo` value to (url, branch) or (None, _). Accepts a
    mapping {url, branch} or a bare url string; branch defaults to 'main'."""
    if not repo:
        return None, None
    if isinstance(repo, str):
        return repo, "main"
    if isinstance(repo, dict):
        return repo.get("url"), (repo.get("branch") or "main")
    return None, None


def cmd_seed_plan(args):
    """Emit a clone plan from the roster — one TSV line per project that declares
    a `repo`: `name\\turl\\tbranch\\twork_tree\\tenv_file`. Paused projects are
    INCLUDED (staging a paused entry then cloning it is the whole point). The
    consumer (deploy.sh onboard) skips any work tree already cloned; this command
    is pure roster→plan and touches nothing."""
    roster = load_roster(args.roster)
    for p in roster["projects"]:
        if args.project and p["name"] != args.project:
            continue
        url, branch = _repo_url_branch(p.get("repo"))
        if not url:
            continue
        print("\t".join([p["name"], url, branch, p["work_tree"], p["env_file"]]))
    return 0


def cmd_listener_plan(args):
    """Emit command listeners as TSV for the PID-1 shell supervisor.

    This is deliberately separate from scheduler eligibility: a project may be
    paused for autonomous passes and still accept explicit Telegram commands.
    """
    roster = load_roster(args.roster)
    for p in roster["projects"]:
        if not p.get("command_listener", False):
            continue
        print("\t".join([p["name"], p["work_tree"], p["env_file"], p["state_dir"]]))
    return 0


# ── CLI (subcommands are added in later tasks) ────────────────────────────────

def emit(args, pairs):
    """Print a result: shell-eval-able KEY=VALUE lines with --sh, else JSON."""
    if getattr(args, "sh", False):
        for k, v in pairs.items():
            print(f"{k}={shlex.quote('' if v is None else str(v))}")
    else:
        print(json.dumps(pairs, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, state=True):
        p.add_argument("--roster", required=True)
        if state:
            p.add_argument("--state", required=True, help="orch-state.json path")
        p.add_argument("--now", help="ISO timestamp override (tests)")
        p.add_argument("--sh", action="store_true",
                       help="print shell-eval-able KEY=VALUE lines")

    p_next = sub.add_parser("next", help="pick the next action")
    common(p_next)
    p_next.add_argument("--run-now", default=None,
                        help="project name from the run-now trigger file "
                             "('' = first roster project)")
    p_next.add_argument("--inbox-wakes", default="",
                        help="comma-separated tenants with a pending Telegram ingress "
                             "wake, oldest first (eligibility only: honors memory "
                             "floor, window, parking, a live lock; bypasses the ladder)")
    p_next.set_defaults(func=cmd_next)

    p_cls = sub.add_parser("classify", help="classify a finished pass")
    p_cls.add_argument("--state-dir", required=True)
    p_cls.add_argument("--rc", type=int, required=True)
    p_cls.add_argument("--timed-out", action="store_true")
    p_cls.set_defaults(func=cmd_classify)

    p_rec = sub.add_parser("record", help="apply a pass outcome to backoff state")
    common(p_rec)
    p_rec.add_argument("--project", required=True)
    p_rec.add_argument("--outcome", required=True)
    p_rec.set_defaults(func=cmd_record)

    p_up = sub.add_parser("startup", help="validate roster, recover write-ahead, clear locks")
    common(p_up)
    p_up.set_defaults(func=cmd_startup)

    p_ps = sub.add_parser("pass-start", help="persist the crash write-ahead record")
    common(p_ps)
    p_ps.add_argument("--project", required=True)
    p_ps.set_defaults(func=cmd_pass_start)

    p_st = sub.add_parser("status", help="human status table")
    common(p_st)
    p_st.set_defaults(func=cmd_status)

    p_sp = sub.add_parser("seed-plan",
                          help="emit the roster's clone plan (name url branch work_tree env_file)")
    p_sp.add_argument("--roster", required=True)
    p_sp.add_argument("--project", help="limit the plan to one project")
    p_sp.set_defaults(func=cmd_seed_plan)

    p_lp = sub.add_parser(
        "listener-plan",
        help="emit manual Telegram listener entries (name work_tree env_file state_dir)",
    )
    p_lp.add_argument("--roster", required=True)
    p_lp.set_defaults(func=cmd_listener_plan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
