"""tg-fetch-attachments: download Telegram files by file_id into the private inbox.

  tg-fetch-attachments --chat <chat id> FILE_ID [FILE_ID ...]

In v4 the Serverless handler passes attachments as file ids (message.attachments[]
with path=null) plus a ready command in `fetch_attachments_cmd`. This tool calls
the Bot API getFile with the box's bot token and saves each file under
~/.local/state/tg-bridge/inbox/YYYY-MM-DD/ (mode 0600). Prints JSON:
  {"ok": true, "files": [{"file_id", "path", "size", "file_name"}]}
Exit 0 if all files were saved, 1 if any failed (see "error" per file), 2 config,
4 refused (chat not allowlisted).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config
from .log import Redactor
from .state import ensure_private_dir
from .tgapi import api_retry, make_bot

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
DOWNLOAD_TIMEOUT = 180


async def fetch_all(cfg, file_ids: list[str], bot=None) -> list[dict]:
    redact = Redactor(cfg.secrets)
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base)
    day = cfg.inbox_dir / time.strftime("%Y-%m-%d")
    ensure_private_dir(cfg.inbox_dir)
    ensure_private_dir(day)
    out = []
    try:
        for i, fid in enumerate(file_ids):
            item: dict = {"file_id": fid}
            try:
                f = None
                async for attempt in api_retry():
                    with attempt:
                        f = await bot.get_file(fid)
                fp = f.file_path or ""
                if not fp or fp.startswith("/") or ".." in fp.split("/"):
                    raise ValueError("unsafe file path from Telegram")
                if (f.file_size or 0) > cfg.max_file_bytes:
                    raise ValueError("file is larger than the Bot API download limit (20 MB)")
                base = _SAFE.sub("_", Path(fp).name).strip("._")[:80] or "file"
                uniq = _SAFE.sub("", f.file_unique_id or "")[:32] or str(i)
                dest = day / f"{uniq}-{base}"
                if not dest.exists():
                    tmp = dest.with_name(dest.name + ".part")
                    try:
                        async with asyncio.timeout(DOWNLOAD_TIMEOUT + 10):
                            await bot.download_file(fp, destination=tmp, timeout=DOWNLOAD_TIMEOUT)
                        os.chmod(tmp, 0o600)
                        os.replace(tmp, dest)
                    finally:
                        if tmp.exists():
                            tmp.unlink()
                item.update(path=str(dest), size=dest.stat().st_size, file_name=Path(fp).name)
            except Exception as e:  # noqa: BLE001 - reported per file
                item["error"] = redact(f"{type(e).__name__}: {e}")
            out.append(item)
    finally:
        if own:
            await bot.session.close()
    return out


def main(argv=None, bot=None) -> int:
    ap = argparse.ArgumentParser(prog="tg-fetch-attachments", description="Download Telegram files by file_id.")
    ap.add_argument("--chat", help="chat the files came from (must be the owner or an allowed user)")
    ap.add_argument("file_ids", nargs="+")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(need_routine=False)
    except ConfigError as e:
        print(json.dumps({"ok": False, "error": f"config: {e}"}))
        return 2
    if a.chat is not None:
        try:
            chat = int(a.chat)
        except ValueError:
            chat = None
        if chat not in cfg.allowed_user_ids:
            print(json.dumps({"ok": False, "error": "refused: chat is not the owner or an allowed user"}))
            return 4
    files = asyncio.run(fetch_all(cfg, a.file_ids, bot=bot))
    ok = all("path" in f for f in files)
    print(json.dumps({"ok": ok, "files": files}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
