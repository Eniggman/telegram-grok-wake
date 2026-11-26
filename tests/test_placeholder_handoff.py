"""v4.3: placeholder hand-off end to end — relay (polling) → tg-send, and the
serverless-mode cloud claim/release with its local cache."""

import asyncio
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import OWNER, make_env, msg
from tests.test_bridge import Base, SyncBase
from tgbridge import cloud
from tgbridge import placeholder as ph_mod
from tgbridge import send as send_mod
from tgbridge.config import load_config
from tgbridge.history import History
from tgbridge.rich import RICH_MAX_CHARS

PH_TEXT = "\U0001F5E8\uFE0F Думаю..."


class _SendMixin:
    def run_send(self, argv, stdin=""):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            with mock.patch.object(send_mod, "spawn_flush"):
                code = send_mod.main(argv, stdin=io.StringIO(stdin), environ=self.env)
        finally:
            sys.stdout = old
        return code, json.loads(out.getvalue())

    def posted(self, method):
        return [c[1] for c in self.tg.calls if c[0] == method]

    def sent(self):
        return [c[1] for c in self.tg.calls if c[0] in ("sendMessage", "sendRichMessage")]

    @property
    def state_dir(self):
        return load_config(need_routine=False, environ=self.env).state_dir

    def add_local(self, reply_to=10, message_id=500, thread=None, chat=OWNER):
        ph_mod.LocalPlaceholders(self.state_dir).add(chat, thread, reply_to, message_id)

    def local_state(self, reply_to=10, chat=OWNER):
        row = ph_mod.LocalPlaceholders(self.state_dir).get(chat, reply_to)
        return row and row["state"]


class RelayToSendTests(_SendMixin, Base):
    """Polling intake → tg-send, through the real local hand-off."""

    async def test_reply_edits_the_placeholder_once(self):
        await self.relay().process([msg(1, "task")])
        ph = self.posted("sendMessage")[0]
        self.assertEqual(ph["text"], PH_TEXT)
        self.assertEqual(ph["reply_parameters"]["message_id"], 10)
        ph_id = self.routine.posts[0][1]["placeholder_message_id"]
        self.tg.calls.clear()
        code, res = await asyncio.to_thread(self.run_send, ["text", "--reply-to", "10"], "## Ответ")
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [ph_id])
        edit = self.posted("editMessageText")[0]
        self.assertEqual(edit["message_id"], ph_id)
        rm = edit["rich_message"] if isinstance(edit["rich_message"], dict) else json.loads(edit["rich_message"])
        self.assertEqual(rm["markdown"], "## Ответ")
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(History(self.state_dir).recent(OWNER, None, 1)[0]["message_id"], ph_id)
        # a second reply to the same message is a new message; the first is not overwritten
        self.tg.calls.clear()
        code, res = await asyncio.to_thread(self.run_send, ["text", "--reply-to", "10"], "ещё")
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(len(self.sent()), 1)

    async def test_failure_notice_never_overwrites_an_answer(self):
        r = self.relay()
        await r.process([msg(1, "task")])
        await asyncio.to_thread(self.run_send, ["text", "--reply-to", "10"], "ответ")
        self.tg.calls.clear()
        await r.fail_notice("⚠️ notice", None, OWNER, 10)
        self.assertEqual(self.posted("editMessageText"), [])
        self.assertEqual(self.posted("sendMessage")[0]["text"], "⚠️ notice")


