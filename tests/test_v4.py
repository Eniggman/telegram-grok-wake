"""v4 (Telegram Serverless intake): box-side pieces.

The Serverless handlers themselves are tested with `node --test tests/serverless/`
(pure logic) and on the platform with `tgcloud run` (see README, "Тесты")."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import GUEST, OWNER, make_env
from tests.test_bridge import SyncBase
from tgbridge import cloud, ctl, fetch, histcli, relay
from tgbridge import send as send_mod
from tgbridge.config import load_config
from tgbridge.history import History

FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CLI_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] == ["run", "endpoints/box"]:
    req = json.loads(args[2])
    print("> box(" + args[2] + ")")          # the real CLI echoes the arguments
    if req["op"] == "boom":
        print("✗ Uncaught exception: EndpointError: boom")
        sys.exit(1)
    if req["op"] == "record":
        res = {"ok": True, "added": len(req.get("items", [])), "skipped": 0, "topics": len(req.get("topics", []))}
    elif req["op"] == "history":
        res = {"ok": True, "items": [{"role": "assistant", "text": "из облака", "date": "2026-11-22T20:00:00Z", "ts": 1}]}
    elif req["op"] == "stop_check":
        res = {"ok": True, "stopped": False}
    else:
        res = {"ok": True, "echo": req}
    print("< 'TGB:" + json.dumps(res, ensure_ascii=False).encode().hex() + "'  12ms")
'''


class CloudCallTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        self.cli = self.tmp / "tgcloud"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o700)
        self.log = self.tmp / "calls.jsonl"
        self.env = {"PATH": os.environ.get("PATH", ""), "TGCLOUD_TOKEN": "app1:secretpart", "FAKE_CLI_LOG": str(self.log)}
        self.p = mock.patch.multiple(cloud, TGCLOUD=self.cli, PROJECT=self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.d.cleanup()

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_call_parses_hex_result_and_passes_ctx(self):
        res = cloud.call("ping", environ=self.env, x="привет")
        self.assertEqual(res["echo"], {"op": "ping", "x": "привет"})
        args = self.calls()[0]
        self.assertEqual(args[:2], ["run", "endpoints/box"])
        self.assertEqual(args[3:], ["--ctx", "{box:true}"])

    def test_error_line_never_contains_the_echoed_arguments(self):
        with self.assertRaises(cloud.CloudError) as cm:
            cloud.call("boom", environ=self.env, routine_key="SUPERSECRETKEY123")
        self.assertIn("boom", str(cm.exception))
        self.assertNotIn("SUPERSECRETKEY123", str(cm.exception))

    def test_requires_token(self):
        env = dict(self.env)
        env.pop("TGCLOUD_TOKEN")
        with self.assertRaisesRegex(cloud.CloudError, "TGCLOUD_TOKEN"):
            cloud.call("ping", environ=env)

    def test_outbox_flush_sends_and_keeps_late_items(self):
        sd = self.tmp / "state"
        sd.mkdir()
        cloud.queue_record(sd, {"role": "assistant", "chat_id": OWNER, "thread_id": 0, "text": "a", "tg_message_id": 5})
        cloud.queue_record(sd, {"kind": "topic", "chat_id": OWNER, "thread_id": 9, "name": "T"})
        self.assertEqual(stat.S_IMODE(os.stat(sd / cloud.OUTBOX).st_mode), 0o600)
        seen = []

        def caller(op, environ=None, **kw):
            seen.append((op, kw))
            if len(seen) == 1:  # a reply sent while the flush is running
                cloud.queue_record(sd, {"role": "assistant", "chat_id": OWNER, "text": "late", "tg_message_id": 6})
            return {"ok": True, "added": len(kw["items"]), "skipped": 0}

        res = cloud.flush(sd, caller=caller)
        self.assertEqual(seen[0][1]["topics"], [{"chat_id": OWNER, "thread_id": 9, "name": "T"}])
        self.assertEqual([i["text"] for i in seen[0][1]["items"]], ["a"])
        self.assertEqual([i["text"] for i in seen[1][1]["items"]], ["late"])
        self.assertEqual(res["added"], 2)
        self.assertEqual(cloud.outbox_size(sd), 0)

    def test_failed_flush_keeps_items(self):
        sd = self.tmp / "state"
        sd.mkdir()
        cloud.queue_record(sd, {"role": "assistant", "chat_id": OWNER, "text": "keep"})

        def caller(op, environ=None, **kw):
            raise cloud.CloudError("offline")

        with self.assertRaises(cloud.CloudError):
            cloud.flush(sd, caller=caller)
        self.assertEqual(cloud.outbox_size(sd), 1)

    def test_mode_file(self):
        sd = self.tmp / "s2"
        self.assertEqual(cloud.get_mode(sd), "polling")
        cloud.set_mode(sd, "serverless")
        self.assertEqual(cloud.get_mode(sd), "serverless")
        self.assertEqual(stat.S_IMODE(os.stat(sd / cloud.MODE_FILE).st_mode), 0o600)


class ServerlessModeBoxTests(SyncBase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.tmp, self.tg, self.routine, {"TG_ALLOWED_USER_IDS": str(GUEST)})
        self.cfg = load_config(need_routine=True, environ=self.env)
        cloud.set_mode(self.cfg.state_dir, "serverless")

    def run_send(self, argv, stdin=""):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            with mock.patch.object(send_mod, "spawn_flush") as sp:
                code = send_mod.main(argv, stdin=io.StringIO(stdin), environ=self.env)
        finally:
            sys.stdout = old
        return code, json.loads(out.getvalue()), sp

    def test_tg_send_queues_reply_for_cloud_memory_not_local(self):
        code, res, sp = self.run_send(["text", "--chat", str(GUEST), "--thread", "55", "ответ"])
        self.assertEqual(code, 0)
        sp.assert_called_once()
        lines = [json.loads(x) for x in (self.cfg.state_dir / cloud.OUTBOX).read_text().splitlines()]
        self.assertEqual(lines[0]["role"], "assistant")
        self.assertEqual((lines[0]["chat_id"], lines[0]["thread_id"], lines[0]["text"]), (GUEST, 55, "ответ"))
        self.assertEqual(lines[0]["tg_message_id"], res["message_ids"][0])
        self.assertEqual(History(self.cfg.state_dir).recent(GUEST, 55, 10), [])

    def test_failed_send_queues_nothing(self):
        self.tg.fail_next["sendRichMessage"] = (400, "Bad Request: chat not found")
        self.tg.fail_next["sendMessage"] = (400, "Bad Request: chat not found")
        code, _, sp = self.run_send(["text", "lost"])
        self.assertEqual(code, 1)
        sp.assert_not_called()
        self.assertEqual(cloud.outbox_size(self.cfg.state_dir), 0)

    def test_relay_and_ensure_refuse_to_take_over(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(relay.main([]), 3)
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                self.assertEqual(ctl.ensure(), 0)
            self.assertIn("Serverless", out.getvalue())
        self.assertEqual(self.tg.methods(), [])  # never touched getUpdates/deleteWebhook

    def test_history_cli_reads_cloud(self):
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(histcli, "call", return_value={"ok": True, "items": [
                    {"role": "user", "text": "из облака", "date": "2026-11-22T20:00:00Z", "ts": 1}]}) as c, \
                mock.patch.object(histcli, "flush", return_value={}):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                self.assertEqual(histcli.main(["--chat", str(GUEST), "--thread", "7", "--limit", "5"]), 0)
            c.assert_called_once_with("history", chat_id=GUEST, thread_id=7, limit=5)
            self.assertIn("из облака", out.getvalue())
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(histcli.main(["--chat", "12345"]), 4)

    def test_fetch_attachments_downloads_by_file_id(self):
        self.tg.files["files/F1.bin"] = b"hello"
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch("sys.stdout", out):
            self.assertEqual(fetch.main(["--chat", str(OWNER), "F1"]), 0)
        res = json.loads(out.getvalue())
        p = Path(res["files"][0]["path"])
        self.assertEqual(p.read_bytes(), b"hello")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertTrue(str(p).startswith(str(self.cfg.inbox_dir)))
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch("sys.stdout", out):
            self.assertEqual(fetch.main(["--chat", "777", "F1"]), 4)
            self.assertEqual(fetch.main(["--chat", str(OWNER), "MISSING"]), 1)


class CloudCliCommandsTests(unittest.TestCase):
    def test_commands_prints_scopes(self):
        from tgbridge import cloudcli
        seen = []

        def fake_call(op, **kw):
            seen.append(op)
            return {"commands": {"default": [{"command": "new", "description": "Новый"}],
                                 "owner_chat": [], "menu_button": {"type": "commands"}}}
        out = io.StringIO()
        with mock.patch.object(cloudcli, "call", fake_call), mock.patch("sys.stdout", out):
            self.assertEqual(cloudcli.commands(False), 0)
            self.assertEqual(cloudcli.commands(True), 0)
        self.assertEqual(seen, ["commands_set", "commands_get"])
        text = out.getvalue()
        self.assertIn("/new — Новый", text)
        self.assertIn("(пусто)", text)
        self.assertIn("menu_button", text)

    def test_version(self):
        from tgbridge import __version__
        self.assertEqual(__version__, "4.6.0")


if __name__ == "__main__":
    unittest.main()
