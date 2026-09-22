#!/usr/bin/env python3
"""GitHub Issues adapter for the manually triggered local ticket loop.

Planning is read-only. Manual execution additionally claims selected issues by
moving them from the configured ready label to the claimed label; a failed
Claude launch restores the original labels.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable


class GitHubAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    url: str
    created_at: str
    labels: tuple[str, ...]
    state: str = "OPEN"


@dataclass(frozen=True)
class GitHubPullRequest:
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    merge_state_status: str
    checks_passed: int = 0
    checks_pending: int = 0
    checks_failed: int = 0


def _label_names(raw_labels: Iterable[object]) -> tuple[str, ...]:
    names = []
    for label in raw_labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
        elif isinstance(label, str):
            names.append(label)
    return tuple(names)


def list_actionable(
    repo: str,
    queue_label: str,
    exclude_labels: Iterable[str],
    *,
    limit: int = 100,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[GitHubIssue]:
    """Return oldest-first open issues carrying the queue label.

    This function is read-only. It never creates labels, edits issues, or
    claims work.
    """
    command = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--label",
        queue_label,
        "--limit",
        str(limit),
        "--json",
        "number,title,url,createdAt,labels",
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        raise GitHubAdapterError("GitHub issue lookup failed: %s" % detail)
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubAdapterError("GitHub issue lookup returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise GitHubAdapterError("GitHub issue lookup returned an unexpected payload")

    excluded = set(exclude_labels)
    issues = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        labels = _label_names(raw.get("labels", []))
        if excluded.intersection(labels):
            continue
        issues.append(
            GitHubIssue(
                number=int(raw["number"]),
                title=str(raw["title"]),
                url=str(raw["url"]),
                created_at=str(raw.get("createdAt", "")),
                labels=labels,
            )
        )
    return sorted(issues, key=lambda issue: (issue.created_at, issue.number))


def get_issue(
    repo: str,
    issue_number: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GitHubIssue:
    """Read one GitHub issue without mutating it."""
    command = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "number,title,url,createdAt,labels,state",
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        raise GitHubAdapterError(
            "Could not read GitHub issue #%d: %s" % (issue_number, detail)
        )
    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GitHubAdapterError("GitHub issue lookup returned invalid JSON") from exc
    if not isinstance(raw, dict) or not raw.get("number"):
        raise GitHubAdapterError("GitHub issue lookup returned an unexpected payload")
    return GitHubIssue(
        number=int(raw["number"]),
        title=str(raw["title"]),
        url=str(raw["url"]),
        created_at=str(raw.get("createdAt", "")),
        labels=_label_names(raw.get("labels", [])),
        state=str(raw.get("state", "OPEN")),
    )


def find_issue_pull_request(
    repo: str,
    issue_number: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GitHubPullRequest | None:
    """Read the newest PR for the ticket loop's conventional issue branch."""
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        "codex/issue-%d" % issue_number,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "number,title,url,state,isDraft,mergeStateStatus,statusCheckRollup",
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        raise GitHubAdapterError(
            "Could not read PR for issue #%d: %s" % (issue_number, detail)
        )
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubAdapterError("GitHub PR lookup returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise GitHubAdapterError("GitHub PR lookup returned an unexpected payload")
    if not payload:
        return None
    raw = payload[0]
    if not isinstance(raw, dict) or not raw.get("number"):
        raise GitHubAdapterError("GitHub PR lookup returned an unexpected payload")

    passed = pending = failed = 0
    for check in raw.get("statusCheckRollup") or []:
        if not isinstance(check, dict):
            continue
        state = str(check.get("conclusion") or check.get("state") or "").upper()
        if state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            passed += 1
        elif state in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}:
            failed += 1
        else:
            pending += 1
    return GitHubPullRequest(
        number=int(raw["number"]),
        title=str(raw.get("title") or ""),
        url=str(raw.get("url") or ""),
        state=str(raw.get("state") or "UNKNOWN"),
        is_draft=bool(raw.get("isDraft")),
        merge_state_status=str(raw.get("mergeStateStatus") or "UNKNOWN"),
        checks_passed=passed,
        checks_pending=pending,
        checks_failed=failed,
    )


def add_label(
    repo: str,
    issue_number: int,
    label: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Add one configured workflow label to an issue."""
    command = [
        "gh",
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo,
        "--add-label",
        label,
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        raise GitHubAdapterError(
            "Could not label GitHub issue #%d: %s" % (issue_number, detail)
        )


def set_claimed(
    repo: str,
    issue_number: int,
    ready_label: str,
    claimed_label: str,
    *,
    claimed: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Claim an issue, or restore it to the ready queue after a failed run."""
    add_label = claimed_label if claimed else ready_label
    remove_label = ready_label if claimed else claimed_label
    command = [
        "gh",
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo,
        "--add-label",
        add_label,
        "--remove-label",
        remove_label,
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        action = "claim" if claimed else "restore"
        raise GitHubAdapterError(
            "Could not %s GitHub issue #%d: %s" % (action, issue_number, detail)
        )


def set_blocked(
    repo: str,
    issue_number: int,
    blocked_label: str,
    *,
    blocked: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Apply or remove the configured human-clarification hold label."""
    action = "--add-label" if blocked else "--remove-label"
    command = [
        "gh",
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo,
        action,
        blocked_label,
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        verb = "block" if blocked else "unblock"
        raise GitHubAdapterError(
            "Could not %s GitHub issue #%d: %s" % (verb, issue_number, detail)
        )


def comment_issue(
    repo: str,
    issue_number: int,
    body: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Persist a clarification question or answer on its GitHub issue."""
    command = [
        "gh",
        "issue",
        "comment",
        str(issue_number),
        "--repo",
        repo,
        "--body",
        body,
    ]
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
        raise GitHubAdapterError(
            "Could not comment on GitHub issue #%d: %s" % (issue_number, detail)
        )
