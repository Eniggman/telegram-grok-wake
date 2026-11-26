"""tg-health: is the bridge configured, supervised, alive, and reaching Telegram?"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import xmlrpc.client
from pathlib import Path

from .config import ConfigError, load_config, state_dir_only
from .state import SingleInstanceLock, list_deadletters
from .watchdog import read_heartbeat

PROGRAM = "tg-bridge"


def supervisor_rpc(state_dir: Path):
    """XML-RPC proxy to our supervisord over its private unix socket (or None)."""
    sock = state_dir / "supervisor.sock"
    if not sock.exists():
        return None
    from supervisor.xmlrpc import SupervisorTransport
    transport = SupervisorTransport(None, None, serverurl=f"unix://{sock}")
    return xmlrpc.client.ServerProxy("http://127.0.0.1", transport=transport)


def supervisor_status(state_dir: Path) -> dict | None:
    rpc = supervisor_rpc(state_dir)
    if rpc is None:
        return None
    try:
        rpc.supervisor.getState()
        procs = {p["name"]: p for p in rpc.supervisor.getAllProcessInfo()}
        return procs
    except (OSError, xmlrpc.client.Error):
        return None


async def _telegram_checks(cfg, bot=None) -> list[tuple[bool, str]]:
    from .send import me_info
    from .tgapi import TelegramAPIError, make_bot
    results: list[tuple[bool, str]] = []
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base, timeout=15)
    try:
        async with asyncio.timeout(40):
            me = me_info(await bot.get_me())
            results.append((True, f"Telegram принимает токен, бот @{me.get('username')}"))
            topics = me.get("has_topics_enabled")
            results.append((bool(topics), "Режим нескольких чатов (темы в личке) включён" if topics
                            else "Режим нескольких чатов (темы в личке) выключен"))
            info = await bot.get_webhook_info()
            hook = bool(info.url)
            results.append((not hook, "Вебхук Telegram не установлен (хорошо для long polling)" if not hook
                            else "У бота установлен вебхук — мост удалит его при старте"))
            pend = info.pending_update_count or 0
            results.append((pend < 50, f"Ожидающих сообщений в Telegram: {pend}"))
    except (TelegramAPIError, TimeoutError) as e:
        results.append((False, f"Не удалось связаться с Telegram: {e or type(e).__name__}"))
    finally:
        if own:
            await bot.session.close()
    return results


async def _telegram_webhook_checks(cfg, bot=None) -> list[tuple[bool, str]]:
    from .send import me_info
    from .tgapi import TelegramAPIError, make_bot
    results: list[tuple[bool, str]] = []
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base, timeout=15)
    try:
        async with asyncio.timeout(40):
            me = me_info(await bot.get_me())
            results.append((True, f"Telegram принимает токен, бот @{me.get('username')}"))
            info = await bot.get_webhook_info()
            host = (info.url or "").split("://", 1)[-1].split("/", 1)[0]
            results.append((bool(info.url), f"getWebhookInfo: вебхук → {host}" if info.url
                            else "getWebhookInfo: вебхук НЕ установлен (сообщения не принимаются!)"))
            pend = info.pending_update_count or 0
            results.append((pend < 20, f"Ожидающих доставки в Serverless: {pend}"))
            if info.last_error_date:
                age = int(time.time()) - int(info.last_error_date.timestamp() if hasattr(info.last_error_date, "timestamp")
                                             else info.last_error_date)
                results.append((age > 3600, f"Последняя ошибка вебхука {age // 60} мин назад: {info.last_error_message}"))
    except (TelegramAPIError, TimeoutError) as e:
        results.append((False, f"Не удалось связаться с Telegram: {e or type(e).__name__}"))
    finally:
        if own:
            await bot.session.close()
    return results


def serverless_checks(cfg, offline: bool, environ=None, bot=None) -> list[tuple[bool, str]]:
    from .cloud import CloudError, call, have_token, outbox_size
    results: list[tuple[bool, str]] = [(True, "Режим приёма: Telegram Serverless (v4)")]
    sd = cfg.state_dir
    procs = supervisor_status(sd) if sd.exists() else None
    relay = sd.exists() and SingleInstanceLock.is_held(sd)
    results.append((procs is None and not relay,
                    "Box-relay и supervisord остановлены (так и должно быть)" if procs is None and not relay
                    else "Box-relay/supervisord запущены — конфликт с вебхуком! bin/tg-stop"))
    tok = have_token(environ)
    results.append((tok, "TGCLOUD_TOKEN задан" if tok else "TGCLOUD_TOKEN не задан — tg-cloud/tg-history не работают"))
    waiting = outbox_size(sd)
    results.append((waiting < 20, f"Ответов в очереди на запись в память: {waiting}"))
    if offline or not tok:
        return results
    try:
        from .cloudcli import fingerprint, project_state, webhook_state
        conf = call("config_status", environ=environ)
        remote = (conf.get("config") or {}).get("routine_key") or {}
        match = bool(cfg.routine_key) and remote.get("sha256_8") == fingerprint(cfg.routine_key)
        results.append((bool(conf.get("ready")), "Настройки в Serverless заданы" if conf.get("ready")
                        else "Serverless не настроен — bin/tg-cloud setup"))
        results.append((match, f"Ключ рутины в Serverless: {remote.get('length')} симв., совпадает с боксом"
                        if match else "Ключ рутины в Serverless не совпадает с боксом — bin/tg-cloud setup"))
        st = call("stats", environ=environ)
        dead = st["deliveries"].get("dead", 0)
        pending = st["deliveries"].get("pending", 0)
        results.append((dead == 0, "Недоставленных сообщений нет" if dead == 0
                        else f"Недоставленных сообщений: {dead} (bin/tg-redeliver)"))
        results.append((pending == 0, f"Доставок в процессе: {pending}"))
        last = st.get("last")
        if last:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last.get("updated") or 0))
            results.append((last.get("status") in ("delivered", "redelivered"),
                            f"Последняя передача боту: {last.get('status')} (HTTP {last.get('http_status')}) в {when}"))
        w = webhook_state()
        results.append((bool(w.get("url_set")) and bool(w.get("in_sync")),
                        f"tgcloud webhook: {'установлен' if w.get('url_set') else 'НЕ установлен'}, "
                        f"{'in sync' if w.get('in_sync') else 'OUT OF SYNC (bin/tg-cloud webhook-sync)'}"))
        ps = project_state()
        results.append((ps["in_sync_with_cloud"], "Код в облаке совпадает с ./serverless" if ps["in_sync_with_cloud"]
                        else "Код в облаке отличается от ./serverless (bin/tg-cloud deploy)"))
    except CloudError as e:
        results.append((False, f"Serverless недоступен: {e}"))
    results += asyncio.run(_telegram_webhook_checks(cfg, bot))
    return results


def run_checks(offline: bool = False, environ=None, bot=None) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    try:
        cfg = load_config(need_routine=True, environ=environ)
        src = cfg.secret_sources.get("ROUTINE_KEY", "?")
        results.append((True, f"Настройки и права на файл секретов в порядке (config ok; "
                              f"ключ рутины: {src}, {len(cfg.routine_key or '')} симв.)"))
    except ConfigError as e:
        results.append((False, f"Ошибка настроек (config): {e}"))
        return results
    from .cloud import get_mode
    if get_mode(cfg.state_dir) == "serverless":
        return results + serverless_checks(cfg, offline, environ, bot)
    results.append((True, "Режим приёма: box-relay (long polling, fallback)"))
    sd = cfg.state_dir
    procs = supervisor_status(sd) if sd.exists() else None
    if procs is None:
        results.append((False, "Сторож (supervisord) не запущен — запустите bin/tg-ensure"))
    else:
        results.append((True, "Сторож (supervisord) работает"))
        wd = procs.get("tg-watchdog", {})
        results.append((wd.get("statename") == "RUNNING",
                        f"Проверка зависаний (tg-watchdog): {wd.get('statename', 'нет')}"))
        br = procs.get(PROGRAM, {})
        results.append((br.get("statename") == "RUNNING",
                        f"Процесс моста под надзором: {br.get('statename', 'нет')}"
                        + (f", pid {br.get('pid')}" if br.get("pid") else "")))
    running = sd.exists() and SingleInstanceLock.is_held(sd)
    results.append((running, "Мост запущен (relay running)" if running else "Мост НЕ запущен (relay not running)"))
    hb = read_heartbeat(sd / "heartbeat")
    if hb is None:
        results.append((False, "Сигнала жизни ещё не было (no heartbeat yet)"))
    else:
        age = time.monotonic() - hb[1]  # monotonic: a VM freeze is not a hang
        results.append((0 <= age < cfg.watchdog_seconds,
                        f"Последний сигнал жизни {int(age)} с назад (порог {cfg.watchdog_seconds} с)"))
    st = sd / "state.json"
    if st.exists():
        try:
            ld = json.loads(st.read_text()).get("last_delivery")
        except ValueError:
            ld = None
        if ld:
            ok = ld.get("status") == "ok"
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ld.get("ts", 0)))
            results.append((ok, f"Последняя передача боту: {ld.get('status')} (HTTP {ld.get('http')}) в {when}"))
    dead = list_deadletters(sd)
    results.append((not dead, "Недоставленных сообщений нет" if not dead else
                    f"Недоставленных сообщений: {len(dead)} (в {sd / 'deadletter'}; bin/tg-redeliver)"))
    if not offline:
        results += asyncio.run(_telegram_checks(cfg, bot))
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tg-health")
    p.add_argument("--offline", action="store_true", help="do not contact Telegram")
    p.add_argument("--json", action="store_true")
    p.add_argument("--print-state-dir", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a.print_state_dir:
        print(state_dir_only())
        return 0
    res = run_checks(a.offline)
    ok = all(r[0] for r in res)
    if a.json:
        print(json.dumps({"ok": ok, "checks": [{"ok": r[0], "detail": r[1]} for r in res]}, ensure_ascii=False))
    else:
        for good, text in res:
            print(("✅ " if good else "❌ ") + text)
        print("ИТОГ: всё в порядке" if ok else "ИТОГ: есть проблемы (см. ❌ выше)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
