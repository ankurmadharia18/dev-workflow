#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Telegram ingress for the ticket-loop orchestrator: message INTAKE, separated
from the WORK (the Claude pass).

One always-on process (a child of orchestrator.sh, restarted by its supervisor)
is the SOLE getUpdates consumer for every bot token the roster names. The moment
a human posts in a tenant's group it:

  1. spools the raw update to  <state_dir>/inbox/<update_id>.json  (tmp + atomic
     rename; media downloaded next to it under <state_dir>/media/),
  2. reacts 👀 on the message — the visible "received" ack,
  3. signals a wake:  <orch-state>/run-now.d/<tenant>  (one slot per tenant).

The pass (`telegram.py poll` with TICKET_LOOP_INGRESS=1) reads the spool and never
touches getUpdates, so a message sent mid-build is acked in a second and drained
at the pass's next re-drain point instead of waiting for the next scheduled pass.

Invariants (the reasons this file looks the way it does):

* Never ack what isn't on disk. Telegram's getUpdates(offset=N) confirms every
  update below N bot-wide, forever. The cursor only advances past updates whose
  spool file has been renamed into place; the first spool failure stops the batch
  and the next poll re-delivers from the last persisted update.
* Redelivery is deduped by the spool itself: a file for that update_id (pending
  `.json` or consumed `.done`) means "already have it". `.done` files are kept 48h
  (Telegram retains undelivered updates 24h) and then garbage-collected with
  their media.
* No high-water-mark arithmetic on update_id: Telegram may pick the next id
  randomly after a week idle, so "id >= offset" is not a consumption test. The
  pass consumes by renaming `.json` → `.done`; presence is the state. For the
  same reason an `offset` is sent to getUpdates ONLY when the previous poll
  returned updates (the ack of exactly those): after an empty poll nothing is
  unconfirmed, and omitting the offset lets a randomly-lower next id through.
* A tenant's env file (token, chat id) and the roster are watched by mtime; a
  change exits 0 and the supervisor restarts with the new routes. The liveness
  marker carries the bot fingerprint so a pass whose token no longer matches it
  falls back to direct polling instead of waiting on a daemon serving another bot.
* Errors of any kind for ~10 minutes straight (network, auth, disk) exit the
  process — the supervisor restarts it and pages ops; a silent daemon must not
  outlive Telegram's 24h retention.
* The cursor is bound to the bot: ingress.json carries a token fingerprint; a
  rotated token starts from Telegram's pending queue, never from a foreign offset.
* Every roster entry with a chat id is routed, enabled or not: a paused tenant's
  messages queue in its spool instead of being acked and dropped. A chat that two
  tenants claim on one token is refused (loudly, on every start) — the token's
  worker is not started, so nothing is acked and nothing is lost inside 24h.
* One worker thread per token; any worker failure exits the whole process, so
  the supervisor's restart is the health check. Five consecutive 409 conflicts
  (another consumer on the token — a stale container from an interrupted deploy)
  also exit, and the supervisor pages ops.
* Reactions run on a side thread per token — a slow setMessageReaction must not
  delay the next getUpdates. Service messages (joins, pins) are acked, never
  spooled, never reacted to.
* Roster edits: the file's mtime is watched; a change exits 0 and the supervisor
  restarts with the new roster (same no-restart onboarding the scheduler has).

Stdlib + PyYAML. Usage:
  telegram-ingress.py --roster /home/agent/roster.yml \
      --run-now-dir /home/agent/orch/run-now.d [--poll-timeout 20]
