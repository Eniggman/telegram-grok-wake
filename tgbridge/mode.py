"""tg-mode: which side receives Telegram messages.

  tg-mode                 show the current mode
  tg-mode serverless      v4 default: Telegram Serverless handles intake (webhook);
                          the box relay and supervisord are stopped
  tg-mode polling         fallback: stop using the webhook and run the v3 box relay
                          (long polling under supervisord) again

Telegram allows one intake per bot: a webhook (Serverless) OR getUpdates (relay).
The mode is stored in ~/.local/state/tg-bridge/mode; tg-ensure and the relay
refuse to start while it says "serverless", so nothing takes the bot over by
accident.
"""

from __future__ import annotations

import sys

from . import ctl
from .cloud import CloudError, cli, get_mode, have_token, set_mode
from .config import ConfigError, load_config


def _cfg():
    try:
        return load_config(need_routine=True)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


def to_serverless() -> int:
    from .cloudcli import import_history, setup, webhook_state
    cfg = _cfg()
    if not have_token():
        print("tg-mode: TGCLOUD_TOKEN is not set; cannot manage Serverless", file=sys.stderr)
        return 2
    try:
        print("1/5 настройки и ключ рутины → Serverless (tg-cloud setup)")
        if setup() != 0:
            print("tg-mode: Serverless settings are incomplete; staying in the current mode", file=sys.stderr)
            return 1
        print("2/5 перенос локальной истории (идемпотентно)")
        import_history()
        print("3/5 остановка box-relay и supervisord")
        set_mode(cfg.state_dir, "serverless")
        ctl.stop()
        print("4/5 вебхук Telegram → Serverless (tgcloud webhook sync)")
        proc = cli(["webhook", "sync"], timeout=60)
        if proc.returncode != 0:
            print((proc.stdout or "")[-400:] + (proc.stderr or "")[-400:], file=sys.stderr)
            print("tg-mode: webhook sync failed. Fallback: bin/tg-mode polling", file=sys.stderr)
            return 1
        print("5/5 проверка")
        w = webhook_state()
        ok = bool(w.get("url_set")) and w.get("in_sync", False)
        print(f"вебхук: {'установлен' if w.get('url_set') else 'НЕ установлен'}, "
              f"синхронизирован: {'да' if w.get('in_sync') else 'нет'}, ожидает: {w.get('pending')}")
        print("ГОТОВО: приём через Telegram Serverless" if ok else "ВНИМАНИЕ: проверьте bin/tg-health")
        return 0 if ok else 1
    except CloudError as e:
        print(f"tg-mode: {e}", file=sys.stderr)
        return 1


def to_polling() -> int:
    cfg = _cfg()
    set_mode(cfg.state_dir, "polling")
    # The relay deletes the webhook on start (pending updates are kept) and
    # continues by long polling under supervisord.
    rc = ctl.ensure()
    print("ГОТОВО: приём через box-relay (long polling). Вернуться: bin/tg-mode serverless" if rc == 0
          else "tg-mode: relay did not start; see bin/tg-health")
    return rc


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("status", "show"):
        try:
            cfg = load_config(need_routine=False)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2
        print(get_mode(cfg.state_dir))
        return 0
    if argv[0] == "serverless":
        return to_serverless()
    if argv[0] == "polling":
        return to_polling()
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
