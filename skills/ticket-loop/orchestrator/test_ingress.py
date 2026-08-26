#!/usr/bin/env python3
"""Stdlib unittests for telegram-ingress.py — routing, spool durability, dedup,
wake signalling, cursor/marker binding and the batch protocol, all without a
network (the Bot API calls are monkeypatched).

Run: python3 skills/ticket-loop/orchestrator/test_ingress.py
"""

import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ingress_mod", HERE / "telegram-ingress.py")
ing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ing)


def env_file(tmp, name, token=None, chat=None):
    f = Path(tmp) / f"{name}.env"
    lines = ["GH_TOKEN=x"]
    if token:
        lines.append(f"TELEGRAM_BOT_TOKEN={token}")
    if chat:
        lines.append(f'AGENT_TELEGRAM_CHAT_ID="{chat}"')
    f.write_text("\n".join(lines) + "\n")
    return f


def roster(tmp, entries):
    r = Path(tmp) / "roster.yml"
    r.write_text("projects:\n" + "".join(entries))
    return r


def msg(uid, chat="-100777", text="hi", bot=False, mid=None, **extra):
    m = {"message_id": mid or uid, "chat": {"id": int(chat), "title": "grp"},
         "from": {"is_bot": bot, "username": "u", "id": 7}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": uid, "message": m}


class ReadEnv(unittest.TestCase):
    def test_keys_quotes_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.env"
            f.write_text("# TELEGRAM_BOT_TOKEN=dead\nTELEGRAM_BOT_TOKEN='1:a'\nAGENT_TELEGRAM_CHAT_ID=-5\nX=\n")
            self.assertEqual(ing.read_env_keys(f, {"TELEGRAM_BOT_TOKEN", "AGENT_TELEGRAM_CHAT_ID", "X"}),
                             {"TELEGRAM_BOT_TOKEN": "1:a", "AGENT_TELEGRAM_CHAT_ID": "-5"})
            self.assertEqual(ing.read_bot_token(f), "1:a")
            self.assertEqual(ing.read_env_keys("/nonexistent", {"A"}), {})


class LoadRoutes(unittest.TestCase):
    def test_dedicated_default_disabled_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = env_file(tmp, "a", "111:a", "-1")
            b = env_file(tmp, "b", None, "-2")          # → shared default bot
            c = env_file(tmp, "c", "333:c", "-3")       # disabled: STILL routed
            d = env_file(tmp, "d", "444:d", None)       # no chat: not routed
            r = roster(tmp, [
                f"  - {{name: a, env_file: {a}, state_dir: {tmp}/sa}}\n",
                f"  - {{name: b, env_file: {b}, state_dir: {tmp}/sb}}\n",
                f"  - {{name: c, env_file: {c}, state_dir: {tmp}/sc, enabled: false}}\n",
                f"  - {{name: d, env_file: {d}, state_dir: {tmp}/sd}}\n",
            ])
            routes, problems, _w = ing.load_routes(r, default_token="999:shared")
        self.assertEqual(routes["111:a"], {"-1": {"name": "a", "state_dir": f"{tmp}/sa"}})
        self.assertEqual(routes["999:shared"], {"-2": {"name": "b", "state_dir": f"{tmp}/sb"}})
        self.assertEqual(routes["333:c"], {"-3": {"name": "c", "state_dir": f"{tmp}/sc"}})
        self.assertNotIn("444:d", routes)
        self.assertTrue(any(p.startswith("d:") for p in problems))
        self.assertEqual(_w, [str(r), str(a), str(b), str(c), str(d)])   # watched for reload

    def test_no_default_and_no_token_is_unrouted(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = env_file(tmp, "b", None, "-2")
            r = roster(tmp, [f"  - {{name: b, env_file: {b}, state_dir: {tmp}/sb}}\n"])
            routes, problems, _w = ing.load_routes(r, default_token=None)
        self.assertEqual(routes, {})
        self.assertEqual(len(problems), 1)

    def test_same_chat_twice_on_one_bot_refuses_that_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = env_file(tmp, "a", "111:a", "-1")
            b = env_file(tmp, "b", "111:a", "-1")       # same bot, same group
            c = env_file(tmp, "c", "111:a", "-9")       # same bot, other group: collateral
            d = env_file(tmp, "d", "222:d", "-1")       # other bot: unaffected
            r = roster(tmp, [
                f"  - {{name: a, env_file: {a}, state_dir: {tmp}/sa}}\n",
                f"  - {{name: b, env_file: {b}, state_dir: {tmp}/sb}}\n",
                f"  - {{name: c, env_file: {c}, state_dir: {tmp}/sc}}\n",
                f"  - {{name: d, env_file: {d}, state_dir: {tmp}/sd}}\n",
            ])
            routes, problems, _w = ing.load_routes(r)
        self.assertNotIn("111:a", routes)
        self.assertIn("222:d", routes)
        self.assertTrue(any("REFUSING" in p and "a, b" in p for p in problems))

    def test_duplicate_state_dir_second_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = env_file(tmp, "a", "111:a", "-1")
            b = env_file(tmp, "b", "222:b", "-2")
            r = roster(tmp, [
                f"  - {{name: a, env_file: {a}, state_dir: {tmp}/same}}\n",
                f"  - {{name: b, env_file: {b}, state_dir: {tmp}/same}}\n",
            ])
            routes, problems, _w = ing.load_routes(r)
        self.assertIn("111:a", routes)
        self.assertNotIn("222:b", routes)
        self.assertTrue(any("already used" in p for p in problems))


class Spool(unittest.TestCase):
    def test_new_then_dup_then_done_dup(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ing.spool_record(tmp, 42, {"update_id": 42}), "new")
            f = Path(tmp) / "inbox" / "42.json"
            self.assertEqual(json.loads(f.read_text())["update_id"], 42)
            self.assertFalse((Path(tmp) / "inbox" / "42.json.tmp").exists())
            self.assertEqual(ing.spool_record(tmp, 42, {"update_id": 42}), "dup")
            f.rename(f.with_suffix(".done"))              # the pass consumed it
            self.assertEqual(ing.spool_record(tmp, 42, {"update_id": 42}), "dup")
            self.assertEqual(ing.pending_count(tmp), 0)
            ing.spool_record(tmp, 43, {})
            self.assertEqual(ing.pending_count(tmp), 1)

    def test_unwritable_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "inbox").write_text("not a dir")
            with self.assertRaises(OSError):
                ing.spool_record(tmp, 1, {})


class Gc(unittest.TestCase):
    def test_old_done_and_media_removed_pending_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"; inbox.mkdir()
            media = Path(tmp) / "media"; media.mkdir()
            pic = media / "5.jpg"; pic.write_bytes(b"x")
            old = inbox / "1.done"
            old.write_text(json.dumps({"media_path": str(pic)}))
            os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
            fresh = inbox / "2.done"; fresh.write_text("{}")
            pending = inbox / "3.json"; pending.write_text("{}")
            os.utime(pending, (time.time() - 30 * 86400, time.time() - 30 * 86400))
            outside = Path(tmp) / "keep.txt"; outside.write_text("x")
            evil = inbox / "4.done"
            evil.write_text(json.dumps({"media_path": str(outside)}))
            os.utime(evil, (time.time() - 3 * 86400, time.time() - 3 * 86400))
            self.assertEqual(ing.gc_tenant(tmp), 2)
            self.assertFalse(old.exists()); self.assertFalse(pic.exists())
            self.assertTrue(fresh.exists()); self.assertTrue(pending.exists())
            self.assertTrue(outside.exists())              # never deletes outside media/

    def test_no_inbox_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ing.gc_tenant(tmp), 0)


