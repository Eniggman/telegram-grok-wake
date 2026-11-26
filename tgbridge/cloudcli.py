"""tg-cloud: manage the Telegram Serverless side of the bridge (v4).

  tg-cloud setup            copy settings + routine key from the box config to the
                            bot's Serverless database (key never printed)
  tg-cloud status [--json]  Serverless config (presence/length only), delivery stats,
                            webhook state
  tg-cloud deploy           push the project, apply safe DB migrations, sync webhook
  tg-cloud flush            store queued tg-send replies in the cloud memory
  tg-cloud import-history   copy the local v3 history.sqlite3 into the cloud (idempotent)
  tg-cloud commands [--show]  (re)register the bot menu (setMyCommands, idempotent): owner chat
                            /new /skills /usage /weather, everyone else /new; --show only reads it back
  tg-cloud webhook-sync     re-point Telegram's webhook at Serverless

Needs TGCLOUD_TOKEN in the environment (the CLI access token from @BotFather →
bot → Serverless → CLI Access). It is passed to the official tgcloud CLI only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import __version__
from .cloud import CloudError, call, cli, flush, get_mode, outbox_size, ROOT
from .config import ConfigError, load_config


def _cfg(need_routine: bool):
    try:
        return load_config(need_routine=need_routine)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def setup() -> int:
    cfg = _cfg(True)
    values = {
        "routine_url": cfg.routine_url,
        "routine_key": cfg.routine_key,
        "owner_id": str(cfg.owner_id),
        "allowed_ids": ",".join(str(i) for i in sorted(cfg.allowed_user_ids - {cfg.owner_id})),
        "bin_dir": str(ROOT / "bin"),
        "history_messages": str(cfg.history_messages),
        "history_max_chars": str(cfg.history_max_chars),
        "history_days": str(cfg.history_days),
        "max_wakes_per_min": str(cfg.max_wakes_per_min),
        "reaction": cfg.reaction or "",            # empty = key removed = Serverless default (off)
        "placeholder": cfg.placeholder or "off",
        "sticker_pack": cfg.sticker_pack or "off",  # default on the platform is VikostVSpack
    }
    res = call("config_set", values=values)
    remote = (res.get("config") or {}).get("routine_key") or {}
    match = remote.get("sha256_8") == fingerprint(cfg.routine_key or "")
    print(f"Serverless настроен: ready={res.get('ready')}, ключ рутины: {remote.get('length')} симв., "
          f"совпадает с ключом на боксе: {'да' if match else 'НЕТ'}")
    return 0 if res.get("ready") and match else 1


def webhook_state() -> dict:
    proc = cli(["webhook"], timeout=60)
    out = proc.stdout or ""
    info = {"raw_ok": proc.returncode == 0}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("url:"):
            info["url_set"] = "(none set)" not in s
            info["url_host"] = s.split("://", 1)[1].split("/", 1)[0] if "://" in s else None
        elif s.startswith("allowed updates:"):
            info["allowed_updates"] = s.split(":", 1)[1].strip()
        elif s.startswith("pending updates:"):
            try:
                info["pending"] = int(s.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif "in sync" in s:
            info["in_sync"] = "out of sync" not in s.lower() and "not in sync" not in s.lower()
        elif "error" in s.lower():
            info["last_error"] = s[:200]
    return info


def project_state() -> dict:
    cli(["fetch"], timeout=60)
    proc = cli(["status"], timeout=60)
    out = proc.stdout or ""
    changed = any(k in out for k in ("Modified", "New (not yet deployed)", "Deleted"))
    return {"in_sync_with_cloud": proc.returncode == 0 and not changed,
            "webhook_line": next((l.strip() for l in out.splitlines() if l.strip().startswith("Webhook")), None)}


def status(as_json: bool) -> int:
    cfg = _cfg(False)
    out = {"mode": get_mode(cfg.state_dir), "box_version": __version__, "outbox_waiting": outbox_size(cfg.state_dir)}
    try:
        out["cloud_config"] = call("config_status")
        out["stats"] = call("stats")
        out["webhook"] = webhook_state()
        out["project"] = project_state()
    except CloudError as e:
        out["error"] = str(e)
    if cfg.routine_key and "cloud_config" in out:
        remote = out["cloud_config"].get("config", {}).get("routine_key") or {}
        out["routine_key_matches_box"] = remote.get("sha256_8") == fingerprint(cfg.routine_key)
    print(json.dumps(out, ensure_ascii=False, indent=None if as_json else 1))
    return 0 if "error" not in out else 1


def deploy() -> int:
    steps = [(["push"], 180), (["migrate", "--safe"], 180), (["webhook", "sync"], 60)]
    for args, t in steps:
        proc = cli(args, timeout=t)
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
        print(f"$ tgcloud {' '.join(args)}  (exit {proc.returncode})\n{tail}\n")
        if proc.returncode != 0:
            print((proc.stderr or "").strip()[-500:], file=sys.stderr)
            return 1
    return 0


def import_history() -> int:
    cfg = _cfg(False)
    db = cfg.state_dir / "history.sqlite3"
    if not db.exists():
        print("локальной истории нет (history.sqlite3 не найден)")
        return 0
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT chat_id, thread_id, role, text, attachments, forwarded, tg_message_id, ts"
                            " FROM messages ORDER BY id").fetchall()
    items = [{"chat_id": r[0], "thread_id": r[1], "role": r[2], "text": r[3], "attachments": r[4],
              "forwarded": bool(r[5]), "tg_message_id": r[6], "ts": r[7]} for r in rows]
    added = skipped = 0
    chunk: list = []
    size = 0
    for it in items + [None]:
        n = len(json.dumps(it, ensure_ascii=False).encode()) if it else 0
        if chunk and (it is None or size + n > 80_000):
            res = call("record", items=chunk)
            added += res.get("added", 0)
            skipped += res.get("skipped", 0)
            chunk, size = [], 0
        if it:
            chunk.append(it)
            size += n
    print(f"перенесено строк: {added}, уже были: {skipped}, всего локально: {len(items)}")
    return 0


def commands(show_only: bool) -> int:
    res = call("commands_get" if show_only else "commands_set")
    for scope, items in res["commands"].items():
        if scope == "menu_button":
            print(f"menu_button: {json.dumps(items, ensure_ascii=False)}")
            continue
        print(f"{scope}:")
        for c in items:
            print(f"  /{c['command']} — {c['description']}")
        if not items:
            print("  (пусто)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tg-cloud", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    sub.add_parser("deploy")
    fl = sub.add_parser("flush")
    fl.add_argument("--quiet", action="store_true")
    sub.add_parser("import-history")
    sub.add_parser("webhook-sync")
    cm = sub.add_parser("commands")
    cm.add_argument("--show", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "setup":
            return setup()
        if a.cmd == "status":
            return status(a.json)
        if a.cmd == "deploy":
            return deploy()
        if a.cmd == "flush":
            cfg = _cfg(False)
            res = flush(cfg.state_dir)
            if not a.quiet:
                print(json.dumps(res, ensure_ascii=False))
            return 0
        if a.cmd == "import-history":
            return import_history()
        if a.cmd == "commands":
            return commands(a.show)
        if a.cmd == "webhook-sync":
            proc = cli(["webhook", "sync"], timeout=60)
            print("\n".join((proc.stdout or "").strip().splitlines()[-6:]))
            return proc.returncode
    except CloudError as e:
        if not getattr(a, "quiet", False):
            print(f"tg-cloud: {e}", file=sys.stderr)
        return 1
    return 64


if __name__ == "__main__":
    sys.exit(main())
