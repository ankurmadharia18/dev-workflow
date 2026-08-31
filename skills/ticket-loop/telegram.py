#!/usr/bin/env python3
"""Telegram bridge for the ticket-loop skill.

Subcommands:
  send [--ticket ABC-123] [--project P] [--context C] [--replace KEY] <text...>
                                      Send a message to the agent group; prints {"message_id": N}.
                                      With --ticket, records message_id -> ticket so a Telegram reply
                                      to that message matches the ticket on poll. --project tags the
                                      reply with a Linear Project (multi-repo routing without a tracker
                                      read). --context records a PRE-TICKET question (ticket=None, e.g.
                                      a "which project?" clarifier) so a plain reply routes back to it.
                                      --replace KEY edits the message previously sent under KEY in
                                      place (a status line such as the heartbeat: one message that
                                      updates instead of one per pass); prints {"message_id", "edited"}.
                                      Not combinable with the routing flags.
  poll [--timeout N]                  Fetch new human messages in the agent group; prints one JSON
                                      line per message. Under the orchestrator's Telegram INGRESS
                                      (TICKET_LOOP_INGRESS=1, or a fresh <state>/ingress.json) the
                                      ingress daemon is the only getUpdates consumer: poll reads the
                                      spool <state>/inbox/<update_id>.json, consumes each record by
                                      renaming it .done, and never touches the network for inbound.
                                      Otherwise (single-mode timer, laptop) it long-polls getUpdates
                                      itself (default 25s). Either way the output line is:
                                      {message_id, from, from_id, text, ticket, project, context,
                                       reply_to_message_id, reply_to_text, reply_to_from_bot,
                                       media_path}
                                      `ticket`/`project`/`context` come from the replied-to question
                                      entry; `ticket` also from an ABC-### prefix (any TEAM-123 key),
                                      else from the ONE key a replied-to BOT headline names
                                      ("✅ ABC-12 — PR opened"). `reply_to_text` is the quoted
                                      message (≤700 chars) so a reply to a heartbeat/alert/PR post
                                      can be read in context; `reply_to_from_bot` says who wrote it.
                                      A reply to a pre-ticket clarifier carries `context` with `ticket`
                                      null.
                                      Photos (and image documents) are downloaded to
                                      <repo>/.agent-loop/media/<message_id>.<ext>; `media_path` carries
                                      the local path (null for text-only messages), `text` the caption.
  peek                                Count pending human messages WITHOUT consuming them
                                      (read-only; the orchestrator pre-check). Spool mode: a local
                                      file count, no network.
  ingress [--mode]                    Report whether this state dir is ingress-served: JSON
                                      {mode: spool|direct, marker, pending}; --mode prints just the
                                      word (the orchestrator sets TICKET_LOOP_INGRESS from it).
  send-photo [--ticket ABC-123] [--caption TEXT] <path>
                                      Send an image file to the agent group; prints {"message_id": N}.
  send-document [--caption TEXT] <path>
                                      Send a file (e.g. a PDF report) to the agent group; prints {"message_id": N}.
  react [--clear] <message_id> [EMOJI]
                                      Set (or clear) the bot's emoji reaction on a group message — the
                                      visible "seen / handled" ack (default 👍). Only Telegram's fixed
                                      reaction set is accepted (👀 👍 🔥 🎉 … — ✅ is NOT in it).
  discover                            Print every chat the bot has recently seen (id, type, title) —
                                      use to grab a new group's chat id. Does not consume updates
                                      (but 409s while a live ingress long-polls the same bot — run
                                      it before the bot is rostered, or briefly stop the orchestrator).

Env (from the environment or repo-root .env): TELEGRAM_BOT_TOKEN, AGENT_TELEGRAM_CHAT_ID.
TELEGRAM_SHARED_BOT=1 switches poll/peek to shared-bot (no-ack) mode: the bot token
is shared by several projects (one group each), so getUpdates is NEVER called with
an offset — an offset acks updates bot-wide and would destroy the other projects'
pending messages. Each project instead filters to its own chat and keeps a local
floor in state.json `offset`. Consequences: updates expire server-side after 24h
(same retention the acked flow has), and the scan window is Telegram's first 100
unacked updates — fine for a handful of low-traffic groups, wrong for busy ones.
State dir: <repo>/.agent-loop by default; override with TICKET_LOOP_STATE_DIR (an
absolute path, or one relative to the repo root). Holds state.json
{offset, questions: {message_id: {ticket, text, asked_at}}} + downloaded media —
gitignore it. Legacy bare {message_id: "ABC-123"} entries still load.
Stdlib only — runs under any python3.

  questions [--json] [--clear <id|TICKET>]
                                      Inspect / clear open clarifying questions from state.json —
                                      no Telegram API, no secrets, never touches `offset`.
                                      No flag → list (oldest-first). --json → raw entry list for
                                      the agent. --clear <id> drops one message_id; --clear TICKET
                                      (case-insensitive) drops every entry for that ticket.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def find_repo_root() -> Path:
    """Walk up from cwd (then the script location) to the nearest directory
    containing .git. cwd is tried FIRST because it is the repo you're operating
    on — the work tree you invoke the bridge from. The script itself is baked
    into the framework/plugin, whose OWN tree may be a git checkout (a plugin or
    dev checkout of dev-workflow); resolving from there would read that repo's
    .env instead of the target repo's. (No effect on the box: run-pass sets
    TICKET_LOOP_STATE_DIR and the creds live in the env, so REPO_ROOT is unused
    there — this only fixes the repo-root .env path in interactive/local runs.)"""
    for base in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (base, *base.parents):
            if (candidate / ".git").exists():
                return candidate
    return Path.cwd()


def state_dir() -> Path:
    """The loop's state dir: TICKET_LOOP_STATE_DIR (absolute, or relative to the
    repo root) when set, else <repo>/.agent-loop. Keeps the runner, the lock, and
    this bridge pointed at the same directory across laptop + container layouts."""
    override = os.environ.get("TICKET_LOOP_STATE_DIR", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / ".agent-loop"


REPO_ROOT = find_repo_root()
STATE_PATH = state_dir() / "state.json"
TICKET_RE = re.compile(r"^\s*([A-Z][A-Z0-9]*-\d+)\b", re.IGNORECASE)
# A bot headline that names ONE ticket in its subject slot: one of the skill's
# ticket-headline markers, an optional verb ("Starting"/"Resuming"), then the key —
# e.g. "✅ ABC-12 — merged", "❓ ABC-12 — title", "🔨 Starting ABC-12 — plan",
# "👍 ABC-12 queued". The marker set is closed on purpose: 💬 answers, 📊 digests,
# 💤 idle notes, ▶️ heartbeats never bind. Anchored to the first line, and only
# honoured when that line holds exactly one key, so a "blocked by ABC-7" line or
# prose like ISO-8601 never binds a reply to the wrong ticket.
HEADLINE_RE = re.compile(
    r"^(?:✅|❓|🧭|🔨|⚠️|🔁|🔀|👍|🐛|💡|🚩|⏸️|🗑|🙅)\s+"
    r"(?:Starting\s+|Resuming\s+|Taking\s+)?([A-Z][A-Z0-9]*-\d+)"
    # …and the key must sit in a template slot: followed by a dash/arrow/colon or
    # one of the skill's headline verbs — so "⚠️ ISO-8601 dates" never binds.
    r"(?=\s*(?:—|–|→|:|$)|\s+(?:queued|logged|merged|held|failed|ballooned|revision|PR|asks|"
    r"cancelled|is|taken|unblocked|split|re-scoped)\b)")
ANY_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b", re.IGNORECASE)   # counting only: any case, any key-shaped token
REPLY_TEXT_MAX = 700          # chars of the quoted message carried on a poll line


def load_env() -> None:
    """Load repo-root .env into os.environ without overriding existing vars."""
    import os

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def shared_bot() -> bool:
    """True when TELEGRAM_SHARED_BOT marks this bot token as shared across
    projects — poll/peek must then never pass `offset` to getUpdates (no acks;
    see the module docstring)."""
    return os.environ.get("TELEGRAM_SHARED_BOT", "").strip().lower() in ("1", "true", "yes")


def require_env(name: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"error: {name} is not set (add it to your repo-root .env — see env.example next to this script)")
    return value


class TelegramError(Exception):
    """A Telegram API call that failed (HTTP error, unreachable, or ok=false).
    `api` turns it into a clean exit; callers with a fallback catch it."""


def api(method: str, params: dict, *, http_timeout: int = 35) -> dict:
    try:
        return api_raw(method, params, http_timeout=http_timeout)
    except TelegramError as exc:
        sys.exit(f"error: {exc}")


def api_raw(method: str, params: dict, *, http_timeout: int = 35) -> dict:
    """Like `api`, but raises TelegramError instead of exiting."""
    return _request(method, urllib.parse.urlencode(params).encode(), {}, http_timeout)


def request(method: str, data: bytes, headers: dict, http_timeout: int) -> dict:
    """`_request` with the CLI's exit-on-error contract (multipart senders)."""
    try:
        return _request(method, data, headers, http_timeout)
    except TelegramError as exc:
        sys.exit(f"error: {exc}")


