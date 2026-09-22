#!/usr/bin/env python3
"""Cheap tracker queue-depth pre-check for the ticket-loop orchestrator.

Implements the read-only `queue_count` verb of the tracker-adapter seam for
Linear and GitHub Issues: count actionable tickets using the SAME eligibility
definition as `list_actionable` (see tracker-adapters.md; one source of truth,
so the pre-check cannot silently drift from what a real pass would pick up).

    LINEAR_API_KEY=lin_api_...  queue-count.py --config <work_tree>/dev-workflow.yml

Prints a bare integer on stdout (exit 0). ANY failure — missing key, bad
config, network error, GraphQL error — exits non-zero with a message on
stderr; the orchestrator treats that as "fail open" and runs the pass.

Config parsing is delegated to the sibling dw-config.py
(same directory in both layouts: dev-workflow/ in the repo, /opt/dev-workflow/bin
in the image), so the YAML handling — PyYAML with a stdlib fallback — is never
duplicated.
"""

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from github_issues import GitHubAdapterError, list_actionable

LINEAR_URL = "https://api.linear.app/graphql"

GQL = ("query($filter: IssueFilter) { issues(filter: $filter, first: 100) "
       "{ nodes { identifier labels { nodes { name } } } } }")


def load_config(path):
    """Parse dev-workflow.yml via the sibling dw-config.py; returns (data, module)."""
    dwc = Path(__file__).resolve().parent / "dw-config.py"
    if not dwc.exists():
        sys.exit(f"error: dw-config.py not found next to {__file__}")
    spec = importlib.util.spec_from_file_location("dwconfig", dwc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        with open(path) as fh:
            return mod._load(fh), mod
    except OSError as exc:
        sys.exit(f"error: cannot read {path}: {exc}")


def read_roles(data, mod):
    """tracker.team + optional tracker.project + queue role + exclude labels,
    straight from tracker.roles — never hardcoded names (the adapter-seam hard
    rule). `project` (a Linear Project name) scopes one repo inside a shared team
    when several repos share one board; None when absent."""
    team = mod.get(data, "tracker.team")
    queue = mod.get(data, "tracker.roles.queue")
    if team is mod._MISSING or queue is mod._MISSING or not isinstance(queue, dict):
        sys.exit("error: tracker.team / tracker.roles.queue missing in config")
    label, states = queue.get("label"), queue.get("states")
    if not label or not isinstance(states, list) or not states:
        sys.exit("error: tracker.roles.queue needs `label` and non-empty `states`")
    exclude = mod.get(data, "tracker.roles.exclude")
    excludes = []
    if isinstance(exclude, dict) and isinstance(exclude.get("labels"), list):
        excludes = [str(x) for x in exclude["labels"]]
    project = mod.get(data, "tracker.project")
    project = str(project) if project not in (mod._MISSING, None, "") else None
    return str(team), str(label), [str(s) for s in states], excludes, project


def build_payload(team, label, states, project=None):
    """Same eligibility filter as list_actionable. When `project` is set, scope to
    that Linear Project too — the per-repo slice of a team shared across repos."""
    flt = {
        "team": {"name": {"eq": team}},
        "labels": {"name": {"eq": label}},
        "state": {"name": {"in": states}},
    }
    if project:
        flt["project"] = {"name": {"eq": project}}
    return {"query": GQL, "variables": {"filter": flt}}


def count_eligible(body, exclude_labels):
    """Count nodes carrying none of the exclude labels (client-side filter,
    mirroring the Linear list_actionable mapping)."""
    ex = {e.lower() for e in exclude_labels}
    n = 0
    for node in body["data"]["issues"]["nodes"]:
        names = {l["name"].lower() for l in node["labels"]["nodes"]}
        if names & ex:
            continue
        n += 1
    return n


def read_provider(data, mod):
    provider = mod.get(data, "tracker.provider")
    if provider is mod._MISSING:
        return "linear"
    provider = str(provider).strip().lower()
    if provider not in {"linear", "github"}:
        sys.exit(f"error: unsupported tracker.provider: {provider}")
    return provider


def read_github_roles(data, mod):
    repo = mod.get(data, "tracker.repo")
    queue = mod.get(data, "tracker.roles.queue")
    if repo is mod._MISSING or queue is mod._MISSING or not isinstance(queue, dict):
        sys.exit("error: tracker.repo / tracker.roles.queue missing in config")
    label = queue.get("label")
    if not label:
        sys.exit("error: tracker.roles.queue needs `label`")
    exclude = mod.get(data, "tracker.roles.exclude")
    excludes = []
    if isinstance(exclude, dict) and isinstance(exclude.get("labels"), list):
        excludes = [str(x) for x in exclude["labels"]]
    return str(repo), str(label), excludes


def count_github(data, mod):
    repo, label, excludes = read_github_roles(data, mod)
    try:
        return len(list_actionable(repo, label, excludes))
    except GitHubAdapterError as exc:
        sys.exit(f"error: {exc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to dev-workflow.yml")
    args = ap.parse_args()
    data, mod = load_config(args.config)
    provider = read_provider(data, mod)
    if provider == "github":
        print(count_github(data, mod))
        return
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        sys.exit("error: LINEAR_API_KEY is not set")
    team, label, states, excludes, project = read_roles(data, mod)
    req = urllib.request.Request(
        LINEAR_URL,
        data=json.dumps(build_payload(team, label, states, project)).encode(),
        # Personal API keys go bare in Authorization (Bearer is for OAuth tokens).
        headers={"Content-Type": "application/json", "Authorization": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        sys.exit(f"error: linear graphql unreachable: {exc}")
    if body.get("errors"):
        sys.exit(f"error: linear: {body['errors'][0].get('message', 'unknown')}")
    print(count_eligible(body, excludes))


if __name__ == "__main__":
    main()