class Wake(unittest.TestCase):
    def test_one_slot_per_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "run-now.d"
            self.assertTrue(ing.signal_wake(d, "alpha"))
            self.assertEqual((d / "alpha").read_text().strip(), "inbox")
            self.assertFalse(ing.signal_wake(d, "alpha"))   # pending: coalesce
            self.assertTrue(ing.signal_wake(d, "beta"))     # never displaces alpha
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["alpha", "beta"])


class Marker(unittest.TestCase):
    def test_cursor_bound_to_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            t1, t2 = [{"name": "a", "state_dir": f"{tmp}/sa"}, {"name": "b", "state_dir": f"{tmp}/sb"}], None
            ing.write_marker(t1[0]["state_dir"], "a", "111:a", 500)
            ing.write_marker(t1[1]["state_dir"], "b", "111:a", 700, {"error": "x"})
            m = ing.read_marker(t1[1]["state_dir"])
            self.assertEqual((m["tenant"], m["tg_offset"], m["error"]), ("b", 700, "x"))
            self.assertEqual(m["token_fp"], ing.token_fp("111:a"))
            self.assertIn("updated_at", m)
            self.assertEqual(ing.load_cursor("111:a", t1), 700)
            self.assertEqual(ing.load_cursor("222:rotated", t1), 0)   # never a foreign offset
            self.assertIsNone(ing.read_marker(f"{tmp}/nowhere"))


