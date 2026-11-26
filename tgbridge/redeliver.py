"""tg-redeliver: list or re-send events the routine webhook did not accept.

  bin/tg-redeliver            list saved events (no sending)
  bin/tg-redeliver --send     deliver them again, oldest first; delete each on success
  bin/tg-redeliver --drop ID  delete one saved event (ID = update id / file name)

v4 (serverless mode): dead letters live in the Serverless database (table
deliveries, status 'dead'); --send posts them from the box with the box's key and
marks them 'redelivered'; --drop ID marks one 'discarded' (ID = delivery id).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from .cloud import CloudError, call, get_mode
from .config import ConfigError, load_config
from .state import deadletter_dir, list_deadletters
from .webhook import WebhookClient


async def _send(cfg, paths) -> int:
    client = WebhookClient(cfg.routine_url, cfg.routine_key, attempts=cfg.delivery_attempts)
    bad = 0
    try:
        for p in paths:
            data = json.loads(p.read_text(encoding="utf-8"))
            event = data.get("event")
            if not isinstance(event, dict) or event.get("type") != "telegram_message":
                print(f"{p.stem}: skipped (raw message saved after an internal error; see file)")
                bad += 1
                continue
            res = await client.deliver(event)
            if res.ok:
                p.unlink()
                print(f"{p.stem}: delivered (HTTP {res.status})")
            else:
                bad += 1
                print(f"{p.stem}: still failing (HTTP {res.status}) — kept")
    finally:
        await client.close()
    return 1 if bad else 0


async def _send_cloud(cfg, items) -> int:
    client = WebhookClient(cfg.routine_url, cfg.routine_key, attempts=cfg.delivery_attempts)
    bad = 0
    try:
        for it in items:
            event = it.get("event")
            if not isinstance(event, dict) or event.get("type") != "telegram_message":
                print(f"{it['id']}: skipped (no event stored)")
                bad += 1
                continue
            res = await client.deliver(event)
            if res.ok:
                call("mark", ids=[it["id"]], status="redelivered", http_status=res.status)
                print(f"{it['id']}: delivered (HTTP {res.status})")
            else:
                bad += 1
                print(f"{it['id']}: still failing (HTTP {res.status}) — kept")
    finally:
        await client.close()
    return 1 if bad else 0


def main_cloud(cfg, a) -> int:
    try:
        if a.drop:
            n = call("mark", ids=[int(a.drop)], status="discarded").get("updated", 0)
            print(f"{a.drop}: {'discarded' if n else 'not found'}")
            return 0 if n else 1
        items = call("deadletters", status="dead", limit=100)["items"]
        if not items:
            print("no undelivered events (Serverless)")
            return 0
        if a.send:
            return asyncio.run(_send_cloud(cfg, items))
        for it in items:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.get("updated") or 0))
            text = ((it.get("event") or {}).get("message") or {}).get("text") or ""
            print(f"{it['id']}: failed {when}, chat {it.get('chat_id')}, HTTP {it.get('http_status')}, "
                  f"attempts {it.get('attempts')}, {it.get('detail') or ''} | {text[:60]!r}")
        return 0
    except CloudError as e:
        print(f"tg-redeliver: {e}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tg-redeliver")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--drop")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(need_routine=True)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if get_mode(cfg.state_dir) == "serverless":
        return main_cloud(cfg, a)
    paths = list_deadletters(cfg.state_dir)
    if a.drop:
        p = deadletter_dir(cfg.state_dir) / f"{int(a.drop)}.json"
        if p.exists():
            p.unlink()
            print(f"{a.drop}: deleted")
            return 0
        print(f"{a.drop}: not found")
        return 1
    if not paths:
        print("no undelivered events")
        return 0
    if a.send:
        return asyncio.run(_send(cfg, paths))
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            d = {}
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("failed_at", 0)))
        print(f"{p.stem}: failed {when}, HTTP {d.get('http')}, {d.get('detail', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
