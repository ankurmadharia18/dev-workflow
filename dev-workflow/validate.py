#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Validate a dev-workflow.yml against the framework contract.
Run with `uv run dev-workflow/validate.py <yml>` (uv supplies PyYAML), or plain
python3 if pyyaml is installed.

Boundary rule 1: a target-repo config can only TIGHTEN the framework
defaults, never loosen them. Unknown top-level keys are rejected, and the
diff-budget / cap-per-pass ceilings are hard caps a config may lower but
never raise. The framework baseline (secrets, protected paths, no direct
push to the base/prod branch) is enforced in the runner, not here — this
validator only guards the shape and the tighten-only ceilings.

Usage:
    python3 dev-workflow/validate.py <path/to/dev-workflow.yml>

Exit 0 and print `OK: <path>` on success; exit 1 with one `ERROR: <msg>`
line per violation.
"""
import re
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "ERROR: PyYAML is required. Install it with "
        "`pip install pyyaml` (or `apt install python3-yaml`).\n"
    )
    sys.exit(1)

ALLOWED_TOP = {"repo", "tracker", "chat", "quality", "version", "deploy",
               "board", "guardrails", "build", "schedule", "hooks", "runtime",
               "blog", "agent", "repos", "notify"}
REQUIRED = {"repo": ["base_branch", "prod_branch"], "tracker": ["provider"]}
BASELINE_OFF_LIMITS = [".env*", "*.key", "*.pem", "credentials.json",
                       ".claude/settings*", ".github/workflows/**"]
CEILING = {"max_lines": 400, "max_files": 15, "cap_per_pass": 3}
KNOWN_TRACKERS = {"github", "linear"}
KNOWN_CHAT = {"telegram"}

WINDOW_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")


def _is_int(v):
    # bool is a subclass of int — a YAML `true`/`false` is not a valid count.
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def check(data):
    """Return (errors, infos): two lists of message strings."""
    errors = []
    infos = []

    if not isinstance(data, dict):
        errors.append("top level of the config must be a YAML mapping")
        return errors, infos

    # No unknown top-level keys.
    for key in data:
        if key not in ALLOWED_TOP:
            errors.append(
                "unknown top-level key: %r (allowed: %s)"
                % (key, ", ".join(sorted(ALLOWED_TOP)))
            )

    # Required fields present and non-empty strings.
    for section, fields in REQUIRED.items():
        block = data.get(section)
        if not isinstance(block, dict):
            for field in fields:
                errors.append("missing required field: %s.%s" % (section, field))
            continue
        for field in fields:
            if field not in block:
                errors.append("missing required field: %s.%s" % (section, field))
            elif not _nonempty_str(block[field]):
                errors.append("%s.%s must be a non-empty string" % (section, field))

    # tracker.provider must be a known adapter.
    tracker = data.get("tracker")
    if isinstance(tracker, dict):
        provider = tracker.get("provider")
        if provider is not None and provider not in KNOWN_TRACKERS:
            errors.append(
                "tracker.provider %r is not supported (known: %s)"
                % (provider, ", ".join(sorted(KNOWN_TRACKERS)))
            )
        if provider == "linear":
            for field in ("team", "ticket_prefix"):
                if not _nonempty_str(tracker.get(field)):
                    errors.append("tracker.%s is required for the linear provider" % field)
        elif provider == "github":
            if not _nonempty_str(tracker.get("repo")):
                errors.append("tracker.repo is required for the github provider")
        # tracker.project (optional) — a Linear Project scoping this repo inside a
        # team shared across repos. When present it must be a non-empty string.
        if "project" in tracker and not _nonempty_str(tracker.get("project")):
            errors.append("tracker.project must be a non-empty string when set")
        # tracker.intake_project (optional, PARENT-ONLY) — the durable parking-lot
        # Project. When present it must be a non-empty string.
        if "intake_project" in tracker and not _nonempty_str(tracker.get("intake_project")):
            errors.append("tracker.intake_project must be a non-empty string when set")
        # tracker.roles — when present, the role→name bindings must be filled.
        roles = tracker.get("roles")
        if isinstance(roles, dict):
            role_reqs = [("queue", "label"), ("blocked", "label"), ("done", "state")]
            for role, attr in role_reqs:
                sub = roles.get(role)
                if not isinstance(sub, dict) or not _nonempty_str(sub.get(attr)):
                    errors.append(
                        "tracker.roles.%s.%s is required when tracker.roles is set"
                        % (role, attr)
                    )
            # Optional roles (Epics C/D): when present, each needs a non-empty label.
            for role in ("claimed", "flagged", "dep_blocked"):
                sub = roles.get(role)
                if sub is not None and (not isinstance(sub, dict) or not _nonempty_str(sub.get("label"))):
                    errors.append(
                        "tracker.roles.%s.label must be a non-empty string when set" % role
                    )

    # tracker.intake_project is PARENT-ONLY — it only means something when repos:
    # is present (a parking lot for reported work not yet routed to a child repo).
    if isinstance(tracker, dict) and "intake_project" in tracker and data.get("repos") is None:
        errors.append("tracker.intake_project is only meaningful in a parent config "
                      "(repos: present)")

    # board (optional) — gate labels + prune policy. All keys optional; a repo
    # without them still validates.
    board = data.get("board")
    if isinstance(board, dict):
        gates = board.get("gates")
        if gates is not None and (
            not isinstance(gates, list) or not all(isinstance(x, str) for x in gates)
        ):
            errors.append("board.gates must be a list of strings")
        prune = board.get("prune")
        if prune is not None:
            if not isinstance(prune, dict):
                errors.append("board.prune must be a mapping")
            else:
                if "allow_delete" in prune and not isinstance(prune["allow_delete"], bool):
                    errors.append("board.prune.allow_delete must be a boolean")
                if "threshold_days" in prune:
                    td = prune["threshold_days"]
                    if not _is_int(td) or td <= 0:
                        errors.append("board.prune.threshold_days must be an integer > 0")

    # chat.provider must be a known channel when chat is present.
    chat = data.get("chat")
    if isinstance(chat, dict):
        provider = chat.get("provider")
        if provider is not None and provider not in KNOWN_CHAT:
            errors.append(
                "chat.provider %r is not supported (known: %s)"
                % (provider, ", ".join(sorted(KNOWN_CHAT)))
            )

    # guardrails.off_limits ADDS to the baseline — must be a list of strings.
    guardrails = data.get("guardrails")
    if isinstance(guardrails, dict):
        off = guardrails.get("off_limits")
        if off is not None:
            if not isinstance(off, list) or not all(isinstance(x, str) for x in off):
                errors.append("guardrails.off_limits must be a list of strings")
            else:
                infos.append(
                    "guardrails.off_limits ADDS to the framework baseline "
                    "(always off-limits: %s)" % ", ".join(BASELINE_OFF_LIMITS)
                )
        # diff-budget ceilings (tighten-only).
        budget = guardrails.get("diff_budget")
        if isinstance(budget, dict):
            for field in ("max_lines", "max_files"):
                if field in budget:
                    _check_ceiling(errors, "guardrails.diff_budget", field, budget[field])

    # build.cap_per_pass ceiling (tighten-only).
    build = data.get("build")
    if isinstance(build, dict):
        if "cap_per_pass" in build:
            _check_ceiling(errors, "build", "cap_per_pass", build["cap_per_pass"])
        # model fields, when present, must be non-empty strings (any model id or
        # alias — the runner passes them through to `claude --model` / the Task
        # tool verbatim).
        for field in (
            "model",
            "subagent_model",
            "codex_model",
            "codex_model_reasoning_effort",
            "codex_subagent_model",
            "codex_subagent_model_reasoning_effort",
        ):
            if field in build and not _nonempty_str(build[field]):
                errors.append("build.%s must be a non-empty string" % field)

    # blog (optional) — enables cleanup's blog-proposal step. Its skill/posts_dir/
    # publish values, when present, must be non-empty strings. Omitting `publish`
    # means "no publish command" (cleanup never publishes on its own regardless).
    blog = data.get("blog")
    if blog is not None:
        if not isinstance(blog, dict):
            errors.append("blog must be a mapping")
        else:
            for field in ("skill", "posts_dir", "publish"):
                if field in blog and not _nonempty_str(blog[field]):
                    errors.append("blog.%s must be a non-empty string" % field)

    # agent (optional) — the v2 local-agent FEATURE OPT-IN, not a guardrail. It is
    # deliberately independent of the tighten-only ceilings: `enabled` never raises
    # or lowers a cap, it only turns the local autonomous tier (the ticket-loop skill
    # + install-cron.sh) on. Default OFF — an absent section or key means disabled;
    # the runner/skill treat anything but exactly true as opt-out. `true`/`false` are
    # both valid; a non-boolean is an error.
    agent = data.get("agent")
    if agent is not None:
        if not isinstance(agent, dict):
            errors.append("agent must be a mapping")
        else:
            if "enabled" in agent and not isinstance(agent["enabled"], bool):
                errors.append("agent.enabled must be a boolean (true/false)")
            # agent.skill (optional) — a BARE skill NAME the runner invokes (the
            # roster entry's `skill:` overrides it). No ':' — the runner
            # namespaces it (/skill or /dev-workflow:skill); a ':' double-prefixes.
            # agent.manager (optional) — parent/manager mode: no work-tree reset.
            if "skill" in agent:
                sk = agent.get("skill")
                if not _nonempty_str(sk):
                    errors.append("agent.skill must be a non-empty string when set")
                elif ":" in sk or any(c.isspace() for c in sk):
                    errors.append("agent.skill must be a bare skill name "
                                  "(no ':' or spaces — the runner namespaces it)")
            if "manager" in agent and not isinstance(agent["manager"], bool):
                errors.append("agent.manager must be a boolean (true/false)")

    # repos (optional) — the parent-orchestration child map (one product, many
    # repos under one team/group). A list of mappings, each with a non-empty
    # string `project` (its Linear Project) and `path` (clone dir under the parent).
    repos = data.get("repos")
    if repos is not None:
        if not isinstance(repos, list) or not repos:
            errors.append("repos must be a non-empty list when set")
        else:
            seen_projects, seen_paths = set(), set()
            for i, r in enumerate(repos):
                if not isinstance(r, dict):
                    errors.append("repos[%d] must be a mapping" % i)
                    continue
                for k in ("project", "path"):
                    if not _nonempty_str(r.get(k)):
                        errors.append("repos[%d].%s must be a non-empty string" % (i, k))
                # 1 project -> 1 child: duplicates break routing (a ticket's project
                # must resolve to exactly one clone).
                proj, path = r.get("project"), r.get("path")
                if _nonempty_str(proj):
                    if proj in seen_projects:
                        errors.append("repos: duplicate project %r (each maps to one child)" % proj)
                    seen_projects.add(proj)
                if _nonempty_str(path):
                    if path in seen_paths:
                        errors.append("repos: duplicate path %r" % path)
                    seen_paths.add(path)
            # A parent config (repos: present) reads the WHOLE team — a
            # tracker.project here would wrongly scope the product to one repo.
            if isinstance(tracker, dict) and tracker.get("project"):
                errors.append("tracker.project must NOT be set when repos: is present "
                              "(a parent reads the whole team; per-repo scope lives in each child)")
            # The intake parking lot must never be one of the child projects — an
            # intake ticket must never be buildable.
            if isinstance(tracker, dict) and _nonempty_str(tracker.get("intake_project")) \
                    and tracker.get("intake_project") in seen_projects:
                errors.append("tracker.intake_project %r must NOT be one of the repos: projects "
                              "(an intake ticket must never be buildable)" % tracker.get("intake_project"))

    # schedule.window format.
    schedule = data.get("schedule")
    if isinstance(schedule, dict):
        window = schedule.get("window")
        if window is not None and not (isinstance(window, str) and WINDOW_RE.match(window)):
            errors.append("schedule.window %r must match HH:MM-HH:MM" % (window,))

    return errors, infos


def _check_ceiling(errors, section, field, value):
    ceiling = CEILING[field]
    if not _is_int(value):
        errors.append("%s.%s must be an integer" % (section, field))
    elif value > ceiling:
        errors.append(
            "%s.%s %s exceeds the framework ceiling of %s (config may lower it, never raise it)"
            % (section, field, value, ceiling)
        )


def validate_file(path):
    """Return a list of error strings (empty == valid). Info lines print to stdout."""
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return ["no such file: %s" % path]
    except yaml.YAMLError as exc:
        return ["%s is not valid YAML: %s" % (path, exc)]
    errors, infos = check(data)
    for info in infos:
        print("INFO: %s" % info)
    return errors


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: validate.py <path/to/dev-workflow.yml>\n")
        return 2
    path = argv[1]
    errors = validate_file(path)
    if errors:
        for err in errors:
            print("ERROR: %s" % err)
        return 1
    print("OK: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