"""

import argparse
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

POLL_TIMEOUT_S = 20          # long-poll length; the marker is refreshed this often
ERROR_BACKOFF_S = 30         # transport / non-ok backoff
CONFLICT_EXIT_AFTER = 5      # consecutive 409s → exit: another consumer owns the token
FAILURES_EXIT_AFTER = 20     # consecutive errors of any kind (≈10 min) → exit, page ops
REACT_TIMEOUT_S = 10
MEDIA_TIMEOUT_S = 60
MEDIA_MAX_BYTES = 20 * 1024 * 1024   # Bot API getFile ceiling
GC_DONE_AFTER_S = 48 * 3600
GC_EVERY_S = 600
ROSTER_RECHECK_S = 60
SUPERVISE_TICK_S = 5
LOG_SUPPRESS_S = 3600        # per-chat repeat-warning suppression
SEEN_EMOJI = "👀"

_ENV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def log(msg):
    print(f"[telegram-ingress] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def token_fp(token):
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# ── env files / roster → routes ──────────────────────────────────────────────

def read_env_keys(env_file, keys):
    """{key: value} for the requested keys of a KEY=VALUE env file (quotes
    stripped, comments ignored). Missing file → {}. Never raises."""
    out = {}
    try:
        for line in Path(env_file).read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = _ENV_RE.match(line)
            if m and m.group(1) in keys:
                out[m.group(1)] = m.group(2).strip().strip("'\"")
    except OSError:
        pass
    return {k: v for k, v in out.items() if v}


def read_bot_token(env_file):
    return read_env_keys(env_file, {"TELEGRAM_BOT_TOKEN"}).get("TELEGRAM_BOT_TOKEN")


def load_routes(roster_path, default_token=None):
    """(routes, problems, watched_files). routes = {token: {chat_id: {"name",
    "state_dir"}}} over
    EVERY roster entry (enabled or not) that resolves a token (its own, else the
    orchestrator's shared default) and a chat id. problems = human-readable
    config errors; a token with an ambiguous chat (two tenants) is dropped from
    routes entirely — nothing on it is acked until the roster is fixed."""
    if yaml is None:
        sys.exit("telegram-ingress: PyYAML required (run via uv, or python3-yaml)")
    raw = yaml.safe_load(Path(roster_path).read_text()) or {}
    routes, problems, seen_state = {}, [], {}
    watched = [str(roster_path)]
    for entry in raw.get("projects") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"])
        env_file, state_dir = entry.get("env_file"), entry.get("state_dir")
        if not env_file or not state_dir:
            problems.append(f"{name}: env_file/state_dir missing — not routed")
            continue
        watched.append(str(env_file))
        env = read_env_keys(env_file, {"TELEGRAM_BOT_TOKEN", "AGENT_TELEGRAM_CHAT_ID"})
        token = env.get("TELEGRAM_BOT_TOKEN") or default_token
        chat = env.get("AGENT_TELEGRAM_CHAT_ID")
        if not token or not chat:
            problems.append(f"{name}: no bot token / chat id in {env_file} — not routed "
                            "(its passes poll Telegram directly)")
            continue
        if state_dir in seen_state:
            problems.append(f"{name}: state_dir {state_dir} already used by "
                            f"{seen_state[state_dir]} — not routed")
            continue
        seen_state[state_dir] = name
        chats = routes.setdefault(token, {})
        if chat in chats:
            prev = chats[chat]
            chats[chat] = {"name": None, "state_dir": None, "conflict": [prev.get("name") or "?", name]} \
                if not prev.get("conflict") else {**prev, "conflict": prev["conflict"] + [name]}
            continue
        chats[chat] = {"name": name, "state_dir": str(state_dir)}
    for token in list(routes):
        conflicts = [(c, r["conflict"]) for c, r in routes[token].items() if r.get("conflict")]
        if conflicts:
            for chat, names in conflicts:
                problems.append(f"chat {chat} claimed by {', '.join(names)} on one bot "
                                f"({token_fp(token)}) — REFUSING that bot: nothing on it "
                                "is consumed until the roster is fixed")
            del routes[token]
    return routes, problems, watched


# ── per-tenant spool files ───────────────────────────────────────────────────

def inbox_dir(state_dir):
    return Path(state_dir) / "inbox"


def spool_record(state_dir, update_id, record):
    """Persist one update as <inbox>/<update_id>.json via tmp + atomic rename.
    Returns "new", or "dup" when a pending or consumed file already exists
    (redelivery after a crash between spool and ack). Raises OSError on failure
    — the caller must NOT advance the cursor past this update."""
    d = inbox_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    final, done = d / f"{update_id}.json", d / f"{update_id}.done"
    if final.exists() or done.exists():
        return "dup"
    tmp = d / f"{update_id}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    try:                       # make the directory entry itself survive a power loss
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass                   # some filesystems refuse dir fsync; the data is synced
    return "new"


def pending_records(state_dir):
    """[(update_id, chat_id, message_id)] of pending records — for re-reacting
    and re-waking after a restart. Unreadable files are skipped."""
    out = []
    try:
        for p in inbox_dir(state_dir).iterdir():
            if p.suffix != ".json" or not p.stem.isdigit():
                continue
            try:
                rec = json.loads(p.read_text())
                out.append((int(p.stem), rec.get("chat_id"), (rec.get("message") or {}).get("message_id")))
            except (OSError, ValueError, AttributeError):
                continue
    except OSError:
        pass
    return out


def pending_count(state_dir):
    try:
        return sum(1 for p in inbox_dir(state_dir).iterdir()
                   if p.suffix == ".json" and p.stem.isdigit())
    except OSError:
        return 0


def write_marker(state_dir, tenant, token, cursor, extra=None):
    """<state_dir>/ingress.json — liveness + cursor, bound to the bot. The pass
    (and the orchestrator) treat a marker younger than a few minutes as "this
    tenant is ingress-served"; the cursor is only trusted for a matching token."""
    p = Path(state_dir) / "ingress.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"tenant": tenant, "token_fp": token_fp(token), "tg_offset": cursor,
            "updated_at": now_iso(), "pid": os.getpid()}
    if extra:
        body.update(extra)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body) + "\n")
    os.replace(tmp, p)


