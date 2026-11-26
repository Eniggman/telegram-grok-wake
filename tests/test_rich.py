"""Rich Messages (Bot API 10.1+): limits, sendRichMessage, fallback, auto-detect."""

import os
from unittest import mock
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import GUEST, OWNER, STRANGER, make_env
from tests.test_bridge import SyncBase
from tgbridge import send as send_mod
from tgbridge.config import load_config
from tgbridge.history import History
from tgbridge.rich import (RICH_MAX_BLOCKS, RICH_MAX_CHARS, RICH_MAX_MEDIA, RICH_MAX_TABLE_COLS,
                           RichLimitError, build_rich_message, estimate_blocks, inspect_rich,
                           looks_rich, should_send_rich, validate_rich)


SAMPLE = """# Проверка

| Поле | Значение |
|:-----|:---------|
| Версия | 4.1.0 |

<details>
<summary>Подробности</summary>

Скрытый текст.

</details>
"""


class LimitTests(unittest.TestCase):
    def test_ok_sample(self):
        validate_rich(SAMPLE)
        self.assertGreater(estimate_blocks(SAMPLE), 2)
        rm = build_rich_message(SAMPLE)
        self.assertEqual(rm.markdown, SAMPLE)
        self.assertIsNone(rm.html)
        self.assertIsNone(build_rich_message("<h1>x</h1>", html=True).markdown)

    def test_char_limit(self):
        with self.assertRaises(RichLimitError) as cm:
            validate_rich("x" * (RICH_MAX_CHARS + 1))
        self.assertTrue(cm.exception.issues[0].hard)
        self.assertIn(str(RICH_MAX_CHARS), str(cm.exception))

    def test_table_columns(self):
        cols = RICH_MAX_TABLE_COLS + 1
        header = "| " + " | ".join(f"c{i}" for i in range(cols)) + " |"
        sep = "|" + "|".join(["---"] * cols) + "|"
        row = "| " + " | ".join("v" for _ in range(cols)) + " |"
        issues = inspect_rich(f"{header}\n{sep}\n{row}\n")
        self.assertTrue(any("columns" in i.message for i in issues))
        validate_rich("| a | b |\n|---|---|\n| 1 | 2 |\n")  # 2 cols is fine

    def test_html_table_columns(self):
        cells = "".join(f"<th>c{i}</th>" for i in range(21))
        text = f"<table><tr>{cells}</tr></table>"
        self.assertTrue(any("columns" in i.message for i in inspect_rich(text)))

    def test_media_and_blocks(self):
        media = "\n".join(f"![](https://example.com/{i}.jpg)" for i in range(RICH_MAX_MEDIA + 1))
        self.assertTrue(any("media" in i.message for i in inspect_rich(media)))
        rows = ["| a | b |", "|---|---|"] + [f"| {i} | x |" for i in range(RICH_MAX_BLOCKS)]
        self.assertTrue(any("blocks" in i.message for i in inspect_rich("\n".join(rows))))


class DetectTests(unittest.TestCase):
    def test_looks_rich_only_for_headings_tables_details(self):
        self.assertFalse(looks_rich("Привет, **мир**. Вот `код` и [ссылка](https://t.me)."))
        self.assertFalse(looks_rich("- пункт\n- ещё"))
        self.assertFalse(looks_rich("не заголовок #hash в середине"))
        self.assertTrue(looks_rich("# Заголовок\n\nтекст"))
        self.assertTrue(looks_rich("## Подзаголовок"))
        self.assertTrue(looks_rich("| A | B |\n|---|---|\n| 1 | 2 |"))
        self.assertTrue(looks_rich("<details><summary>x</summary>y</details>"))
        self.assertTrue(looks_rich("<h2>Title</h2><p>hi</p>"))
        self.assertTrue(looks_rich("<table><tr><th>A</th></tr></table>"))
        # a heading only inside a fence must not force Rich
        self.assertFalse(looks_rich("пример:\n```\n# not a heading\n```\nготово"))

    def test_should_send_rich_flags(self):
        short = "просто ответ"
        structured = "# Отчёт\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        self.assertFalse(should_send_rich(short))
        self.assertTrue(should_send_rich(structured))  # auto-detect
        self.assertTrue(should_send_rich(short, rich=True))
        self.assertFalse(should_send_rich(structured, classic=True))  # --classic wins over auto
        self.assertTrue(should_send_rich(structured, rich=True, classic=True))  # explicit --rich wins
        self.assertFalse(should_send_rich(structured, fmt="plain"))
        self.assertFalse(should_send_rich(short, classic=True))  # no-op alias for short text


