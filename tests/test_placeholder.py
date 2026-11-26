"""v4.3: placeholder claim / edit / delete / release, and format choice when replacing it."""

import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import OWNER, make_env
from tests.test_bridge import SyncBase
from tgbridge import placeholder as ph_mod
from tgbridge import send as send_mod
from tgbridge.config import load_config
from tgbridge.placeholder import LocalPlaceholders


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpd.name)
        os.chmod(self.tmp, 0o700)
        self.store = LocalPlaceholders(self.tmp)

    def tearDown(self):
        self.tmpd.cleanup()

    def test_add_take_is_atomic_and_single_use(self):
        self.store.add(OWNER, None, reply_to=10, message_id=42)
        first = self.store.take(OWNER, 10)
        self.assertEqual(first["message_id"], 42)
        self.assertEqual(first["state"], "active")  # row as it was before the update
        self.assertIsNone(self.store.take(OWNER, 10))
        self.assertEqual(self.store.get(OWNER, 10)["state"], "claimed")

    def test_release_makes_it_claimable_again(self):
        self.store.add(OWNER, 55, reply_to=10, message_id=42)
        self.store.take(OWNER, 10)
        self.store.release(OWNER, 42)
        again = self.store.take(OWNER, 10)
        self.assertEqual(again["message_id"], 42)

    def test_unscoped_take_only_when_exactly_one_recent(self):
        self.store.add(OWNER, None, reply_to=10, message_id=42)
        self.store.add(OWNER, None, reply_to=11, message_id=43)
        self.assertIsNone(self.store.take(OWNER, None))  # two actives
        self.store.take(OWNER, 11)
        got = self.store.take(OWNER, None)
        self.assertEqual(got["message_id"], 42)

    def test_sticker_message_id_round_trips_on_take_and_claim(self):
        self.store.add(OWNER, 7, reply_to=10, message_id=42, sticker_message_id=88)
        got = self.store.take(OWNER, 10)
        self.assertEqual(got["message_id"], 42)
        self.assertEqual(got["sticker_message_id"], 88)
        self.store.release(OWNER, 42)
        ph = ph_mod.claim(self.tmp, "polling", OWNER, 10, 7)
        self.assertIsNotNone(ph)
        self.assertEqual(ph.message_id, 42)
        self.assertEqual(ph.sticker_message_id, 88)
        self.assertEqual(ph.source, "local")

    def test_old_sqlite_without_sticker_column_is_upgraded(self):
        import sqlite3
        from contextlib import closing
        path = self.tmp / "placeholders.sqlite3"
        with closing(sqlite3.connect(path)) as c:
            c.execute("CREATE TABLE placeholders (chat_id INTEGER, reply_to INTEGER, thread_id INTEGER,"
                      " message_id INTEGER, state TEXT, created INTEGER, updated INTEGER,"
                      " PRIMARY KEY (chat_id, reply_to))")
            now = int(time.time())
            c.execute("INSERT INTO placeholders VALUES (1, 10, 0, 42, 'active', ?, ?)", (now, now))
            c.commit()
        store = LocalPlaceholders(self.tmp)
        store.add(OWNER, None, reply_to=11, message_id=50, sticker_message_id=9)
        self.assertEqual(store.get(OWNER, 11)["sticker_message_id"], 9)
        self.assertEqual(store.get(1, 10)["message_id"], 42)
        self.assertIsNone(store.get(1, 10)["sticker_message_id"])


