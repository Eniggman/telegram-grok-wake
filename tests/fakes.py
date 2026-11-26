"""Fake Telegram Bot API and fake Grok routine webhook on 127.0.0.1 (no real token)."""

from __future__ import annotations

import email.parser
import email.policy
import json
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "00000:TEST_FAKE_TOKEN_NOT_REAL_000000000000"
OWNER = 4242
GUEST = 7777
STRANGER = 9999
ROUTINE_KEY = "routine-key-for-tests-0123456789abcdef"


def read_body(handler) -> bytes:
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        out = b""
        while True:
            size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline()
                return out
            out += handler.rfile.read(size)
            handler.rfile.readline()
    n = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(n)


def _value(v: str):
    """aiogram sends every field as a form value: numbers/JSON as text."""
    try:
        return json.loads(v)
    except ValueError:
        return v


def parse_body(ctype: str, raw: bytes) -> dict:
    if ctype.startswith("application/json"):
        return json.loads(raw or b"{}")
    params: dict = {"_multipart": raw}
    if ctype.startswith("multipart/form-data"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename() is None:
                params[name] = _value(part.get_content() if isinstance(part.get_content(), str)
                                      else part.get_payload(decode=True).decode())
    elif ctype.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl
        for k, v in parse_qsl(raw.decode()):
            params[k] = _value(v)
    return params


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.fail_next: dict[str, tuple[int, str]] = {}
        self.next_message_id = 1000
        self.me = {"id": 1, "is_bot": True, "first_name": "Test", "username": "test_bridge_bot",
                   "has_topics_enabled": False, "has_topic_sidebar": False,
                   "allows_users_to_create_topics": False}
        self.profile_photos = 0
        self.hang: dict[str, float] = {}  # method -> seconds to stall before answering
        self.next_thread_id = 200
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                m = re.match(r"^/file/bot([^/]+)/(.+)$", self.path)
                if not m or m.group(1) != TOKEN or m.group(2) not in fake.files:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = fake.files[m.group(2)]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                m = re.match(r"^/bot([^/]+)/(\w+)$", self.path)
                raw = read_body(self)
                if not m or m.group(1) != TOKEN:
                    return self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                method = m.group(2)
                ctype = self.headers.get("Content-Type", "")
                params = parse_body(ctype, raw)
                fake.calls.append((method, params))
                if method in fake.hang:
                    import time as _t
                    _t.sleep(fake.hang[method])
                if method in fake.fail_next:
                    code, desc = fake.fail_next.pop(method)
                    return self._reply(code, {"ok": False, "error_code": code, "description": desc})
                self._reply(200, {"ok": True, "result": fake.result(method, params)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.handle_error = lambda *a: None  # clients aborting mid-request is expected
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def result(self, method, params):
        if method == "getMe":
            return dict(self.me)
        if method == "getWebhookInfo":
            return {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
        if method == "getUpdates":
            # Like Telegram: updates stay queued until a later call confirms them
            # with a higher offset.
            off = params.get("offset", 0)
            self.updates = [u for u in self.updates if u["update_id"] >= off]
            return list(self.updates)
        if method in ("sendMessage", "sendPhoto", "sendDocument", "editMessageText", "sendRichMessage"):
            self.next_message_id += 1
            out = {"message_id": self.next_message_id, "date": 1,
                   "chat": {"id": params.get("chat_id", OWNER), "type": "private"}}
            if method == "sendRichMessage":
                rm = params.get("rich_message") or {}
                if isinstance(rm, str):
                    try:
                        rm = json.loads(rm)
                    except ValueError:
                        rm = {}
                out["rich_message"] = {"blocks": []}
                # keep a copy of the input so tests can inspect markdown/html
                out["_input_rich_message"] = rm
            return out
        if method == "sendRichMessageDraft":
            return True
        if method == "setMySettings":
            for k in ("has_topics_enabled", "has_topic_sidebar", "allows_users_to_create_topics"):
                if k in params:
                    self.me[k] = bool(params[k])
            return True
        if method == "createForumTopic":
            self.next_thread_id += 1
            return {"message_thread_id": self.next_thread_id, "name": params.get("name"),
                    "icon_color": params.get("icon_color") or 7322096}
        if method == "getFile":
            return {"file_id": params["file_id"], "file_unique_id": params["file_id"],
                    "file_path": f"files/{params['file_id']}.bin"}
        if method == "setMyProfilePhoto":
            self.profile_photos += 1
            return True
        if method == "getUserProfilePhotos":
            count = self.profile_photos
            photos = [[{"file_id": "ph1", "file_unique_id": "u1", "width": 512, "height": 512}]] if count else []
            return {"total_count": count, "photos": photos}
        return True

    def methods(self):
        return [c[0] for c in self.calls]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeRoutine:
    def __init__(self):
        self.posts: list[tuple[dict, dict]] = []
        self.statuses: list[int] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(read_body(self))
                fake.posts.append((dict(self.headers), body))
                code = fake.statuses.pop(0) if fake.statuses else 200
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/automations/webhook/test"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_env(tmp: Path, tg: FakeTelegram, routine: FakeRoutine | None = None, extra: dict | None = None) -> dict:
    cfgdir = tmp / "cfg"
    cfgdir.mkdir(mode=0o700, exist_ok=True)
    env_file = cfgdir / "env"
    lines = [f"TG_BOT_TOKEN={TOKEN}", f"TG_OWNER_ID={OWNER}", f"TG_API_BASE={tg.base}",
             f"TG_BRIDGE_HOME={tmp / 'state'}", "TG_TYPING_MAX_SECONDS=0"]
    if routine:
        lines += [f"ROUTINE_URL={routine.url}", f"ROUTINE_KEY={ROUTINE_KEY}"]
    extra = dict(extra or {})
    # Legacy placeholder tests cover the edit hand-off; v4.3.2 default is "done".
    extra.setdefault("TG_PLACEHOLDER_MODE", "edit")
    for k, v in extra.items():
        lines.append(f"{k}={v}")
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return {"TG_BRIDGE_ENV_FILE": str(env_file), "HOME": str(tmp)}


def msg(update_id, text="hi", from_id=OWNER, chat_id=OWNER, chat_type="private", **extra):
    m = {"message_id": update_id * 10, "date": 1, "chat": {"id": chat_id, "type": chat_type},
         "from": {"id": from_id, "is_bot": False, "first_name": "Owner"}, "text": text}
    m.update(extra)
    if "caption" in extra or "photo" in extra or "document" in extra or "voice" in extra:
        m.pop("text", None) if text == "hi" else None
    return {"update_id": update_id, "message": m}