class RichSendTests(SyncBase):

    def run_send(self, argv, stdin=""):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            code = send_mod.main(argv, stdin=io.StringIO(stdin), environ=self.env)
        finally:
            sys.stdout = old
        return code, json.loads(out.getvalue())

    def posted(self, method):
        return [c[1] for c in self.tg.calls if c[0] == method]

    def rich_body(self, call=None):
        call = call or self.posted("sendRichMessage")[0]
        body = call.get("rich_message") or {}
        if isinstance(body, str):
            body = json.loads(body)
        return body

    @mock.patch.dict(os.environ, {"TG_REPLY_QUOTE": "1"})

    def test_rich_subcommand_sends_markdown(self):
        self.env = make_env(self.tmp, self.tg, None, {"TG_ALLOWED_USER_IDS": str(GUEST)})
        code, res = self.run_send(["rich", "--chat", str(GUEST), "--thread", "55", "--reply-to", "9"],
                                  stdin=SAMPLE)
        self.assertEqual(code, 0)
        self.assertEqual(len(res["message_ids"]), 1)
        call = self.posted("sendRichMessage")[0]
        self.assertEqual(call["chat_id"], GUEST)
        self.assertEqual(call["message_thread_id"], 55)
        self.assertEqual(call["reply_parameters"]["message_id"], 9)
        self.assertEqual(self.rich_body(call)["markdown"], SAMPLE.rstrip("\n"))
        self.assertEqual(self.posted("sendMessage"), [])
        hist = History(load_config(need_routine=False, environ=self.env).state_dir)
        self.assertEqual(hist.recent(GUEST, 55, 1)[0]["text"], SAMPLE.rstrip("\n"))

    def test_rich_html_flag(self):
        html = "<h1>Заголовок</h1><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        code, res = self.run_send(["rich", "--html"], stdin=html)
        self.assertEqual(code, 0)
        self.assertEqual(self.rich_body()["html"], html)
        self.assertNotIn("markdown", self.rich_body())

    def test_text_defaults_to_classic_for_short_replies(self):
        code, res = self.run_send(["text", "--reply-to", "3"], stdin="привет, это обычный ответ")
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertGreaterEqual(len(self.posted("sendMessage")), 1)
        self.assertIn("привет", self.posted("sendMessage")[0]["text"])

    def test_text_auto_detects_headings_tables_details(self):
        code, res = self.run_send(["text", "--reply-to", "3"], stdin="## Hi\n\nhello")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted("sendRichMessage")), 1)
        self.assertEqual(self.rich_body()["markdown"], "## Hi\n\nhello")
        self.assertEqual(self.posted("sendMessage"), [])

    def test_text_rich_flag_forces_rich(self):
        code, res = self.run_send(["text", "--rich", "просто текст без заголовка"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted("sendRichMessage")), 1)
        self.assertEqual(self.rich_body()["markdown"], "просто текст без заголовка")

    def test_text_classic_disables_auto_rich(self):
        code, res = self.run_send(["text", "--classic"], stdin="## Hi\n\nhello")
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertGreaterEqual(len(self.posted("sendMessage")), 1)

    def test_text_classic_and_plain_skip_rich(self):
        self.assertEqual(self.run_send(["text", "--classic", "hello"])[0], 0)
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertEqual(self.posted("sendMessage")[0]["text"].find("hello") >= 0, True)
        self.tg.calls.clear()
        self.assertEqual(self.run_send(["text", "--format", "plain", "raw <b>"])[0], 0)
        self.assertEqual(self.posted("sendRichMessage"), [])
        self.assertIsNone(self.posted("sendMessage")[0].get("parse_mode"))

    def test_fallback_when_rich_rejected(self):
        self.tg.fail_next["sendRichMessage"] = (400, "Bad Request: RICH_MESSAGE_INVALID")
        code, res = self.run_send(["text", "--rich"], stdin="**hello** world")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted("sendRichMessage")), 1)
        self.assertGreaterEqual(len(self.posted("sendMessage")), 1)
        hist = History(load_config(need_routine=False, environ=self.env).state_dir)
        self.assertIn("hello", hist.recent(OWNER, None, 1)[0]["text"])

    def test_rich_no_fallback_surfaces_the_error(self):
        self.tg.fail_next["sendRichMessage"] = (400, "Bad Request: RICH_MESSAGE_INVALID")
        code, res = self.run_send(["rich", "--no-fallback", "hello"])
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(self.posted("sendMessage"), [])

    def test_over_limit_rich_refuses_but_text_splits(self):
        huge = "слово " * (RICH_MAX_CHARS // 4)
        code, res = self.run_send(["rich", "--no-fallback"], stdin=huge)
        self.assertEqual(code, 4)
        self.assertIn("32768", res["error"])
        self.assertEqual(self.tg.calls, [])
        code, res = self.run_send(["text"], stdin=huge)
        self.assertEqual(code, 0)
        self.assertEqual(self.posted("sendRichMessage"), [])  # skipped, too big
        self.assertGreater(len(self.posted("sendMessage")), 1)

    def test_allowlist_and_secrets_apply_to_rich(self):
        self.assertEqual(self.run_send(["--chat", str(STRANGER), "rich", "hi"])[0], 4)
        from tests.fakes import TOKEN
        self.assertEqual(self.run_send(["rich", f"token {TOKEN}"])[0], 5)
        self.assertEqual(self.tg.calls, [])

    def test_stream_defaults_to_classic_then_switches_when_text_looks_rich(self):
        code, res = self.run_send(["stream"], stdin="# Title\nline two\n")
        self.assertEqual(code, 0)
        methods = self.tg.methods()
        self.assertIn("sendMessageDraft", methods)          # started classic (empty buffer)
        self.assertIn("sendRichMessageDraft", methods)     # switched after the heading arrived
        self.assertEqual(methods[-1], "sendRichMessage")
        self.assertIn("Title", self.rich_body()["markdown"])

    def test_stream_plain_stays_classic(self):
        code, res = self.run_send(["stream"], stdin="hello stream\n")
        self.assertEqual(code, 0)
        self.assertIn("sendMessageDraft", self.tg.methods())
        self.assertEqual(self.tg.methods()[-1], "sendMessage")
        self.assertNotIn("sendRichMessage", self.tg.methods())

    def test_stream_rich_flag_uses_rich_draft(self):
        code, res = self.run_send(["stream", "--rich"], stdin="hello stream\n")
        self.assertEqual(code, 0)
        self.assertIn("sendRichMessageDraft", self.tg.methods())
        self.assertEqual(self.tg.methods()[-1], "sendRichMessage")


if __name__ == "__main__":
    unittest.main()