class PlaceholderSendTests(SyncBase):
    USER_MSG = 10
    PH_ID = 99

    def setUp(self):
        super().setUp()
        self.store = LocalPlaceholders(self.tmp / "state")
        self.store.add(OWNER, None, reply_to=self.USER_MSG, message_id=self.PH_ID)

    def run_send(self, argv, stdin=""):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            code = send_mod.main(argv, stdin=io.StringIO(stdin), environ=self.env)
        finally:
            sys.stdout = old
        raw = out.getvalue()
        return code, json.loads(raw) if raw.strip() else {}

    def posted(self, method):
        return [c[1] for c in self.tg.calls if c[0] == method]

    def test_classic_reply_edits_placeholder(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "короткий ответ"])
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [self.PH_ID])
        edits = self.posted("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], self.PH_ID)
        self.assertIn("короткий ответ", edits[0]["text"])
        self.assertEqual(edits[0].get("parse_mode"), "HTML")
        self.assertNotIn("rich_message", edits[0])
        self.assertEqual(self.posted("sendMessage"), [])
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(self.store.get(OWNER, self.USER_MSG)["state"], "claimed")

    def test_rich_auto_detect_edits_placeholder_with_rich_message(self):
        body = "# Отчёт\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG)], stdin=body)
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [self.PH_ID])
        edits = self.posted("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], self.PH_ID)
        rm = edits[0].get("rich_message") or {}
        if isinstance(rm, str):
            rm = json.loads(rm)
        self.assertIn("Отчёт", rm.get("markdown") or "")
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertEqual(self.posted("sendMessage"), [])
        self.assertEqual(self.posted("deleteMessage"), [])

    def test_rich_edit_refusal_deletes_and_sends_new(self):
        self.tg.fail_next["editMessageText"] = (400, "Bad Request: message can't be edited")
        code, res = self.run_send(["text", "--rich", "--reply-to", str(self.USER_MSG), "итог"])
        self.assertEqual(code, 0)
        self.assertNotEqual(res["message_ids"], [self.PH_ID])
        self.assertEqual(len(self.posted("editMessageText")), 1)
        self.assertEqual(len(self.posted("deleteMessage")), 1)
        self.assertEqual(self.posted("deleteMessage")[0]["message_id"], self.PH_ID)
        self.assertEqual(len(self.posted("sendRichMessage")), 1)

    def test_failed_send_releases_placeholder(self):
        self.tg.fail_next["editMessageText"] = (400, "Bad Request: message can't be edited")
        self.tg.fail_next["sendMessage"] = (400, "Bad Request: chat not found")
        # deleteMessage also may be called, then the send fails; claim must be released
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "не дошло"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.store.get(OWNER, self.USER_MSG)["state"], "active")
        # a later attempt can still replace it
        self.tg.calls.clear()
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "вторая попытка"])
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [self.PH_ID])
        self.assertEqual(self.posted("editMessageText")[0]["message_id"], self.PH_ID)

    def test_second_send_does_not_overwrite(self):
        self.assertEqual(self.run_send(["text", "--reply-to", str(self.USER_MSG), "первый"])[0], 0)
        self.tg.calls.clear()
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "второй, уже новое"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(len(self.posted("sendMessage")), 1)
        self.assertIn("второй", self.posted("sendMessage")[0]["text"])

    def test_no_placeholder_flag_leaves_it_alone(self):
        code, res = self.run_send(["text", "--no-placeholder", "--reply-to", str(self.USER_MSG), "мимо"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(len(self.posted("sendMessage")), 1)
        self.assertEqual(self.store.get(OWNER, self.USER_MSG)["state"], "active")

    def test_effect_cannot_edit_so_deletes_and_sends(self):
        code, res = self.run_send(["text", "--effect", "5104841245755180586",
                                   "--reply-to", str(self.USER_MSG), "с эффектом"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(self.posted("deleteMessage")[0]["message_id"], self.PH_ID)
        self.assertEqual(len(self.posted("sendMessage")), 1)
        self.assertEqual(int(self.posted("sendMessage")[0].get("message_effect_id")), 5104841245755180586)

    def test_photo_deletes_placeholder_after_send(self):
        pic = self.tmp / "pic.jpg"
        pic.write_bytes(b"\xff\xd8\xffJPEG")
        code, res = self.run_send(["photo", str(pic), "--reply-to", str(self.USER_MSG)])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(self.posted("deleteMessage")[0]["message_id"], self.PH_ID)
        self.assertEqual(len(self.posted("sendPhoto")), 1)

    def test_too_old_to_edit_deletes_and_sends(self):
        # Rewrite the record with an old created time; env has a 10s max age.
        self.tmpd  # keep lint happy
        env_file = Path(self.env["TG_BRIDGE_ENV_FILE"])
        with open(env_file, "a") as f:
            f.write("TG_PLACEHOLDER_EDIT_MAX_AGE=10\n")
        # Recreate the row with created 60s ago
        created = int(time.time()) - 60
        import sqlite3
        db = self.tmp / "state" / ph_mod.DB_NAME
        with sqlite3.connect(db) as c:
            c.execute("UPDATE placeholders SET created = ? WHERE chat_id = ? AND reply_to = ?",
                      (created, OWNER, self.USER_MSG))
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "уже поздно"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(self.posted("deleteMessage")[0]["message_id"], self.PH_ID)
        self.assertEqual(len(self.posted("sendMessage")), 1)


if __name__ == "__main__":
    unittest.main()


class PlaceholderDoneModeTests(PlaceholderSendTests.__mro__[1]):
    """v4.3.2 default: the answer is a new message, the placeholder stays and becomes ✅."""
    USER_MSG = 10
    PH_ID = 99

    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp, self.tg, None, {"TG_PLACEHOLDER_MODE": "done"})
        self.store = LocalPlaceholders(self.tmp / "state")
        self.store.add(OWNER, None, reply_to=self.USER_MSG, message_id=self.PH_ID)

    run_send = PlaceholderSendTests.run_send
    posted = PlaceholderSendTests.posted

    def _check(self, send_method):
        self.assertEqual(len(self.posted(send_method)), 1)
        edits = self.posted("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], self.PH_ID)
        self.assertEqual(edits[0]["text"], "\u2705")
        self.assertEqual(self.posted("deleteMessage"), [])

    def test_classic_answer_is_new_message_and_placeholder_turns_done(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "короткий ответ"])
        self.assertEqual(code, 0)
        self.assertNotEqual(res["message_ids"], [self.PH_ID])
        self._check("sendMessage")

    def test_rich_answer_is_new_message_and_placeholder_turns_done(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG)],
                                  stdin="# Отчёт\n\n| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(code, 0)
        self._check("sendRichMessage")


class ThinkingStickerDeleteTests(PlaceholderSendTests.__mro__[1]):
    """v4.4: the first answer deletes the thinking sticker; the placeholder stays (done → ✅)."""
    USER_MSG = 10
    PH_ID = 99
    STICKER_ID = 88

    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp, self.tg, None, {"TG_PLACEHOLDER_MODE": "done"})
        self.store = LocalPlaceholders(self.tmp / "state")
        self.store.add(OWNER, None, reply_to=self.USER_MSG, message_id=self.PH_ID,
                       sticker_message_id=self.STICKER_ID)

    run_send = PlaceholderSendTests.run_send
    posted = PlaceholderSendTests.posted

    def test_first_classic_answer_deletes_sticker_and_marks_placeholder_done(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "короткий ответ"])
        self.assertEqual(code, 0)
        self.assertNotEqual(res["message_ids"], [self.PH_ID])
        self.assertEqual(len(self.posted("sendMessage")), 1)
        deleted = self.posted("deleteMessage")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["message_id"], self.STICKER_ID)
        self.assertNotEqual(deleted[0]["message_id"], self.PH_ID)
        edits = self.posted("editMessageText")
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], self.PH_ID)
        self.assertEqual(edits[0]["text"], "\u2705")

    def test_sticker_delete_failure_does_not_break_the_answer(self):
        self.tg.fail_next["deleteMessage"] = (400, "Bad Request: message to delete not found")
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "всё равно"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted("sendMessage")), 1)
        self.assertIn("всё равно", self.posted("sendMessage")[0]["text"])
        self.assertEqual(self.posted("editMessageText")[0]["text"], "\u2705")
        self.assertEqual(self.posted("editMessageText")[0]["message_id"], self.PH_ID)

    def test_failed_send_leaves_the_sticker(self):
        self.tg.fail_next["sendMessage"] = (400, "Bad Request: chat not found")
        code, _ = self.run_send(["text", "--classic", "--reply-to", str(self.USER_MSG), "нет"])
        self.assertEqual(code, 1)
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(self.store.get(OWNER, self.USER_MSG)["state"], "active")


