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
