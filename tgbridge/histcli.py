"""tg-history: read the stored conversation of one chat (+ topic).

  tg-history --chat 123 [--thread 55] [--limit 30] [--json] [--local]
Only the owner and TG_ALLOWED_USER_IDS chats can be read. Text is shown as data.
v4 (serverless mode): read from the Serverless database (the memory the routine
gets); --local reads the box's v3 history.sqlite3 (used in polling mode).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .cloud import CloudError, call, flush, get_mode
from .config import ConfigError, load_config
from .history import History

DEFAULT_LIMIT = 30
MAX_LIMIT = 500


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tg-history", description="Show stored conversation history.")
    ap.add_argument("--chat", help="numeric chat id (default: the owner)")
    ap.add_argument("--thread", type=int, help="topic id (message_thread_id); omit for the main chat")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"how many latest messages (1-{MAX_LIMIT}, default {DEFAULT_LIMIT})")
    ap.add_argument("--json", action="store_true", help="JSON array, same item format as the webhook `history`")
    ap.add_argument("--local", action="store_true", help="read the box's local SQLite history (polling mode)")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(need_routine=False)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        chat = int(a.chat) if a.chat else cfg.owner_id
    except ValueError:
        print("refused: --chat must be a numeric Telegram user id", file=sys.stderr)
        return 4
    if chat not in cfg.allowed_user_ids:
        print("refused: only the owner or an allowed user's chat can be read", file=sys.stderr)
        return 4
    limit = max(1, min(a.limit, MAX_LIMIT))
    if get_mode(cfg.state_dir) == "serverless" and not a.local:
        try:
            try:
                flush(cfg.state_dir)  # store just-sent replies first
            except CloudError:
                pass
            items = call("history", chat_id=chat, thread_id=a.thread or 0, limit=limit)["items"]
        except CloudError as e:
            print(f"history unavailable (Serverless): {e}", file=sys.stderr)
            return 1
    else:
        try:
            items = History(cfg.state_dir).recent(chat, a.thread, limit, item_max_chars=None)
        except sqlite3.Error as e:
            print(f"history unavailable: {e}", file=sys.stderr)
            return 1
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    else:
        for it in items:
            extra = "".join(f" [{x.get('kind')}: {x.get('path') or x.get('file_name')}]"
                            for x in it.get("attachments", []))
            fwd = " (forwarded)" if it.get("forwarded") else ""
            print(f"[{it['date']}] {it['role']}{fwd}: {it['text']}{extra}")
        if not items:
            print("(no history)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
