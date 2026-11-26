"""Local control requests for the running relay.

tg-send can call Telegram itself when TG_BOT_TOKEN is in the process
environment. When the token is only in the already-running relay, a command
drops a small JSON request into the 700 state directory and the relay
performs it with the token it already holds. The token is never written
to disk.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .state import ensure_private_dir, write_json_atomic

ACTIONS = frozenset({"set_photo", "enable_topics", "create_topic", "get_me"})


class ControlError(Exception):
    pass


def _dirs(state_dir: Path) -> tuple[Path, Path]:
    root = state_dir / "control"
    inbox = root / "inbox"
    outbox = root / "outbox"
    ensure_private_dir(root)
    ensure_private_dir(inbox)
    ensure_private_dir(outbox)
    return inbox, outbox


def _is_private_regular(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return not os.path.islink(path) and os.path.isfile(path) and st.st_uid == os.getuid()


def submit_request(state_dir: Path, payload: dict, timeout: float = 90, sleep=time.sleep) -> dict:
    if payload.get("action") not in ACTIONS:
        raise ControlError(f"unknown control action: {payload.get('action')}")
    inbox, outbox = _dirs(state_dir)
    req_id = uuid.uuid4().hex
    req = inbox / f"{req_id}.json"
    res = outbox / f"{req_id}.json"
    write_json_atomic(req, payload)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if _is_private_regular(res):
                try:
                    data = json.loads(res.read_text(encoding="utf-8"))
                except (ValueError, OSError) as e:
                    raise ControlError(f"could not read control result: {e}") from None
                try:
                    res.unlink()
                except OSError:
                    pass
                if not isinstance(data, dict):
                    raise ControlError("control result is not an object")
                return data
            sleep(0.2)
        raise ControlError("the running bridge did not pick up the request in time")
    finally:
        try:
            req.unlink()
        except OSError:
            pass


def take_requests(state_dir: Path) -> list[tuple[str, dict]]:
    inbox, _outbox = _dirs(state_dir)
    out: list[tuple[str, dict]] = []
    try:
        names = sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime)
    except OSError:
        return out
    for path in names:
        if path.suffix != ".json" or not _is_private_regular(path):
            continue
        req_id = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            path.unlink()
        except OSError:
            continue
        if isinstance(data, dict) and data.get("action") in ACTIONS:
            out.append((req_id, data))
    return out


def write_result(state_dir: Path, req_id: str, result: dict) -> None:
    if not all(c.isalnum() or c == "-" for c in req_id) or not req_id:
        return
    _inbox, outbox = _dirs(state_dir)
    write_json_atomic(outbox / f"{req_id}.json", result)