def read_marker(state_dir):
    try:
        return json.loads((Path(state_dir) / "ingress.json").read_text())
    except (OSError, ValueError):
        return None


def load_cursor(token, tenants):
    """Highest tg_offset among the token's tenants' markers whose fingerprint
    matches this token; 0 (Telegram's pending queue) for a new/rotated token."""
    fp, best = token_fp(token), 0
    for t in tenants:
        m = read_marker(t["state_dir"]) or {}
        if m.get("token_fp") == fp:
            try:
                best = max(best, int(m.get("tg_offset") or 0))
            except (TypeError, ValueError):
                pass
    return best


def gc_tenant(state_dir, now=None, done_after_s=GC_DONE_AFTER_S):
    """Delete consumed (.done) records older than done_after_s, plus the media
    each one downloaded (only paths under this tenant's media dir). Returns the
    number of records removed. Pending .json files are never touched."""
    now = now or time.time()
    d, removed = inbox_dir(state_dir), 0
    media_root = (Path(state_dir) / "media").resolve()
    try:
        entries = list(d.iterdir())
    except OSError:
        return 0
    for p in entries:
        if p.suffix != ".done":
            continue
        try:
            if now - p.stat().st_mtime < done_after_s:
                continue
            media = None
            try:
                media = (json.loads(p.read_text()) or {}).get("media_path")
            except (OSError, ValueError):
                pass
            if media:
                mp = Path(media)
                try:
                    if mp.resolve().is_relative_to(media_root):
                        mp.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ── wake signal ──────────────────────────────────────────────────────────────