def _request(method: str, data: bytes, headers: dict, http_timeout: int) -> dict:
    token = require_env("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"telegram {method} failed: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramError(f"telegram {method} unreachable: {exc}")
    if not payload.get("ok"):
        raise TelegramError(f"telegram {method} failed: {payload.get('description')}")
    return payload["result"]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            sys.exit(f"error: {STATE_PATH} is corrupt — inspect/fix it (offset + questions map), "
                     "or delete it to restart from Telegram's current backlog")
    return {"offset": 0, "questions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)  # atomic — a crash mid-write can't corrupt the state file


def _q_ticket(v):
    """Resolve the ticket key from a question entry, accepting either the rich dict
    shape ({ticket, text, asked_at}) or a legacy bare-string entry ("ABC-123")."""
    return v if isinstance(v, str) else v.get("ticket")


def _q_project(v):
    """The Linear Project recorded with a question, or None. Lets a reply route to
    the right repo (multi-repo parent) without a tracker read; None for legacy
    entries and single-repo setups."""
    return None if isinstance(v, str) else v.get("project")


def _q_context(v):
    """The opaque routing context recorded with a PRE-TICKET question (e.g. the
    original `bug:` report awaiting a 'which project?' answer), or None. Lets a
    plain reply resolve back to what it answers when no ticket exists yet."""
    return None if isinstance(v, str) else v.get("context")


def _q_entry(ticket, text, project=None, context=None) -> dict:
    """Build a rich question entry recorded at ask time. `ticket` may be None for a
    pre-ticket disambiguation entry (carried by `context`). `project` tags the
    reply with its repo. asked_at is ISO-8601 UTC with a trailing Z."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    asked_at = now.isoformat().replace("+00:00", "Z")
    entry = {"ticket": (ticket.upper() if ticket else None),
             "text": text or "", "asked_at": asked_at}
    if project:
        entry["project"] = str(project)
    if context:
        entry["context"] = context
    return entry


def edit_or_send(chat_id: str, text: str, old_id):
    """Edit the bot's earlier message `old_id` in place, or post a fresh one.
    Returns (message_id, edited). An unchanged text is a success (Telegram
    answers 400 "message is not modified"); a target Telegram explicitly reports
    as gone/uneditable falls back to a fresh send; ANY other failure (network,
    auth, rate limit, 5xx) propagates — the edit may have gone through, and a
    blind resend could duplicate it."""
    if old_id:
        try:
            api_raw("editMessageText", {"chat_id": chat_id, "message_id": old_id, "text": text})
            return old_id, True
        except TelegramError as exc:
            reason = str(exc).lower()
            if "not modified" in reason:
                return old_id, True
            if not any(s in reason for s in UNEDITABLE_REASONS):
                raise
            # target deleted / too old / not ours → post afresh
    result = api_raw("sendMessage", {"chat_id": chat_id, "text": text})
    return result["message_id"], False


# Telegram's wording when a message can't be edited because of the TARGET (not
# the request): it was deleted, is too old, or isn't the bot's. Only these
# justify posting a fresh message in `edit_or_send`.
UNEDITABLE_REASONS = ("message to edit not found", "message can't be edited",
                      "message_id_invalid", "message identifier is not specified")


def cmd_send(args: argparse.Namespace) -> None:
    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        sys.exit("error: empty message")
    replace = getattr(args, "replace", None)
    if replace:
        # An in-place status line (heartbeat, merge queue) is never a routed
        # question: it carries no ticket/context, so it must not touch the
        # questions map — refuse the combination rather than half-record it.
        if getattr(args, "ticket", None) or getattr(args, "project", None) or getattr(args, "context", None):
            sys.exit("error: --replace cannot be combined with --ticket/--project/--context")
        state = load_state()
        notices = state.setdefault("notices", {})
        try:
            message_id, edited = edit_or_send(chat_id, text, notices.get(replace))
        except TelegramError as exc:
            sys.exit(f"error: {exc}")
        if not edited:
            notices[replace] = message_id
            save_state(state)
        print(json.dumps({"message_id": message_id, "edited": edited}))
        return
    result = api("sendMessage", {"chat_id": chat_id, "text": text})
    message_id = result["message_id"]
    # Record a question entry whenever this message expects a routed reply: a
    # ticket-bound question (--ticket), OR a pre-ticket disambiguation (--context,
    # no ticket yet). --project tags the reply with its repo so the consumer routes
    # without a tracker read.
    if getattr(args, "ticket", None) or getattr(args, "context", None):
        state = load_state()
        state.setdefault("questions", {})[str(message_id)] = _q_entry(
            args.ticket, text, project=getattr(args, "project", None),
            context=getattr(args, "context", None))
        save_state(state)
    print(json.dumps({"message_id": message_id}))


def download_media(msg: dict):
    """Download a photo (or image document) attached to msg; return the local path or None.

    A failed download degrades to a warning — the poll batch must still emit the
    message (with its caption) rather than die on a transient file fetch.
    """
    photo = msg.get("photo")
    doc = msg.get("document") or {}
    if photo:
        file_id = photo[-1]["file_id"]  # PhotoSize list is ordered smallest → largest
    elif str(doc.get("mime_type") or "").startswith("image/"):
        file_id = doc["file_id"]
    else:
        return None
    token = require_env("TELEGRAM_BOT_TOKEN")
    try:
        remote = api("getFile", {"file_id": file_id}).get("file_path") or ""
        ext = Path(remote).suffix or ".jpg"
        dest = STATE_PATH.parent / "media" / f"{msg['message_id']}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.telegram.org/file/bot{token}/{remote}"
        with urllib.request.urlopen(url, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return str(dest)
    except (Exception, SystemExit) as exc:
        print(f"warning: media download failed for message {msg.get('message_id')}: {exc}", file=sys.stderr)
        return None


def resolve_reply(msg: dict, questions: dict):
    """(reply_message_id, matched question entry or None) for a message that replies
    to a recorded question. The caller pops the entry — a reply consumes it."""
    rid = str((msg.get("reply_to_message") or {}).get("message_id"))
    return rid, questions.get(rid)


def reply_context(msg: dict, *, full: bool = False):
    """(quoted text or None, quoted-author-is-bot) for the message this one replies
    to — straight from Telegram's reply_to_message, so no outbound message ever
    needs storing. Text is truncated to REPLY_TEXT_MAX unless `full` (headline
    matching reads the untruncated first line)."""
    target = msg.get("reply_to_message") or {}
    text = target.get("text") or target.get("caption") or ""
    is_bot = bool((target.get("from") or {}).get("is_bot"))
    if text and not full:
        text = text[:REPLY_TEXT_MAX]
    return (text or None), is_bot


def headline_ticket(quoted: str):
    """The ONE ticket a bot headline names in its subject slot, or None. Only the
    first line counts, it must match HEADLINE_RE, and it must contain exactly one
    key — a digest, a two-ticket line, or prose never yields a binding."""
    if not quoted:
        return None
    first = quoted.splitlines()[0]
    m = HEADLINE_RE.match(first)
    if not m or len(ANY_KEY_RE.findall(first)) != 1:
        return None
    return m.group(1).upper()


def match_ticket(msg: dict, questions: dict):
    """Return the issue key (e.g. ABC-123) this message answers, or None — from the
    replied-to question entry, else a leading TEAM-123 prefix in the text, else
    the single key in the replied-to BOT headline (a reply to "✅ ABC-12 — PR
    opened" is about ABC-12). (project/context come from `resolve_reply`; poll
    emits them.)"""
    _rid, entry = resolve_reply(msg, questions)
    if entry is not None:
        return _q_ticket(entry)
    prefix = TICKET_RE.match(msg.get("text") or msg.get("caption") or "")
    if prefix:
        return prefix.group(1).upper()
    quoted, from_bot = reply_context(msg, full=True)
    return headline_ticket(quoted) if from_bot else None


INGRESS_FRESH_S = 300   # a marker older than this means no ingress is serving this state dir


def ingress_marker():
    """The ingress daemon's <state>/ingress.json, or None."""
    try:
        return json.loads((STATE_PATH.parent / "ingress.json").read_text())
    except (OSError, ValueError):
        return None


def ingress_fresh(marker, now=None) -> bool:
    """True when the marker was refreshed within INGRESS_FRESH_S (the daemon rewrites
    it after every long-poll, i.e. every ~20s while healthy)."""
    if not marker:
        return False
    try:
        ts = datetime.datetime.fromisoformat(str(marker.get("updated_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    now = now if now is not None else time.time()
    return 0 <= now - ts.timestamp() < INGRESS_FRESH_S


def token_fp(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def ingress_serving(marker=None) -> bool:
    """Is a live ingress daemon serving THIS bot in this state dir? Fresh marker,
    and — when our token is known — the marker's bot fingerprint matches it (a
    rotated token must poll directly, not wait on a daemon serving the old bot)."""
    marker = ingress_marker() if marker is None else marker
    if not ingress_fresh(marker):
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return not token or marker.get("token_fp") == token_fp(token)


def ingress_mode() -> bool:
    """Spool mode? TICKET_LOOP_INGRESS=1/0 is authoritative (the orchestrator sets
    it per pass from the same check, so a pass never flips mid-run); unset →
    auto: a live daemon serving this bot owns its getUpdates, and a direct poll
    from here would 409 one of the two."""
    v = os.environ.get("TICKET_LOOP_INGRESS", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return ingress_serving()


def read_spool() -> list:
    """Every parsable pending spool record, oldest first: [(path, record)].
    Unparsable files are left alone with a warning — they are the daemon's to
    fix, never silently dropped."""
    d = STATE_PATH.parent / "inbox"
    out = []
    try:
        paths = [p for p in d.iterdir() if p.suffix == ".json" and p.stem.isdigit()]
    except FileNotFoundError:
        return out                       # no spool yet: genuinely nothing pending
    except OSError as exc:               # unreadable ≠ empty: let the caller fail OPEN
        sys.exit(f"error: cannot read the ingress spool {d}: {exc}")
    for path in paths:
        try:
            rec = json.loads(path.read_text())
            if not isinstance(rec.get("message"), dict):
                raise ValueError("no message")
        except (OSError, ValueError, AttributeError):
            print(f"warning: unreadable spool record {path.name} — left for the ingress", file=sys.stderr)
            continue
        out.append((path, rec))
    out.sort(key=lambda pr: (str(pr[1].get("received_at") or ""), int(pr[0].stem)))
    return out


def spooled_records(chat_id: str, records=None) -> list:
    """The pending records that are this group's human messages. Anything else
    (another chat — the tenant's group changed — or a bot post) is neither
    emitted nor consumed: it stays in place with a warning, never silently
    discarded; a human decides."""
    records = read_spool() if records is None else records
    mine = [(p, r) for p, r in records
            if str((r["message"].get("chat") or {}).get("id")) == chat_id
            and not (r["message"].get("from") or {}).get("is_bot")]
    if len(mine) != len(records):
        print(f"warning: {len(records) - len(mine)} spool record(s) for another chat/bot left "
              f"in {STATE_PATH.parent / 'inbox'} — inspect (did the group change?)", file=sys.stderr)
    return mine


def consume_records(paths) -> set:
    """Mark spool records consumed: <id>.json → <id>.done, mtime reset so the
    ingress's 48h GC counts from consumption, not arrival. Returns the paths
    that could NOT be consumed — the caller must not emit those (they would be
    emitted again next poll: double-processing)."""
    failed = set()
    for path in paths:
        done = path.with_suffix(".done")
        try:
            os.replace(path, done)
            os.utime(done, None)
        except OSError as exc:
            print(f"warning: could not consume spool record {path.name}: {exc} — not emitted",
                  file=sys.stderr)
            failed.add(path)
    return failed


def emit_record(msg: dict, questions: dict, media_path=None) -> dict:
    """The poll output line for one human message (shared by both modes)."""
    sender = msg.get("from", {})
    rid, entry = resolve_reply(msg, questions)
    quoted, quoted_from_bot = reply_context(msg)
    if entry is not None:
        # A reply consumes its question entry, and carries the project/context
        # recorded at ask time (routes without a tracker read; a pre-ticket
        # disambiguation reply has ticket=None but a context).
        ticket, project, context = _q_ticket(entry), _q_project(entry), _q_context(entry)
        questions.pop(rid, None)
    else:
        prefix = TICKET_RE.match(msg.get("text") or msg.get("caption") or "")
        if prefix:
            ticket = prefix.group(1).upper()
        else:
            ticket = headline_ticket(reply_context(msg, full=True)[0]) if quoted_from_bot else None
        project = context = None
    return {
        "message_id": msg["message_id"],
        # username is display-level; from_id is the stable identity — record
        # from_id in any audit trail (e.g. mirrored Linear comments).
        "from": sender.get("username") or sender.get("first_name") or "unknown",
        "from_id": sender.get("id"),
        "text": msg.get("text") or (msg.get("caption") or ""),
        "ticket": ticket,
        "project": project,       # Linear Project for repo routing (or None)
        "context": context,       # pre-ticket disambiguation payload (or None)
        "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
        # What the human is replying TO — so a reply to a heartbeat, an alert or
        # a PR post can still be read in context (null when not a reply).
        "reply_to_text": quoted,
        "reply_to_from_bot": quoted_from_bot,
        "media_path": (media_path if media_path and os.path.exists(media_path)
                       else download_media(msg)),
    }


def cmd_poll(args: argparse.Namespace) -> None:
    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    state = load_state()
    questions = state.setdefault("questions", {})
    shared = shared_bot()
    spool = ingress_mode()
    started = time.monotonic()
    emitted, updates = [], []
    # 1. The spool — ALWAYS drained first, in both modes, so records the ingress
    #    wrote before a mode flip (daemon stopped, laptop run) are never stranded.
    everything = read_spool()
    records = spooled_records(chat_id, everything)
    if spool and not records and args.timeout > 0:
        # Long-poll equivalent: wait for the spool to grow, then re-read once.
        deadline = started + args.timeout
        while time.monotonic() < deadline and not records:
            time.sleep(1)
            everything = read_spool()
            records = spooled_records(chat_id, everything)
    # Consume BEFORE emitting: a record whose rename fails is not emitted (it
    # will be next time), so nothing is ever acted on twice.
    failed = consume_records([path for path, _ in records])
    for path, rec in records:
        if path not in failed:
            emitted.append(emit_record(rec["message"], questions, rec.get("media_path")))
    # 2. Direct getUpdates — only when nothing owns this bot's update stream.
    if not spool:
        params = {"timeout": args.timeout, "allowed_updates": '["message"]'}
        if not shared:
            params["offset"] = state.get("offset", 0)
        updates = api("getUpdates", params, http_timeout=args.timeout + 15)
        floor = state.get("offset", 0)
        for update in updates:
            if shared and update["update_id"] < floor:
                continue  # scanned (and skipped or processed) in an earlier no-ack pass
            state["offset"] = update["update_id"] + 1
            msg = update.get("message")
            if not msg or str(msg.get("chat", {}).get("id")) != chat_id:
                continue
            if (msg.get("from") or {}).get("is_bot"):
                continue
            emitted.append(emit_record(msg, questions))
    # Crash-recovery affordance: the batch is persisted before the offset commit /
    # spool consumption, so a consumer that dies mid-processing can re-read what
    # would otherwise have been swallowed.
    batch_path = STATE_PATH.parent / "last-batch.json"
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    batch_path.write_text(json.dumps(emitted, ensure_ascii=False, indent=2) + "\n")
    save_state(state)
    if not spool and shared and updates and not emitted and args.timeout > 0:
        # In no-ack mode a permanently-pending foreign update makes getUpdates
        # return instantly, so a caller's poll loop would spin; sleep out the
        # remainder of the requested long-poll window instead.
        remaining = args.timeout - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
    for line in emitted:
        print(json.dumps(line, ensure_ascii=False))


def cmd_peek(_args: argparse.Namespace) -> None:
    """Read-only look at pending updates WITHOUT consuming them: getUpdates at the
    stored offset, timeout 0, and NO state write — the offset is untouched, so the
    next real `poll` still sees every message. Prints the count of human messages
    in the agent group. Used by the orchestrator pre-check to catch a human poke
    ("stop", "urgent: X") on an otherwise idle project; safe by construction ONLY
    while a single consumer drives this bot (the orchestrator, after any per-project
    cron is decommissioned). In shared-bot mode the offset is never sent at all —
    the local floor filters instead (module docstring)."""
    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    if ingress_mode():
        print(len(spooled_records(chat_id)))   # local, free, and never 409s the daemon
        return
    state = load_state()
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if not shared_bot():
        params["offset"] = state.get("offset", 0)
    updates = api("getUpdates", params, http_timeout=15)
    floor = state.get("offset", 0)
    count = 0
    for update in updates:
        if shared_bot() and update["update_id"] < floor:
            continue
        msg = update.get("message")
        if not msg or str(msg.get("chat", {}).get("id")) != chat_id:
            continue
        if (msg.get("from") or {}).get("is_bot"):
            continue
        count += 1
    print(count)


def cmd_send_photo(args: argparse.Namespace) -> None:
    import uuid

    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    path = Path(args.path)
    if not path.is_file():
        sys.exit(f"error: {path} is not a file")
    boundary = uuid.uuid4().hex
    parts = []
    fields = {"chat_id": chat_id}
    if args.caption:
        fields["caption"] = args.caption
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    result = request(
        "sendPhoto",
        b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        http_timeout=60,
    )
    message_id = result["message_id"]
    if args.ticket:
        state = load_state()
        state.setdefault("questions", {})[str(message_id)] = _q_entry(args.ticket, args.caption or "")
        save_state(state)
    print(json.dumps({"message_id": message_id}))


def cmd_send_document(args: argparse.Namespace) -> None:
    """Send a file (e.g. a PDF report) to the agent group via sendDocument."""
    import uuid

    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    path = Path(args.path)
    if not path.is_file():
        sys.exit(f"error: {path} is not a file")
    boundary = uuid.uuid4().hex
    parts = []
    fields = {"chat_id": chat_id}
    if args.caption:
        fields["caption"] = args.caption
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    result = request(
        "sendDocument",
        b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        http_timeout=120,
    )
    message_id = result["message_id"]
    if args.ticket:
        state = load_state()
        state.setdefault("questions", {})[str(message_id)] = _q_entry(args.ticket, args.caption or "")
        save_state(state)
    print(json.dumps({"message_id": message_id}))


def _parse_asked_at(asked_at):
    """Parse an ISO-8601 asked_at (trailing Z or offset) into an aware datetime, or
    None if absent/unparseable (legacy entries)."""
    if not asked_at or not isinstance(asked_at, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(asked_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _format_age(asked_at, now):
    """Human age like '3d ago' / '5h ago' / '2m ago' / 'just now'; '—' when unknown."""
    dt = _parse_asked_at(asked_at)
    if dt is None:
        return "—"
    secs = int((now - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _first_lines(text, n=2, width=72):
    """First n non-empty lines of text, each truncated to width; [] when no text."""
    if not text:
        return []
    out = []
    for line in str(text).splitlines():
        line = line.rstrip()
        if not line:
            continue
        if len(line) > width:
            line = line[: width - 1] + "…"
        out.append(line)
        if len(out) >= n:
            break
    return out


def _sorted_questions(questions):
    """(message_id, entry) pairs oldest-first by asked_at; entries with no asked_at
    (legacy / most likely stale) sort FIRST."""
    def key(item):
        entry = item[1]
        asked_at = entry.get("asked_at") if isinstance(entry, dict) else None
        dt = _parse_asked_at(asked_at)
        # missing age sorts first: (0, "") before (1, iso-string)
        return (1, dt.isoformat()) if dt is not None else (0, "")
    return sorted(questions.items(), key=key)


def render_questions(questions, now):
    """Human list of open questions, oldest-first. Returns the full text block."""
    if not questions:
        return "no open questions"
    blocks = []
    for mid, entry in _sorted_questions(questions):
        if isinstance(entry, dict):
            ticket = entry.get("ticket") or "—"
            age = _format_age(entry.get("asked_at"), now)
            lines = _first_lines(entry.get("text"))
        else:  # legacy bare string
            ticket = entry or "—"
            age = "—"
            lines = []
        block = [f"{ticket}  msg {mid}  asked {age}"]
        block.extend(f"    {line}" for line in (lines or ["—"]))
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def questions_json(questions):
    """Raw entry list for the agent:
    [{message_id, ticket, project, context, text, asked_at}, …]."""
    out = []
    for mid, entry in _sorted_questions(questions):
        if isinstance(entry, dict):
            out.append({
                "message_id": mid,
                "ticket": entry.get("ticket"),
                "project": entry.get("project"),
                "context": entry.get("context"),
                "text": entry.get("text"),
                "asked_at": entry.get("asked_at"),
            })
        else:
            out.append({"message_id": mid, "ticket": entry, "project": None,
                        "context": None, "text": None, "asked_at": None})
    return out


def clear_questions(questions, arg):
    """Drop entries matching arg from `questions` (mutates it). If arg is all digits
    it targets that one message_id; otherwise it's a ticket key (case-insensitive)
    and every entry for that ticket is dropped. Returns the cleared (message_id,
    entry) pairs in list order (empty when nothing matched)."""
    if arg.isdigit():
        matches = [(mid, e) for mid, e in questions.items() if mid == arg]
    else:
        want = arg.upper()
        matches = [(mid, e) for mid, e in questions.items()
                   if (_q_ticket(e) or "").upper() == want]
    for mid, _e in matches:
        questions.pop(mid, None)
    return matches


def cmd_questions(args: argparse.Namespace) -> None:
    state = load_state()
    questions = state.get("questions", {}) or {}
    if args.clear:
        cleared = clear_questions(questions, args.clear)
        if not cleared:
            print(f"no open question matches '{args.clear}' "
                  "(pass a message_id or a ticket key)", file=sys.stderr)
            sys.exit(1)
        state["questions"] = questions
        save_state(state)  # atomic; never touches `offset`, never calls Telegram
        print(f"cleared {len(cleared)} question(s):")
        now = datetime.datetime.now(datetime.timezone.utc)
        print(render_questions(dict(cleared), now))
        return
    if args.json:
        print(json.dumps(questions_json(questions), ensure_ascii=False, indent=2))
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    print(render_questions(questions, now))


def cmd_ingress(args: argparse.Namespace) -> None:
    """Is this state dir ingress-served? Reads only local files (no secrets, no
    network). `--mode` prints just `spool` or `direct` for shell callers."""
    marker = ingress_marker()
    mode = "spool" if ingress_mode() else "direct"
    if args.mode:
        print(mode)
        return
    chat_id = os.environ.get("AGENT_TELEGRAM_CHAT_ID", "").strip()
    pending = len(spooled_records(chat_id)) if chat_id else None
    print(json.dumps({"mode": mode, "fresh": ingress_fresh(marker), "serving": ingress_serving(marker),
                      "marker": marker, "pending": pending}, ensure_ascii=False))


def cmd_react(args: argparse.Namespace) -> None:
    """Set the bot's reaction on a group message (setMessageReaction), or clear it
    with --clear. Idempotent — re-setting the same emoji is a no-op server-side.
    The ingress reacts 👀 the second a message is spooled; the agent flips it to 👍
    once it has acted on the message, so the group can see both stages."""
    chat_id = require_env("AGENT_TELEGRAM_CHAT_ID")
    reaction = [] if args.clear else [{"type": "emoji", "emoji": args.emoji}]
    api("setMessageReaction", {"chat_id": chat_id, "message_id": args.message_id,
                               "reaction": json.dumps(reaction, ensure_ascii=False)})
    print(json.dumps({"message_id": args.message_id,
                      "reaction": None if args.clear else args.emoji}, ensure_ascii=False))


def cmd_discover(_args: argparse.Namespace) -> None:
    updates = api("getUpdates", {"timeout": 0}, http_timeout=15)
    seen = {}
    for update in updates:
        chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat")
        if chat:
            seen[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                "title": chat.get("title") or chat.get("username") or ""}
    if not seen:
        print("no recent updates — send a message in the target group (with the bot added) and rerun",
              file=sys.stderr)
    for chat in seen.values():
        print(json.dumps(chat, ensure_ascii=False))


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="send a message to the agent group")
    p_send.add_argument("--ticket", help="issue key this question belongs to (records reply matching)")
    p_send.add_argument("--project", help="Linear Project to tag the reply with (multi-repo routing)")
    p_send.add_argument("--context", help="opaque payload for a PRE-TICKET question "
                        "(e.g. a 'which project?' clarifier) so a plain reply routes back")
    p_send.add_argument("--replace", metavar="KEY",
                        help="edit the message last sent under KEY in place (state.json notices) "
                             "instead of posting a new one; falls back to a fresh send when the "
                             "old message can't be edited. For status lines, never questions.")
    p_send.add_argument("text", nargs="*", help="message text (or pipe via stdin)")
    p_send.set_defaults(func=cmd_send)

    p_poll = sub.add_parser("poll", help="fetch new messages from the agent group")
    p_poll.add_argument("--timeout", type=int, default=25, help="long-poll seconds (0 = instant)")
    p_poll.set_defaults(func=cmd_poll)

    p_peek = sub.add_parser("peek", help="count pending group messages WITHOUT consuming the offset")
    p_peek.set_defaults(func=cmd_peek)

    p_ing = sub.add_parser("ingress", help="report whether this state dir is served by the Telegram ingress")
    p_ing.add_argument("--mode", action="store_true", help="print only `spool` or `direct`")
    p_ing.set_defaults(func=cmd_ingress)

    p_photo = sub.add_parser("send-photo", help="send an image file to the agent group")
    p_photo.add_argument("--ticket", help="issue key this image belongs to (records reply matching)")
    p_photo.add_argument("--caption", help="caption to send with the image")
    p_photo.add_argument("path", help="path to the image file")
    p_photo.set_defaults(func=cmd_send_photo)

    p_doc = sub.add_parser("send-document", help="send a file (e.g. a PDF) to the agent group")
    p_doc.add_argument("--ticket", help="issue key this document belongs to (records reply matching)")
    p_doc.add_argument("--caption", help="caption to send with the document")
    p_doc.add_argument("path", help="path to the file to send")
    p_doc.set_defaults(func=cmd_send_document)

    p_react = sub.add_parser("react", help="set (or clear) the bot's emoji reaction on a group message")
    p_react.add_argument("--clear", action="store_true", help="remove the bot's reaction instead")
    p_react.add_argument("message_id", type=int, help="the group message to react to")
    p_react.add_argument("emoji", nargs="?", default="👍",
                         help="reaction emoji from Telegram's fixed set (default 👍; 👀 = seen)")
    p_react.set_defaults(func=cmd_react)

    p_q = sub.add_parser("questions", help="list / clear open clarifying questions (no Telegram, no secrets)")
    p_q.add_argument("--json", action="store_true", help="emit the raw entry list for the agent")
    p_q.add_argument("--clear", metavar="ID|TICKET",
                     help="drop one message_id (all digits) or every entry for a ticket key")
    p_q.set_defaults(func=cmd_questions)

    p_disc = sub.add_parser("discover", help="print chats the bot has recently seen")
    p_disc.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
