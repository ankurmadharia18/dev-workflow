"""Opt-in, non-authoritative Jev comparison for Telegram intent routing.

The live Claude route always wins. This module never launches an action, stores
message text, or sends anything to TypeSafe unless DW_JEV_SHADOW=1 is set.
"""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from urllib import request


API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
ACTIONS = (
    "review_feedback",
    "independent_review",
    "start_issues",
    "status",
    "answer",
    "clarify",
    "ignore",
)
_SLOTS = threading.BoundedSemaphore(2)


def _state(message: dict, progress: dict) -> dict:
    """Send only routing context, not PR bodies, findings, logs, or secrets."""
    active = []
    issues = progress.get("issues")
    if isinstance(issues, dict):
        for number, item in list(issues.items())[:3]:
            if isinstance(item, dict):
                active.append({"issue": str(number), "phase": str(item.get("phase") or "")})
    return {
        "message": str(message.get("text") or "")[:2000],
        "reply_to": str(message.get("reply_to_text") or "")[:500],
        "conversation_context": str(message.get("context") or "")[:500],
        "active_issues": active,
    }


def _questions() -> dict:
    return {
        "intent": {
            "type": "choice",
            "instructions": "What workflow action does the human request in the current message? Classify the request, not an action described in quoted context.",
            "criteria": {
                "review_feedback": "Work on an existing PR: address comments, CI, requested edits, or record that a review finding is rejected.",
                "independent_review": "Launch a new, separate, fresh reviewer agent to inspect an existing PR.",
                "start_issues": "Begin implementing one or more GitHub issues.",
                "status": "Report the current or past progress of an issue, PR, agent, or listener.",
                "answer": "Answer a factual question about work already done or recorded, without starting new work.",
                "clarify": "The human requests an action but its intent is unclear.",
                "ignore": "Social conversation with no workflow request.",
            },
        },
        "rejects_review_finding": {
            "type": "noul",
            "instructions": "Does the current human message explicitly reject a review finding or say not to implement its proposed fix?",
        },
    }


def evaluate(message: dict, progress: dict, api_key: str) -> dict:
    payload = json.dumps(
        {"state": _state(message, progress), "model": MODEL, "questions": _questions()}
    ).encode("utf-8")
    incoming = request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(incoming, timeout=4) as response:
        answers = json.load(response)["answers"]
    intent = answers["intent"]
    action = intent["choice"]
    confidence = intent["confidence"]
    rejection = answers["rejects_review_finding"]["noul"]
    if action not in ACTIONS or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("invalid Jev intent result")
    if not isinstance(rejection, (int, float)) or not 0 <= rejection <= 1:
        raise ValueError("invalid Jev rejection result")
    return {
        "action": action,
        "confidence": round(float(confidence), 4),
        "rejects_review_finding": round(float(rejection), 4),
    }


def _append_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(result, separators=(",", ":")) + "\n")
        output.flush()
        fcntl.flock(output, fcntl.LOCK_UN)


def _observe(message: dict, progress: dict, live_action: str, api_key: str, path: Path) -> None:
    record = {
        "observed_at": int(time.time()),
        "message_id": message.get("message_id"),
        "live_action": live_action,
        "jev_model": MODEL,
    }
    try:
        record["jev"] = evaluate(message, progress, api_key)
        record["agrees"] = record["jev"]["action"] == live_action
    except Exception as exc:
        # Never log exception text: HTTP errors can contain request data.
        record["error_type"] = type(exc).__name__
    try:
        _append_result(path, record)
    finally:
        _SLOTS.release()


def observe_async(message: dict, progress: dict, live_action: str, state_dir: Path) -> bool:
    """Best-effort shadow call; never delays or changes the live decision."""
    if os.environ.get("DW_JEV_SHADOW") != "1":
        return False
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key or not _SLOTS.acquire(blocking=False):
        return False
    try:
        threading.Thread(
            target=_observe,
            args=(message, progress, live_action, api_key, state_dir / "jev-shadow.jsonl"),
            daemon=True,
            name="jev-shadow",
        ).start()
    except Exception:
        _SLOTS.release()
        return False
    return True


def summarize(path: Path) -> dict:
    """Read the local metadata-only log without displaying message content."""
    totals = Counter()
    disagreements = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            totals["observations"] += 1
            if "error_type" in record:
                totals["errors"] += 1
            elif record.get("agrees"):
                totals["agreements"] += 1
            else:
                totals["disagreements"] += 1
                disagreements.append(
                    {
                        "message_id": record.get("message_id"),
                        "live_action": record.get("live_action"),
                        "jev_action": (record.get("jev") or {}).get("action"),
                        "confidence": (record.get("jev") or {}).get("confidence"),
                    }
                )
    return {"counts": dict(totals), "disagreements": disagreements}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize Jev shadow comparisons")
    parser.add_argument("log", type=Path, help="path to jev-shadow.jsonl")
    args = parser.parse_args()
    print(json.dumps(summarize(args.log), indent=2))