def signal_wake(run_now_dir, name):
    """Create <run_now_dir>/<name> (content "inbox") atomically; False when a
    wake is already pending for that tenant. One slot per tenant, so a burst
    coalesces and one tenant's wake never displaces another's."""
    d = Path(run_now_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write("inbox\n")
    return True


# ── Telegram Bot API ─────────────────────────────────────────────────────────

class ApiError(RuntimeError):
    def __init__(self, method, code, text):
        super().__init__(f"{method}: HTTP {code} {text[:160]}")
        self.code = code


def api(token, method, params, timeout):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiError(method, exc.code, exc.read().decode(errors="replace")) from None
    if not payload.get("ok"):
        raise ApiError(method, payload.get("error_code", 0), str(payload.get("description")))
    return payload.get("result")


def get_updates(token, cursor, timeout_s, ack=True):
    """One long poll. `offset` (which CONFIRMS every update below it, bot-wide)
    is sent only when `ack` — i.e. the previous poll returned updates that are
    now on disk. After an empty poll nothing is unconfirmed, and omitting the
    offset is what lets a randomly re-seeded (possibly lower) next update_id
    through instead of being confirmed unseen."""
    params = {"timeout": timeout_s, "allowed_updates": '["message"]'}
    if ack and cursor > 0:
        params["offset"] = cursor + 1
    return api(token, "getUpdates", params, timeout_s + 15) or []


def set_reaction(token, chat_id, message_id, emoji=SEEN_EMOJI):
    api(token, "setMessageReaction",
        {"chat_id": chat_id, "message_id": message_id,
         "reaction": json.dumps([{"type": "emoji", "emoji": emoji}], ensure_ascii=False)},
        REACT_TIMEOUT_S)


def media_ref(msg):
    """(file_id, declared size) of a photo / image document, else (None, None)."""
    photo = msg.get("photo")
    doc = msg.get("document") or {}
    if photo:
        return photo[-1].get("file_id"), photo[-1].get("file_size")
    if str(doc.get("mime_type") or "").startswith("image/"):
        return doc.get("file_id"), doc.get("file_size")
    return None, None


def download_media(token, msg, state_dir):
    """Fetch the message's image into <state_dir>/media/<message_id><ext> (tmp +
    rename). Returns (local path or None, file_id or None). Downloading at
    receipt is about durability and decoupling (the pass may run hours later
    and needs no network to read the spool); failures degrade to a spool record
    without media — the pass retries from file_id."""
    file_id, size = media_ref(msg)
    if not file_id:
        return None, None
    if size and size > MEDIA_MAX_BYTES:
        return None, file_id
    try:
        remote = (api(token, "getFile", {"file_id": file_id}, 30) or {}).get("file_path") or ""
        ext = Path(remote).suffix or ".jpg"
        dest = Path(state_dir) / "media" / f"{msg['message_id']}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        url = f"https://api.telegram.org/file/bot{token}/{remote}"
        with urllib.request.urlopen(url, timeout=MEDIA_TIMEOUT_S) as resp, open(tmp, "wb") as fh:
            fh.write(resp.read())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        return str(dest), file_id
    except (ApiError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        log(f"media download failed for message {msg.get('message_id')}: {exc}")
        return None, file_id


# ── classification ───────────────────────────────────────────────────────────

CONTENT_KEYS = ("text", "caption", "photo", "document", "sticker", "animation",
                "audio", "voice", "video", "video_note", "poll", "contact",
                "location", "venue", "dice", "story", "game", "invoice")


def human_content(msg):
    """True for a message worth spooling: from a human and carrying any content
    the pass could act on (text, media, sticker, voice, poll, location …) —
    exactly what direct-mode `poll` used to emit. Service messages (joins,
    pins, title changes, forum topics) and bot posts are acked and dropped."""
    if not isinstance(msg, dict) or (msg.get("from") or {}).get("is_bot"):
        return False
    return any(msg.get(k) for k in CONTENT_KEYS)


# ── the per-token worker ─────────────────────────────────────────────────────

class TokenWorker(threading.Thread):
    def __init__(self, token, chats, run_now_dir, poll_timeout, stop):
        super().__init__(daemon=True, name=f"tg-{token_fp(token)}")
        self.token, self.chats, self.run_now_dir = token, chats, run_now_dir
        self.poll_timeout, self.stop = poll_timeout, stop
        self.tenants = list(chats.values())
        self.cursor = load_cursor(token, self.tenants)
        self.failed = None
        self.conflicts = 0
        self.failures = 0            # consecutive errors of any kind
        self.ack = self.cursor > 0   # the marker's cursor is spooled but maybe unconfirmed
        self.reactions = queue.Queue()
        self._warned = {}            # key → monotonic time of the last warning
        self._last_gc = 0.0

    # rate-limited warnings (one per key per hour)
    def warn(self, key, msg):
        now = time.monotonic()
        if now - self._warned.get(key, -LOG_SUPPRESS_S) >= LOG_SUPPRESS_S:
            self._warned[key] = now
            log(msg)

    def names(self):
        return " ".join(t["name"] for t in self.tenants)

    def reactor(self):
        """Side thread: 👀 each spooled message; never blocks intake. An
        unexpected failure here is a worker failure (restart), not a silent
        thread death that leaves intake alive with reactions gone."""
        try:
            while not self.stop.is_set():
                try:
                    chat_id, message_id = self.reactions.get(timeout=1)
                except queue.Empty:
                    continue
                if chat_id is None or message_id is None:
                    continue
                try:
                    set_reaction(self.token, chat_id, message_id)
                except (ApiError, urllib.error.URLError, OSError) as exc:
                    self.warn(f"react:{chat_id}", f"reaction failed in chat {chat_id}: {exc} "
                              "(group may restrict reactions; intake unaffected)")
        except BaseException:          # noqa: BLE001
            self.failed = traceback.format_exc()
            log(f"reactor {self.name} died:\n{self.failed}")

    def write_markers(self, extra=None):
        """A marker that cannot be written is fatal: the pass would read a stale
        marker, decide "direct", and become a second getUpdates consumer."""
        for t in self.tenants:
            write_marker(t["state_dir"], t["name"], self.token, self.cursor, extra)

    def resume_pending(self):
        """Startup / restart: anything already spooled deserves a wake, and a
        fresh 👀 (idempotent server-side — a crash may have lost the queued one)."""
        for t in self.tenants:
            pend = pending_records(t["state_dir"])
            if not pend:
                continue
            for _uid, chat_id, message_id in pend:
                self.reactions.put((chat_id, message_id))
            if signal_wake(self.run_now_dir, t["name"]):
                log(f"{t['name']}: {len(pend)} spooled message(s) pending at start — wake signalled")

    def wake_pending(self):   # back-compat name
        self.resume_pending()

    def fail_if_persistent(self, what):
        self.failures += 1
        if self.failures >= FAILURES_EXIT_AFTER:
            raise RuntimeError(f"{self.failures} consecutive failures ({what}) — exiting for a "
                               "supervised restart + ops page")

    def process(self, updates):
        """Spool a batch in id order. The cursor advances per update ONLY after
        that update is persisted (or classified as ack-and-drop); the first
        spool failure stops the batch so the next poll re-delivers from there."""
        for u in sorted((u for u in updates if isinstance(u, dict)),
                        key=lambda u: u.get("update_id", 0)):
            uid = u.get("update_id")
            if not isinstance(uid, int):
                continue
            msg = u.get("message")
            if human_content(msg):
                chat = str((msg.get("chat") or {}).get("id"))
                route = self.chats.get(chat)
                if route is None:
                    self.warn(f"unrouted:{chat}", f"message in unrouted chat {chat} "
                              f"({(msg.get('chat') or {}).get('title') or '?'}) — acked and "
                              "dropped; add the group to a roster tenant to route it")
                else:
                    media_path, file_id = download_media(self.token, msg, route["state_dir"])
                    rec = {"update_id": uid, "received_at": now_iso(), "tenant": route["name"],
                           "chat_id": chat, "message": msg,
                           "media_path": media_path, "file_id": file_id}
                    status = spool_record(route["state_dir"], uid, rec)   # OSError → stop batch
                    if status == "new":
                        self.reactions.put((chat, msg.get("message_id")))
                        if signal_wake(self.run_now_dir, route["name"]):
                            log(f"{route['name']}: message {msg.get('message_id')} spooled "
                                f"(update {uid}) — wake signalled")
                        else:
                            log(f"{route['name']}: message {msg.get('message_id')} spooled "
                                f"(update {uid}) — wake already pending")
            self.cursor = max(self.cursor, uid)

    def gc(self):
        if time.monotonic() - self._last_gc < GC_EVERY_S:
            return
        self._last_gc = time.monotonic()
        for t in self.tenants:
            n = gc_tenant(t["state_dir"])
            if n:
                log(f"{t['name']}: gc removed {n} consumed record(s)")

    def run(self):
        try:
            self.loop()
        except BaseException:              # noqa: BLE001 — any worker death is fatal
            self.failed = traceback.format_exc()
            log(f"worker {self.name} ({self.names()}) died:\n{self.failed}")

    def loop(self):
        threading.Thread(target=self.reactor, daemon=True, name=self.name + "-react").start()
        log(f"{self.names()}: consuming bot {token_fp(self.token)} from cursor {self.cursor}")
        self.write_markers()
        self.resume_pending()
        while not self.stop.is_set():
            try:
                updates = get_updates(self.token, self.cursor, self.poll_timeout, ack=self.ack)
            except ApiError as exc:
                if exc.code == 409:
                    self.conflicts += 1
                    log(f"{self.names()}: getUpdates 409 — ANOTHER CONSUMER holds bot "
                        f"{token_fp(self.token)} ({self.conflicts}/{CONFLICT_EXIT_AFTER})")
                    if self.conflicts >= CONFLICT_EXIT_AFTER:
                        raise RuntimeError("persistent 409 conflict: a second getUpdates "
                                           "consumer (stale container? webhook?) owns this bot")
                else:
                    log(f"{self.names()}: {exc} — backing off {ERROR_BACKOFF_S}s")
                self.write_markers({"error": str(exc)})
                self.fail_if_persistent(str(exc))
                self.stop.wait(ERROR_BACKOFF_S)
                continue
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"{self.names()}: poll error ({exc}) — backing off {ERROR_BACKOFF_S}s")
                self.write_markers({"error": str(exc)})
                self.fail_if_persistent(str(exc))
                self.stop.wait(ERROR_BACKOFF_S)
                continue
            self.conflicts = 0
            try:
                self.process(updates)
            except OSError as exc:
                log(f"{self.names()}: spool write failed ({exc}) — cursor held at "
                    f"{self.cursor}, backing off {ERROR_BACKOFF_S}s")
                self.ack = True        # re-poll WITH offset: re-deliver from the held cursor
                self.write_markers({"error": f"spool: {exc}"})
                self.fail_if_persistent(f"spool: {exc}")
                self.stop.wait(ERROR_BACKOFF_S)
                continue
            self.failures = 0
            self.ack = bool(updates)   # next poll confirms exactly this batch, or nothing
            self.write_markers()
            self.gc()


# ── main / supervision ───────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--run-now-dir", required=True)
    ap.add_argument("--poll-timeout", type=int, default=POLL_TIMEOUT_S)
    ap.add_argument("--ready-file", default=None,
                    help="touched once every worker has written its markers (the "
                         "orchestrator waits for it before its first turn)")
    args = ap.parse_args(argv)

    routes, problems, watched = load_routes(args.roster, os.environ.get("DEFAULT_TELEGRAM_BOT_TOKEN"))
    for p in problems:
        log(f"config: {p}")
    stop = threading.Event()
    workers = [TokenWorker(tok, chats, args.run_now_dir, args.poll_timeout, stop)
               for tok, chats in routes.items()]
    if workers:
        log("routing: " + "; ".join(
            f"bot {token_fp(w.token)} → {', '.join(t['name'] for t in w.tenants)}" for w in workers))
    else:
        log("no routable tenants — idling until the roster changes")
    for w in workers:
        w.start()
    # Readiness: every worker writes its markers first thing; wait for that
    # (bounded) before telling the orchestrator to trust the markers.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(
            not w.failed and w.is_alive() and not all(read_marker(t["state_dir"]) for t in w.tenants)
            for w in workers):
        time.sleep(0.2)
    if args.ready_file:
        try:
            Path(args.ready_file).write_text(now_iso() + "\n")
        except OSError as exc:
            log(f"cannot write ready file {args.ready_file}: {exc}")

    def mtimes():
        out = []
        for f in watched:
            try:
                out.append(os.stat(f).st_mtime)
            except OSError:
                out.append(None)
        return out
    seen, last_check = mtimes(), time.monotonic()
    try:
        while True:
            time.sleep(SUPERVISE_TICK_S)
            dead = [w for w in workers if w.failed or not w.is_alive()]
            if dead:
                log("worker(s) down: " + ", ".join(w.names() for w in dead) + " — exiting for a restart")
                stop.set()
                return 1
            if time.monotonic() - last_check >= ROSTER_RECHECK_S:
                last_check = time.monotonic()
                if mtimes() != seen:
                    log("roster / tenant env changed — exiting so the supervisor restarts with the new routes")
                    stop.set()
                    return 0
    except KeyboardInterrupt:
        stop.set()
        return 0


if __name__ == "__main__":
    sys.exit(main())
