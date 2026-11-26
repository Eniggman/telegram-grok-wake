"""Small on-disk state: offset, last delivery, lock, dead letters. All 0600/0700."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class State:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        ensure_private_dir(self.dir)
        self.path = self.dir / "state.json"
        self.data: dict = {"offset": 0, "last_delivery": None, "wakes": []}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass

    @property
    def offset(self) -> int:
        return int(self.data.get("offset") or 0)

    def advance(self, update_id: int) -> None:
        if update_id + 1 > self.offset:
            self.data["offset"] = update_id + 1
            self.save()

    def record_delivery(self, status: str, http: int | None, update_id: int) -> None:
        self.data["last_delivery"] = {"ts": int(time.time()), "status": status, "http": http, "update_id": update_id}
        self.save()

    def allow_wake(self, per_minute: int, now: float | None = None) -> bool:
        now = now or time.time()
        wakes = [t for t in self.data.get("wakes", []) if now - t < 60]
        if len(wakes) >= per_minute:
            self.data["wakes"] = wakes
            return False
        wakes.append(now)
        self.data["wakes"] = wakes
        return True

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    # -- private-chat topics (thread_id -> name) --------------------------
    def remember_topic(self, thread_id: int, name: str | None = None, **meta) -> None:
        topics = self.data.setdefault("topics", {})
        entry = dict(topics.get(str(thread_id)) or {})
        if name:
            entry["name"] = name
        for k, v in meta.items():
            if v is not None:
                entry[k] = v
        topics[str(thread_id)] = entry
        self.save()

    def topic_name(self, thread_id: int | None) -> str | None:
        if not thread_id:
            return None
        entry = (self.data.get("topics") or {}).get(str(thread_id)) or {}
        return entry.get("name")

    def forget_topic(self, thread_id: int) -> None:
        topics = self.data.get("topics") or {}
        if str(thread_id) in topics:
            topics.pop(str(thread_id), None)
            self.save()


def deadletter_dir(state_dir: Path) -> Path:
    return state_dir / "deadletter"


def save_deadletter(state_dir: Path, event: dict, status: int | None, detail: str) -> Path:
    """Persist an event the webhook did not accept, so nothing is silently lost."""
    d = deadletter_dir(state_dir)
    ensure_private_dir(d)
    uid = (event.get("update_ids") or [0])[-1]
    path = d / f"{int(uid)}.json"
    write_json_atomic(path, {"failed_at": int(time.time()), "http": status, "detail": detail,
                             "event": event})
    return path


def list_deadletters(state_dir: Path) -> list[Path]:
    d = deadletter_dir(state_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file() and not p.is_symlink())


class SingleInstanceLock:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "relay.lock"
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())
        return True

    @staticmethod
    def is_held(state_dir: Path) -> bool:
        path = state_dir / "relay.lock"
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)


def mark_outbound(state_dir: Path) -> None:
    """Called by tg-send so the relay's typing indicator can stop."""
    try:
        if state_dir.is_dir():
            p = state_dir / "last_outbound"
            p.touch(mode=0o600, exist_ok=True)
            os.utime(p, None)
    except OSError:
        pass


def last_outbound(state_dir: Path) -> float:
    try:
        return (state_dir / "last_outbound").stat().st_mtime
    except OSError:
        return 0.0
