"""Conversation memory: storage, isolation, payload, /reset, tg-send recording, tg-history."""

import io
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import GUEST, OWNER, STRANGER, make_env, msg
from tests.test_bridge import Base, SyncBase
from tgbridge import histcli
from tgbridge.histcli import DEFAULT_LIMIT, MAX_LIMIT
from tgbridge import send as send_mod
from tgbridge.config import load_config
from tgbridge.history import ITEM_MAX_CHARS, History


class StorageTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.TemporaryDirectory()
        self.h = History(Path(self.d.name))

    def tearDown(self):
        self.d.cleanup()

    def test_order_limit_and_item_format(self):
        for i in range(5):
            self.h.add(1, None, "user" if i % 2 == 0 else "assistant", f"m{i}", tg_message_id=100 + i, ts=1000 + i)
        items = self.h.recent(1, None, 3)
        self.assertEqual([x["text"] for x in items], ["m2", "m3", "m4"])  # last 3, oldest first
        self.assertEqual(items[0], {"role": "user", "text": "m2", "date": "1970-01-01T00:16:42Z", "ts": 1002,
                                    "message_id": 102})

    def test_isolation_between_chats_and_topics(self):
        self.h.add(1, None, "user", "owner main", tg_message_id=1)
        self.h.add(1, 55, "user", "owner topic 55", tg_message_id=2)
        self.h.add(1, 56, "user", "owner topic 56", tg_message_id=3)
        self.h.add(2, None, "user", "guest main", tg_message_id=1)
        self.h.add(2, 55, "user", "guest topic 55", tg_message_id=2)
        texts = lambda c, t: [x["text"] for x in self.h.recent(c, t, 10)]
        self.assertEqual(texts(1, None), ["owner main"])
        self.assertEqual(texts(1, 55), ["owner topic 55"])
        self.assertEqual(texts(1, 56), ["owner topic 56"])
        self.assertEqual(texts(2, None), ["guest main"])
        self.assertEqual(texts(2, 55), ["guest topic 55"])
        self.assertEqual(texts(3, None), [])

    def test_char_budget_and_item_truncation(self):
        for i in range(10):
            self.h.add(1, None, "user", "x" * 1000, tg_message_id=i, ts=i)
        items = self.h.recent(1, None, 20, max_chars=3500)
        self.assertLessEqual(sum(len(x["text"]) for x in items), 3500)
        self.assertEqual(items[-1]["message_id"], 9)  # newest kept
        self.assertEqual(ITEM_MAX_CHARS, 4000)
        self.h.add(1, None, "assistant", "y" * 5000, tg_message_id=50)
        last = self.h.recent(1, None, 1)[0]
        self.assertEqual(len(last["text"]), ITEM_MAX_CHARS)
        self.assertTrue(last["text"].startswith("y") and last["text"].endswith("y"))
        self.assertIn("\n… [пропуск] …\n", last["text"])
        self.assertEqual(len(self.h.recent(1, None, 1, item_max_chars=None)[0]["text"]), 5000)
        body = "HEAD" + ("m" * 5000) + "MIDDLE" + ("m" * 5000) + "TAIL"
        self.h.add(1, None, "user", body, tg_message_id=51)
        cut = self.h.recent(1, None, 1)[0]["text"]
        self.assertEqual(len(cut), ITEM_MAX_CHARS)
        self.assertLessEqual(len(cut), ITEM_MAX_CHARS)
        self.assertTrue(cut.startswith("HEAD"))
        self.assertTrue(cut.endswith("TAIL"))
        self.assertIn("\n… [пропуск] …\n", cut)
        self.assertNotIn("MIDDLE", cut)

    def test_dedupe_by_telegram_message_id(self):
        self.assertIsNotNone(self.h.add(1, None, "user", "a", tg_message_id=7))
        self.assertIsNone(self.h.add(1, None, "user", "a again", tg_message_id=7))
        self.assertEqual(len(self.h.recent(1, None, 10)), 1)

    def test_reset_only_that_conversation(self):
        self.h.add(1, None, "user", "keep", tg_message_id=1)
        self.h.add(1, 55, "user", "drop", tg_message_id=2)
        self.assertEqual(self.h.reset(1, 55), 1)
        self.assertEqual(self.h.recent(1, 55, 10), [])
        self.assertEqual(len(self.h.recent(1, None, 10)), 1)

    def test_retention_prune(self):
        now = int(time.time())
        self.h.add(1, None, "user", "old", tg_message_id=1, ts=now - 31 * 86400)
        self.h.add(1, None, "user", "new", tg_message_id=2, ts=now - 86400)
        self.assertEqual(self.h.prune(30), 1)
        self.assertEqual([x["text"] for x in self.h.recent(1, None, 10)], ["new"])

    def test_db_is_private(self):
        self.h.add(1, None, "user", "x", tg_message_id=1)
        self.assertEqual(oct(self.h.path.stat().st_mode & 0o777), "0o600")