class ThinkingStickerEditModeDeleteTests(PlaceholderSendTests.__mro__[1]):
    """Edit-mode first answer also deletes the sticker, without deleting the placeholder."""
    USER_MSG = 10
    PH_ID = 99

    def setUp(self):
        super().setUp()
        self.store = LocalPlaceholders(self.tmp / "state")
        self.store.add(OWNER, None, reply_to=self.USER_MSG, message_id=self.PH_ID,
                       sticker_message_id=77)

    run_send = PlaceholderSendTests.run_send
    posted = PlaceholderSendTests.posted

    def test_edit_answer_deletes_sticker_only(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "короткий ответ"])
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [self.PH_ID])
        deleted = self.posted("deleteMessage")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["message_id"], 77)
        self.assertEqual(self.posted("editMessageText")[0]["message_id"], self.PH_ID)
        self.assertIn("короткий ответ", self.posted("editMessageText")[0]["text"])



class PlaceholderKeepModeTests(PlaceholderSendTests.__mro__[1]):
    """v4.4.1 default: the answer is a new message, the sticker goes, «Думаю...» is left untouched."""
    USER_MSG = 10
    PH_ID = 99
    STICKER_ID = 88

    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp, self.tg, None, {"TG_PLACEHOLDER_MODE": "keep"})
        self.store = LocalPlaceholders(self.tmp / "state")
        self.store.add(OWNER, None, reply_to=self.USER_MSG, message_id=self.PH_ID,
                       sticker_message_id=self.STICKER_ID)

    run_send = PlaceholderSendTests.run_send
    posted = PlaceholderSendTests.posted

    def test_default_keeps_placeholder_unchanged(self):
        code, res = self.run_send(["text", "--reply-to", str(self.USER_MSG), "короткий ответ"])
        self.assertEqual(code, 0)
        self.assertNotEqual(res["message_ids"], [self.PH_ID])
        self.assertEqual(len(self.posted("sendMessage")), 1)
        self.assertEqual(self.posted("editMessageText"), [])
        deleted = self.posted("deleteMessage")
        self.assertEqual([d["message_id"] for d in deleted], [self.STICKER_ID])

