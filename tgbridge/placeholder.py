"""v4.3: hand-off of the bridge's placeholder reply ("🗨️ Думаю...") to tg-send.

When a message is accepted, the intake (Telegram Serverless, or the polling relay
in fallback mode) replies to it with a short placeholder and records
(chat_id, reply_to = the user's message_id) → placeholder message_id. The first
tg-send that answers that message (`--reply-to <message.message_id>`) CLAIMS the
placeholder and turns it into the answer (editMessageText with rich_message or
text), or deletes it and sends normally when an edit is impossible. A claim is
atomic and single-use, so a second tg-send for the same message (e.g. after
"Принял, делаю…") never overwrites the first one, and the intake's failure notice
never overwrites an answer.

Where the record lives:
- serverless mode: the Serverless DB table `placeholders`, reached through
  `endpoints/box` (`placeholder_claim` / `placeholder_release`). The result is
  cached in the local file below so repeated tg-sends for the same message do
  not ask the cloud again.
- polling mode: the local file only (the relay writes it).

The local file is SQLite (stdlib, WAL, 0600) in the private state dir; it must
never break messaging, so every caller treats errors as "no placeholder".
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DB_NAME = "placeholders.sqlite3"
KEEP_SECONDS = 2 * 86400
UNSCOPED_MAX_AGE = 3600  # same rule as PLACEHOLDER_UNSCOPED_MAX_AGE in serverless core.js

# Same text as PLACEHOLDER_NOTE in serverless/tgcloud/lib/core.js (event field placeholder_note).
PLACEHOLDER_NOTE = (
    "The bridge already replied to this message with a short placeholder (placeholder_message_id). "
    "Reply as usual with tg-send ... --reply-to <message.message_id>: the first such tg-send replaces "
    "the placeholder with your answer (edit, or delete + send), so do not edit or delete it yourself.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS placeholders (
  chat_id INTEGER NOT NULL,
  reply_to INTEGER NOT NULL,
  thread_id INTEGER NOT NULL DEFAULT 0,
  message_id INTEGER,
  sticker_message_id INTEGER,      -- v4.4: thinking sticker; deleted on first answer
  state TEXT NOT NULL,             -- active | claimed | failed | none (cloud said: no placeholder)
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  PRIMARY KEY (chat_id, reply_to)
);
CREATE INDEX IF NOT EXISTS idx_ph_conv ON placeholders (chat_id, thread_id, state);
"""


def _sticker_id(row: dict | None) -> int | None:
    if not row:
        return None
    v = row.get("sticker_message_id")
    try:
        n = int(v) if v is not None else 0
    except (TypeError, ValueError):
        return None
    return n or None


@dataclass
class Placeholder:
    message_id: int
    created: int
    age: int
    source: str            # "cloud" | "local"
    done: bool = False     # turned into the answer, or deleted
    sticker_message_id: int | None = None  # v4.4: deleted when the first answer is sent