class PayloadTests(Base):
    def posts(self):
        return [p[1] for p in self.routine.posts]

    async def test_history_in_payload_and_current_message_excluded(self):
        r = self.relay()
        await r.process([msg(1, "first question")])
        first = self.posts()[0]
        self.assertEqual(first["history"], [])
        self.assertIn("history_note", first)
        # the bot answers via tg-send (same state dir), then the user follows up
        r.history.add(OWNER, None, "assistant", "first answer", tg_message_id=5000)
        await r.process([msg(2, "and a follow-up?")])
        h = self.posts()[1]["history"]
        self.assertEqual([(x["role"], x["text"]) for x in h],
                         [("user", "first question"), ("assistant", "first answer")])
        self.assertEqual(set(h[0]), {"role", "text", "date", "ts", "message_id"})
        self.assertTrue(h[0]["date"].endswith("Z"))
        # existing contract is untouched
        body = self.posts()[1]
        for k in ("type", "message", "sender", "sender_is_owner", "sender_role", "chat", "owner", "how_to_reply"):
            self.assertIn(k, body)
        self.assertEqual(body["message"]["text"], "and a follow-up?")

    async def test_topics_and_guest_are_isolated(self):
        self.env = make_env(self.tmp, self.tg, self.routine, {"TG_ALLOWED_USER_IDS": str(GUEST)})
        r = self.relay()
        await r.process([
            msg(1, "owner main"),
            msg(2, "owner in topic", is_topic_message=True, message_thread_id=55),
            msg(3, "guest main", from_id=GUEST, chat_id=GUEST),
        ])
        await r.process([
            msg(4, "owner topic again", is_topic_message=True, message_thread_id=55),
            msg(5, "owner main again"),
            msg(6, "guest again", from_id=GUEST, chat_id=GUEST),
        ])
        later = {p["message"]["text"]: [x["text"] for x in p["history"]] for p in self.posts()[3:]}
        self.assertEqual(later["owner topic again"], ["owner in topic"])
        self.assertEqual(later["owner main again"], ["owner main"])
        self.assertEqual(later["guest again"], ["guest main"])

    async def test_history_limit_setting_and_off(self):
        self.env = make_env(self.tmp, self.tg, self.routine, {"TG_HISTORY_MESSAGES": "2"})
        r = self.relay()
        await r.process([msg(i, f"m{i}") for i in range(1, 6)])
        self.assertEqual([x["text"] for x in self.posts()[-1]["history"]], ["m3", "m4"])
        self.env = make_env(self.tmp, self.tg, self.routine, {"TG_HISTORY_MESSAGES": "0"})
        await self.relay().process([msg(9, "no memory")])
        self.assertEqual(self.posts()[-1]["history"], [])

    async def test_attachments_and_forwarded_are_recorded(self):
        self.tg.files["files/d1.bin"] = b"%PDF"
        r = self.relay()
        await r.process([msg(1, document={"file_id": "d1", "file_name": "report.pdf", "file_size": 4}),
                         msg(2, "from eve", forward_origin={"type": "user", "sender_user": {"first_name": "Eve"}})])
        await r.process([msg(3, "what did I send?")])
        h = self.posts()[-1]["history"]
        self.assertEqual(h[0]["attachments"][0]["kind"], "document")
        self.assertTrue(Path(h[0]["attachments"][0]["path"]).exists())
        self.assertTrue(h[1]["forwarded"])

    async def test_redelivery_after_restart_does_not_duplicate(self):
        r = self.relay()
        await r.process([msg(1, "once")])
        r.state.data["offset"] = 0  # simulate Telegram re-sending update 1 after a crash
        r.state.save()
        await self.relay().process([msg(1, "once")])
        h = self.posts()[-1]["history"]
        self.assertEqual(h, [])  # the message is never in its own history
        self.assertEqual(len(r.history.recent(OWNER, None, 10)), 1)

    async def test_failed_delivery_is_not_remembered(self):
        self.routine.statuses = [401]
        r = self.relay()
        await r.process([msg(1, "never answered")])
        self.assertEqual(r.history.recent(OWNER, None, 10), [])

    async def test_strangers_and_commands_leave_no_history(self):
        r = self.relay()
        await r.process([msg(1, "hi", from_id=STRANGER, chat_id=STRANGER), msg(2, "/ping")])
        self.assertEqual(r.history.recent(STRANGER, None, 10), [])
        self.assertEqual(r.history.recent(OWNER, None, 10), [])