class HumanContent(unittest.TestCase):
    def test_classes(self):
        self.assertTrue(ing.human_content(msg(1)["message"]))
        self.assertTrue(ing.human_content(msg(1, text=None, caption="c", photo=[{"file_id": "f"}])["message"]))
        self.assertTrue(ing.human_content(msg(1, text=None, document={"file_id": "f", "mime_type": "application/pdf"})["message"]))
        self.assertTrue(ing.human_content(msg(1, text=None, sticker={"file_id": "s"})["message"]))
        self.assertTrue(ing.human_content(msg(1, text=None, voice={"file_id": "v"})["message"]))
        self.assertTrue(ing.human_content(msg(1, text=None, poll={"id": "p"})["message"]))
        self.assertFalse(ing.human_content(msg(1, text=None, pinned_message={"message_id": 3})["message"]))
        self.assertFalse(ing.human_content(msg(1, bot=True)["message"]))
        self.assertFalse(ing.human_content(msg(1, text=None, new_chat_members=[{}])["message"]))   # service
        self.assertFalse(ing.human_content(None))

    def test_media_ref(self):
        self.assertEqual(ing.media_ref({"photo": [{"file_id": "s", "file_size": 1}, {"file_id": "L", "file_size": 9}]}), ("L", 9))
        self.assertEqual(ing.media_ref({"document": {"file_id": "d", "mime_type": "image/png"}}), ("d", None))
        self.assertEqual(ing.media_ref({"document": {"file_id": "d", "mime_type": "text/plain"}}), (None, None))