class LocalPlaceholders:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / DB_NAME

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)  # private from the start
        os.close(fd)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(placeholders)")}
        if "sticker_message_id" not in cols:
            conn.execute("ALTER TABLE placeholders ADD COLUMN sticker_message_id INTEGER")
        return conn

    def add(self, chat_id: int, thread: int | None, reply_to: int, message_id: int,
            sticker_message_id: int | None = None) -> None:
        t = int(time.time())
        with closing(self._connect()) as c:
            c.execute("INSERT INTO placeholders (chat_id, reply_to, thread_id, message_id, sticker_message_id,"
                      " state, created, updated) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)"
                      " ON CONFLICT(chat_id, reply_to) DO UPDATE SET"
                      " thread_id = excluded.thread_id, message_id = excluded.message_id,"
                      " sticker_message_id = excluded.sticker_message_id, state = 'active',"
                      " created = excluded.created, updated = excluded.updated",
                      (chat_id, reply_to, int(thread or 0), message_id, sticker_message_id, t, t))
            c.execute("DELETE FROM placeholders WHERE created < ?", (t - KEEP_SECONDS,))

    def get(self, chat_id: int, reply_to: int) -> dict | None:
        with closing(self._connect()) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM placeholders WHERE chat_id = ? AND reply_to = ?",
                          (chat_id, reply_to)).fetchone()
            return dict(r) if r else None

    def take(self, chat_id: int, reply_to: int | None, state: str = "claimed",
             thread: int | None = None) -> dict | None:
        """Atomically move an ACTIVE placeholder to `state`; None if there is none.
        Without reply_to: only the single active one in that chat/topic (< 1 h old)."""
        t = int(time.time())
        with closing(self._connect()) as c:
            c.row_factory = sqlite3.Row
            c.execute("BEGIN IMMEDIATE")
            try:
                if reply_to:
                    rows = c.execute("SELECT * FROM placeholders WHERE chat_id = ? AND reply_to = ?"
                                     " AND state = 'active'", (chat_id, reply_to)).fetchall()
                else:
                    rows = c.execute("SELECT * FROM placeholders WHERE chat_id = ? AND thread_id = ?"
                                     " AND state = 'active' AND created > ? LIMIT 2",
                                     (chat_id, int(thread or 0), t - UNSCOPED_MAX_AGE)).fetchall()
                if len(rows) != 1:
                    c.execute("COMMIT")
                    return None
                row = dict(rows[0])
                c.execute("UPDATE placeholders SET state = ?, updated = ? WHERE chat_id = ? AND reply_to = ?",
                          (state, t, chat_id, row["reply_to"]))
                c.execute("COMMIT")
                return row
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def note(self, chat_id: int, reply_to: int, message_id: int | None, state: str, created: int | None = None,
             sticker_message_id: int | None = None) -> None:
        """Cache what the cloud answered ('claimed' with its id, or 'none')."""
        t = int(time.time())
        with closing(self._connect()) as c:
            c.execute("INSERT INTO placeholders (chat_id, reply_to, thread_id, message_id, sticker_message_id,"
                      " state, created, updated) VALUES (?, ?, 0, ?, ?, ?, ?, ?)"
                      " ON CONFLICT(chat_id, reply_to) DO UPDATE SET"
                      " message_id = excluded.message_id, sticker_message_id = excluded.sticker_message_id,"
                      " state = excluded.state, updated = excluded.updated",
                      (chat_id, reply_to, message_id, sticker_message_id, state, created or t, t))

    def forget(self, chat_id: int, reply_to: int) -> None:
        with closing(self._connect()) as c:
            c.execute("DELETE FROM placeholders WHERE chat_id = ? AND reply_to = ?", (chat_id, reply_to))

    def release(self, chat_id: int, message_id: int) -> None:
        with closing(self._connect()) as c:
            c.execute("UPDATE placeholders SET state = 'active', updated = ? WHERE chat_id = ? AND message_id = ?"
                      " AND state = 'claimed'", (int(time.time()), chat_id, message_id))


def claim(state_dir: Path, mode: str, chat_id: int, reply_to: int | None, thread: int | None,
          caller=None) -> Placeholder | None:
    """Claim the placeholder that answers `reply_to` (see module docstring).
    Never raises: any problem means "no placeholder" and tg-send sends normally."""
    local = LocalPlaceholders(state_dir)
    now = int(time.time())
    try:
        row = local.get(chat_id, reply_to) if reply_to else None
        if row is not None:
            if row["state"] == "active":
                got = local.take(chat_id, reply_to)
                if got and got.get("message_id"):
                    return Placeholder(int(got["message_id"]), int(got["created"]), now - int(got["created"]), "local",
                                       sticker_message_id=_sticker_id(got))
            return None  # already claimed / failed / known to have none
        if mode != "serverless":
            if reply_to:
                return None
            got = local.take(chat_id, None, thread=thread)
            if got and got.get("message_id"):
                return Placeholder(int(got["message_id"]), int(got["created"]), now - int(got["created"]), "local",
                                   sticker_message_id=_sticker_id(got))
            return None
    except (sqlite3.Error, OSError):
        if mode != "serverless":
            return None
    # serverless: the cloud is authoritative
    from .cloud import CloudError, call
    try:
        res = (caller or call)("placeholder_claim", timeout=30, chat_id=int(chat_id),
                               reply_to=int(reply_to) if reply_to else None, thread_id=int(thread or 0))
    except (CloudError, OSError, ValueError):
        return None
    ph = res.get("placeholder") or None
    cloud_now = int(res.get("now") or now)
    if reply_to:
        try:
            local.note(chat_id, reply_to, int(ph["message_id"]) if ph else None, "claimed" if ph else "none",
                       int(ph["created"]) if ph else None, sticker_message_id=_sticker_id(ph) if ph else None)
        except (sqlite3.Error, OSError):
            pass
    if not ph or not ph.get("message_id"):
        return None
    created = int(ph.get("created") or cloud_now)
    return Placeholder(int(ph["message_id"]), created, max(0, cloud_now - created), "cloud",
                       sticker_message_id=_sticker_id(ph))


def release(state_dir: Path, chat_id: int, ph: Placeholder, reply_to: int | None, caller=None) -> None:
    """Give an unused placeholder back so the next tg-send can still use it."""
    local = LocalPlaceholders(state_dir)
    if ph.source == "cloud":
        from .cloud import CloudError, call
        try:
            (caller or call)("placeholder_release", timeout=30, chat_id=int(chat_id), message_id=int(ph.message_id))
        except (CloudError, OSError, ValueError):
            pass
        try:
            if reply_to:
                local.forget(chat_id, reply_to)
        except (sqlite3.Error, OSError):
            pass
        return
    try:
        local.release(chat_id, ph.message_id)
    except (sqlite3.Error, OSError):
        pass