class ResetTests(Base):
    async def test_reset_and_new_clear_only_this_topic_and_reply_there(self):
        r = self.relay()
        await r.process([msg(1, "main"), msg(2, "topic", is_topic_message=True, message_thread_id=55)])
        for n, cmd in enumerate(("/reset", "/new")):
            await r.process([msg(10 + 2 * n, "before", is_topic_message=True, message_thread_id=55)])
            await r.process([msg(11 + 2 * n, cmd, is_topic_message=True, message_thread_id=55)])
            await r.process([msg(12 + 2 * n, "after", is_topic_message=True, message_thread_id=55)])
        posts = [p[1] for p in self.routine.posts]
        after = [p for p in posts if p["message"]["text"] == "after"]
        self.assertEqual(len(after), 2)
        for p in after:
            self.assertEqual(p["history"], [])  # topic forgotten after /reset and after /new
        self.assertEqual(len(r.history.recent(OWNER, None, 10)), 1)  # main chat untouched
        # reset is answered locally in the same topic and does not wake the routine
        notes = [c[1] for c in self.tg.calls if c[0] == "sendMessage" and "очищена" in c[1]["text"]]
        self.assertEqual(notes[0]["message_thread_id"], 55)
        self.assertIn("очищена", notes[0]["text"])
        self.assertNotIn("/reset", [p["message"]["text"] for p in posts])


class SendRecordsTests(SyncBase):
    def run_send(self, argv, stdin=""):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            code = send_mod.main(argv, stdin=io.StringIO(stdin), environ=self.env)
        finally:
            sys.stdout = old
        return code, json.loads(out.getvalue())

    def hist(self, chat=OWNER, thread=None):
        cfg = load_config(need_routine=False, environ=self.env)
        return History(cfg.state_dir).recent(chat, thread, 20)

    def test_tg_send_records_replies_per_chat_and_topic(self):
        self.env = make_env(self.tmp, self.tg, None, {"TG_ALLOWED_USER_IDS": str(GUEST)})
        self.assertEqual(self.run_send(["text", "to owner main"])[0], 0)
        self.assertEqual(self.run_send(["text", "--thread", "55", "owner topic"])[0], 0)
        self.assertEqual(self.run_send(["text", "--chat", str(GUEST), "to guest"])[0], 0)
        self.assertEqual([x["text"] for x in self.hist(OWNER, None)], ["to owner main"])
        self.assertEqual([x["text"] for x in self.hist(OWNER, 55)], ["owner topic"])
        self.assertEqual([x["text"] for x in self.hist(GUEST, None)], ["to guest"])
        self.assertEqual(self.hist(OWNER, None)[0]["role"], "assistant")

    def test_long_reply_is_one_history_item_and_photo_has_label(self):
        self.assertEqual(self.run_send(["text"], stdin="слово " * 1500)[0], 0)
        self.assertEqual(len(self.hist()), 1)
        img = self.tmp / "pic.png"
        img.write_bytes(b"\x89PNG fake")
        self.assertEqual(self.run_send(["photo", str(img), "--caption", "готово"])[0], 0)
        self.assertEqual(self.hist()[-1]["text"], "[photo: pic.png] готово")

    def test_refused_or_failed_send_is_not_recorded(self):
        self.assertEqual(self.run_send(["--chat", str(STRANGER), "text", "x"])[0], 4)
        self.tg.fail_next["sendRichMessage"] = (400, "Bad Request: chat not found")
        self.tg.fail_next["sendMessage"] = (400, "Bad Request: chat not found")
        self.assertEqual(self.run_send(["text", "lost"])[0], 1)
        self.assertEqual(self.hist(), [])

    def test_history_cli_reads_and_enforces_allowlist(self):
        self.run_send(["text", "answer one"])
        old_env = dict(os.environ)
        os.environ.update(self.env)
        try:
            def cli(*argv):
                out, old = io.StringIO(), sys.stdout
                sys.stdout = out
                try:
                    code = histcli.main(list(argv))
                finally:
                    sys.stdout = old
                return code, out.getvalue()
            code, out = cli("--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)[0]["text"], "answer one")
            code, out = cli()
            self.assertIn("assistant: answer one", out)
            self.assertEqual(DEFAULT_LIMIT, 30)
            self.assertEqual(MAX_LIMIT, 500)
            self.assertEqual(cli("--chat", str(STRANGER))[0], 4)
            self.assertIn("(no history)", cli("--thread", "99")[1])
        finally:
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