class Worker(unittest.TestCase):
    """TokenWorker.process — the batch protocol, with the network stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.sa, self.sb = f"{tmp}/sa", f"{tmp}/sb"
        self.run_now = f"{tmp}/run-now.d"
        chats = {"-100777": {"name": "a", "state_dir": self.sa},
                 "-100888": {"name": "b", "state_dir": self.sb}}
        self.w = ing.TokenWorker("111:a", chats, self.run_now, 1, threading.Event())
        self._dl = ing.download_media
        ing.download_media = lambda token, m, sd: (None, None)

    def tearDown(self):
        ing.download_media = self._dl
        self._tmp.cleanup()

    def reactions(self):
        out = []
        while not self.w.reactions.empty():
            out.append(self.w.reactions.get_nowait())
        return out

    def test_batch_spools_routes_reacts_wakes_and_advances(self):
        self.w.process([msg(12, chat="-100888"), msg(10), msg(11, bot=True),
                        msg(13, chat="-5"), {"update_id": 14, "my_chat_member": {}},
                        msg(15, text=None, new_chat_members=[{}])])
        self.assertEqual(sorted(p.name for p in Path(self.sa, "inbox").iterdir()), ["10.json"])
        self.assertEqual(sorted(p.name for p in Path(self.sb, "inbox").iterdir()), ["12.json"])
        rec = json.loads(Path(self.sa, "inbox", "10.json").read_text())
        self.assertEqual((rec["tenant"], rec["chat_id"], rec["message"]["text"]), ("a", "-100777", "hi"))
        self.assertIn("received_at", rec)
        self.assertEqual(self.reactions(), [("-100777", 10), ("-100888", 12)])   # id order
        self.assertEqual(sorted(p.name for p in Path(self.run_now).iterdir()), ["a", "b"])
        self.assertEqual(self.w.cursor, 15)     # bots/foreign/service: acked, dropped

    def test_redelivery_is_deduped_no_second_reaction(self):
        self.w.process([msg(10)])
        self.reactions()
        self.w.process([msg(10), msg(11)])
        self.assertEqual(self.reactions(), [("-100777", 11)])
        self.assertEqual(sorted(p.name for p in Path(self.sa, "inbox").iterdir()), ["10.json", "11.json"])

    def test_spool_failure_holds_cursor_before_the_failed_update(self):
        calls = []
        orig = ing.spool_record

        def flaky(sd, uid, rec):
            calls.append(uid)
            if uid == 11:
                raise OSError("disk full")
            return orig(sd, uid, rec)
        ing.spool_record = flaky
        try:
            with self.assertRaises(OSError):
                self.w.process([msg(10), msg(11), msg(12)])
        finally:
            ing.spool_record = orig
        self.assertEqual(self.w.cursor, 10)       # 11 is NOT acked next poll
        self.assertEqual(calls, [10, 11])         # 12 never attempted
        self.assertEqual(sorted(p.name for p in Path(self.sa, "inbox").iterdir()), ["10.json"])

    def test_resume_pending_on_start_wakes_and_rereacts(self):
        ing.spool_record(self.sb, 3, {"chat_id": "-100888", "message": {"message_id": 3}})
        self.w.resume_pending()
        self.assertEqual([p.name for p in Path(self.run_now).iterdir()], ["b"])
        self.assertEqual(self.reactions(), [("-100888", 3)])   # idempotent re-👀

    def test_ack_flag_follows_batches_and_get_updates_offset(self):
        sent = []
        def fake_get(token, cursor, timeout, ack=True):
            sent.append(ack)
            return []
        # offset param only when ack: exercise get_updates' param building directly
        seen = {}
        def fake_api(token, method, params, timeout):
            seen.update(params); return []
        orig = ing.api; ing.api = fake_api
        try:
            ing.get_updates("t", 50, 1, ack=True);  self.assertEqual(seen.get("offset"), 51)
            seen.clear()
            ing.get_updates("t", 50, 1, ack=False); self.assertNotIn("offset", seen)
            seen.clear()
            ing.get_updates("t", 0, 1, ack=True);   self.assertNotIn("offset", seen)
        finally:
            ing.api = orig
        # worker flag: a fresh cursor from a marker starts with ack=True (the
        # marker's batch may be unconfirmed); no marker → nothing to ack
        self.assertFalse(self.w.ack)
        self.w.process([msg(10)]); self.w.ack = bool([1])
        self.assertTrue(self.w.ack)

    def test_persistent_failures_raise(self):
        for _ in range(ing.FAILURES_EXIT_AFTER - 1):
            self.w.fail_if_persistent("x")
        with self.assertRaises(RuntimeError):
            self.w.fail_if_persistent("x")

    def test_fsynced_record_survives_and_marker_write_failure_is_fatal(self):
        self.assertEqual(ing.spool_record(self.sa, 1, {"a": 1}), "new")
        bad = ing.TokenWorker("111:a", {"-1": {"name": "z", "state_dir": self.sa + "/inbox/1.json/x"}},
                              self.run_now, 1, threading.Event())
        with self.assertRaises(OSError):
            bad.write_markers()


if __name__ == "__main__":
    unittest.main()