class SendPlaceholderTests(_SendMixin, SyncBase):
    def test_too_long_for_rich_edits_first_classic_part(self):
        self.add_local()
        huge = "## big\n\n" + "слово " * (RICH_MAX_CHARS // 4)
        code, res = self.run_send(["text", "--reply-to", "10"], huge)
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"][0], 500)
        self.assertEqual(self.posted("editMessageText")[0]["message_id"], 500)
        self.assertNotIn("rich_message", self.posted("editMessageText")[0])
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertGreater(len(self.posted("sendMessage")), 0)
        self.assertEqual(self.posted("deleteMessage"), [])

    def test_file_sends_then_deletes_placeholder(self):
        f = self.tmp / "report.txt"
        f.write_text("data")
        self.add_local()
        code, res = self.run_send(["file", str(f), "--reply-to", "10"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        methods = [m for m in self.tg.methods() if m in ("sendDocument", "deleteMessage")]
        self.assertEqual(methods, ["sendDocument", "deleteMessage"])

    def test_every_send_path_failing_gives_the_placeholder_back(self):
        self.add_local()
        for m in ("sendRichMessage", "sendMessage"):
            self.tg.fail_next[m] = (400, "Bad Request: chat not found")
        # an effect rules out the edit, so both send paths are tried and both fail
        code, _ = self.run_send(["text", "--rich", "--effect", "1", "--reply-to", "10"], "ответ")
        self.assertEqual(code, 1)
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(self.local_state(), "active")
        self.tg.calls.clear()
        code, res = self.run_send(["text", "--reply-to", "10"], "ответ")
        self.assertEqual((code, res["message_ids"]), (0, [500]))

    def test_undeletable_placeholder_is_marked_done(self):
        self.add_local()
        self.tg.fail_next["deleteMessage"] = (400, "Bad Request: message can't be deleted")
        f = self.tmp / "a.txt"
        f.write_text("x")
        code, _ = self.run_send(["file", str(f), "--reply-to", "10"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText")[0]["text"], "\u2705")

    def test_stream_final_message_replaces_placeholder(self):
        self.add_local()
        code, res = self.run_send(["stream", "--reply-to", "10"], "часть один\nчасть два\n")
        self.assertEqual(code, 0)
        self.assertEqual(res["message_ids"], [500])
        self.assertEqual(self.sent(), [])

    def test_without_reply_to_only_an_unambiguous_placeholder_is_used(self):
        self.add_local(reply_to=10, message_id=500)
        self.add_local(reply_to=20, message_id=600)
        code, _ = self.run_send(["text", "two pending"])
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("editMessageText"), [])
        ph_mod.LocalPlaceholders(self.state_dir).take(OWNER, 20)
        self.tg.calls.clear()
        code, res = self.run_send(["text", "one pending"])
        self.assertEqual(res["message_ids"], [500])

    def test_config_defaults_and_off(self):
        cfg = load_config(need_routine=False, environ=make_env(self.tmp, self.tg, None, {"TG_PLACEHOLDER": "off"}))
        self.assertEqual(cfg.placeholder, "")
        cfg = load_config(need_routine=False, environ=make_env(self.tmp, self.tg, None, {}))
        self.assertEqual((cfg.placeholder, cfg.reaction, cfg.placeholder_edit_max_age), (PH_TEXT, "", 0))
        self.assertEqual((cfg.history_days, cfg.history_messages, cfg.history_max_chars), (7, 20, 12000))
        self.assertEqual(cfg.sticker_pack, "VikostVSpack")
        cfg = load_config(need_routine=False, environ=make_env(self.tmp, self.tg, None, {"TG_STICKER_PACK": "off"}))
        self.assertEqual(cfg.sticker_pack, "")
        cfg = load_config(need_routine=False, environ=make_env(self.tmp, self.tg, None,
                           {"TG_STICKER_PACK": "https://t.me/addstickers/OtherPack"}))
        self.assertEqual(cfg.sticker_pack, "OtherPack")


class ServerlessHandoffTests(_SendMixin, SyncBase):
    def setUp(self):
        super().setUp()
        cloud.set_mode(self.state_dir, "serverless")
        self.cloud_calls = []
        self.cloud_answer = {"ok": True, "placeholder": {"message_id": 700, "thread_id": 55, "reply_to": 10,
                                                          "created": 1000, "sticker_message_id": 801}, "now": 1003}

        def fake_call(op, timeout=90, environ=None, **params):
            self.cloud_calls.append((op, params))
            if op == "placeholder_claim":
                ans, self.cloud_answer = self.cloud_answer, {"ok": True, "placeholder": None, "now": 1}
                return ans
            return {"ok": True, "released": True}
        self.p = mock.patch.object(cloud, "call", fake_call)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_claims_in_the_cloud_once_and_edits(self):
        code, res = self.run_send(["text", "--thread", "55", "--reply-to", "10"], "ответ")
        self.assertEqual((code, res["message_ids"]), (0, [700]))
        self.assertEqual(self.cloud_calls, [("placeholder_claim", {"chat_id": OWNER, "reply_to": 10, "thread_id": 55})])
        self.assertEqual(self.local_state(), "claimed")
        deleted = self.posted("deleteMessage")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["message_id"], 801)
        # follow-up for the same message: answered from the local cache, no cloud round trip
        code, res = self.run_send(["text", "--thread", "55", "--reply-to", "10"], "ещё")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.cloud_calls), 1)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(len(self.posted("editMessageText")), 1)

    def test_no_placeholder_in_cloud_is_cached(self):
        self.cloud_answer = {"ok": True, "placeholder": None, "now": 1}
        self.run_send(["text", "--reply-to", "10"], "a")
        self.run_send(["text", "--reply-to", "10"], "b")
        self.assertEqual(len(self.cloud_calls), 1)
        self.assertEqual(self.posted("editMessageText"), [])

    def test_failed_send_releases_in_the_cloud(self):
        for m in ("editMessageText", "sendRichMessage", "sendMessage"):
            self.tg.fail_next[m] = (400, "Bad Request: chat not found")
        code, _ = self.run_send(["text", "--reply-to", "10"], "ответ")
        self.assertEqual(code, 1)
        self.assertEqual(self.posted("deleteMessage"), [])
        self.assertEqual(self.cloud_calls[-1], ("placeholder_release", {"chat_id": OWNER, "message_id": 700}))
        self.assertIsNone(self.local_state())  # forgotten: the next tg-send asks the cloud again

    def test_cloud_unreachable_means_normal_send(self):
        def boom(*a, **k):
            raise cloud.CloudError("offline")
        with mock.patch.object(cloud, "call", boom):
            code, res = self.run_send(["text", "--reply-to", "10"], "ответ")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent()), 1)
        self.assertIsNone(self.local_state())


if __name__ == "__main__":
    unittest.main()
