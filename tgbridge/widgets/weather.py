"""7-day weather (default city: Kyiv) as a Telegram HTML widget (Bot API 10.4 sendWidget).

How it works (https://core.telegram.org/bots/widgets):
* A widget is fixed code + changeable state. The code (weather.html: CSS, inline
  SVG icons, a small renderer in JS) is sent once and never changes. The forecast
  itself goes into the widget's ``public_data`` (<= 1024 bytes of JSON), so a later
  ``update`` only calls editMessageWidgetData - no new message, no new HTML.
* The widget has no network access, so this module fetches meteoblue
  (packages basic-1h + basic-day) and packs the numbers.
* Limits checked before sending: HTML <= 32768 bytes and <= 4096 bytes after
  zlib level 9 (the server counts exactly like Python's zlib); state <= 1024 bytes.

Secrets: the meteoblue key comes from METEOBLUE_API_KEY and is only used in the
request URL; neither the key nor the bot token is ever printed, logged or put into
the widget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from ..tz import kyiv_tz

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "weather.html"
PROJECT = HERE.parent.parent
DEFAULT_PREVIEW = PROJECT / "widgets" / "preview.png"
TZ = kyiv_tz()

# City presets. Coordinates/elevation: GeoNames (via the Open-Meteo geocoding API)
# and the meteoblue location search agree; "slug" is the meteoblue page
# https://www.meteoblue.com/ru/weather/week/<slug> (attribution link).
CITIES = {
    "kyiv": {"name": "Киев", "lat": 50.45, "lon": 30.52, "asl": 179,
             "slug": "kyiv_ukraine_703448"},
}
DEFAULT_CITY = "kyiv"


def resolve_city(key: str | None = None, **over) -> dict:
    """Preset from --city / env TG_WEATHER_CITY (default kyiv); --lat etc. override."""
    key = (key or os.environ.get("TG_WEATHER_CITY") or DEFAULT_CITY).strip().lower()
    if key not in CITIES and not (over.get("lat") is not None and over.get("lon") is not None):
        raise WidgetError(f"unknown city {key!r}; presets: {', '.join(CITIES)} (or pass --lat/--lon)")
    city = dict(CITIES.get(key, {"name": key.title(), "asl": None, "slug": ""}))
    city.update({k: v for k, v in over.items() if v is not None})
    return city
MB_URL = "https://my.meteoblue.com/packages/basic-1h_basic-day"

HTML_MAX = 32768
ZIP_MAX = 4096
DATA_MAX = 1024

# meteoblue pictocodes -> widget icon index (see P/T in weather.html).
# https://content.meteoblue.com/en/research-education/specifications/standards/symbols-and-pictograms
# 0 clear, 1 mostly clear, 2 partly cloudy, 3 mostly cloudy, 4 overcast, 5 fog,
# 6 light rain, 7 rain, 8 showers, 9 thunderstorm, 10 light snow, 11 snow,
# 12 snow showers, 13 sleet, 14 haze.  +20 = night (moon instead of sun).
HOURLY_ICON = {
    1: 0, 2: 0, 3: 0, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 2, 11: 2, 12: 2,
    13: 14, 14: 14, 15: 14, 16: 5, 17: 5, 18: 5, 19: 3, 20: 3, 21: 3, 22: 4,
    23: 7, 24: 11, 25: 7, 26: 11, 27: 9, 28: 9, 29: 11, 30: 9, 31: 8, 32: 12,
    33: 6, 34: 10, 35: 13,
}
DAILY_ICON = {
    1: 0, 2: 1, 3: 2, 4: 4, 5: 5, 6: 7, 7: 8, 8: 9, 9: 11, 10: 12, 11: 13,
    12: 6, 13: 10, 14: 7, 15: 11, 16: 6, 17: 10,
}
ICON_TEXT = ["Ясно", "Малооблачно", "Переменная облачность", "Облачно", "Пасмурно", "Туман",
             "Небольшой дождь", "Дождь", "Ливни", "Гроза", "Небольшой снег", "Снег",
             "Снежные заряды", "Мокрый снег", "Дымка"]
ICON_EMOJI = ["☀️", "🌤", "⛅", "🌥", "☁️", "🌫", "🌦", "🌧", "🌦", "⛈", "🌨", "❄️", "🌨", "🌨", "🌫"]


class WidgetError(Exception):
    pass


class WidgetNotAllowed(WidgetError):
    """Telegram refused widgets for this bot (INPUT_MEDIA_WIDGET_NOT_ALLOWED).

    Since 2026-11-13 general bots may not send widgets even in private chats:
    https://github.com/tdlib/telegram-bot-api/issues/947 (levlam, Telegram).
    Not retried; ``send`` falls back to the PNG preview as a photo.
    """


NOT_ALLOWED_NOTE = ("\n\n⚠️ Telegram пока не разрешает обычным ботам отправлять виджеты "
                    "(INPUT_MEDIA_WIDGET_NOT_ALLOWED), поэтому это картинка-превью виджета.")


def fetch_meteoblue(api_key: str, city: dict, timeout: float = 20) -> dict:
    if not api_key:
        raise WidgetError("METEOBLUE_API_KEY is not set")
    params = {"lat": city["lat"], "lon": city["lon"]}
    if city.get("asl") is not None:
        params["asl"] = city["asl"]
    q = urllib.parse.urlencode({**params, "format": "json", "apikey": api_key})
    try:
        with urllib.request.urlopen(f"{MB_URL}?{q}", timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:  # never echo the URL (it holds the key)
        body = e.read()[:300].decode("utf-8", "replace").replace(api_key, "<redacted>")
        raise WidgetError(f"meteoblue HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WidgetError(f"meteoblue unreachable: {type(e).__name__}") from None
    if "error_message" in data or "data_1h" not in data or "data_day" not in data:
        raise WidgetError(f"meteoblue error: {str(data.get('error_message', 'bad response'))[:200]}")
    return data


def _i(v) -> int:
    return int(round(float(v)))


def build_state(mb: dict, now: datetime | None = None, city: dict | None = None) -> dict:
    """Pack the forecast into a compact JSON for public_data (<= 1024 bytes).

    h: [icon, temp, precip*10 mm, wind m/s, humidity %, precip probability %] per hour
    d: [icon, tmin, tmax, precip*10 mm, precip probability %, wind min, wind max, humidity max %]
    """
    now = (now or datetime.now(TZ)).astimezone(TZ)
    h1, dd = mb["data_1h"], mb["data_day"]
    key = now.strftime("%Y-%m-%d %H:00")
    times = h1["time"]
    start = times.index(key) if key in times else 0
    # hourly rain probability (%); older/partial responses may lack it -> 0
    prob = h1.get("precipitation_probability") or []
    hours = []
    for i in range(start, min(start + 24, len(times))):
        icon = HOURLY_ICON.get(int(h1["pictocode"][i]), 3) + (0 if h1["isdaylight"][i] else 20)
        hours.append([icon, _i(h1["temperature"][i]), _i(float(h1["precipitation"][i]) * 10),
                      _i(h1["windspeed"][i]), _i(h1["relativehumidity"][i]),
                      _i(prob[i] if i < len(prob) and prob[i] is not None else 0)])
    today = now.strftime("%Y-%m-%d")
    d0 = dd["time"].index(today) if today in dd["time"] else 0
    days = []
    for i in range(d0, min(d0 + 7, len(dd["time"]))):
        days.append([DAILY_ICON.get(int(dd["pictocode"][i]), 3),
                     _i(dd["temperature_min"][i]), _i(dd["temperature_max"][i]),
                     _i(float(dd["precipitation"][i]) * 10), _i(dd["precipitation_probability"][i]),
                     _i(dd["windspeed_min"][i]), _i(dd["windspeed_max"][i]),
                     _i(dd["relativehumidity_max"][i])])
    if not hours or not days:
        raise WidgetError("meteoblue returned no forecast rows")
    city = city or CITIES[DEFAULT_CITY]
    return {"c": city["name"], "l": city.get("slug") or "", "u": now.strftime("%d.%m %H:%M"), "d0": dd["time"][d0], "h0": int(times[start][11:13]),
            "h": hours, "d": days}


def state_json(state: dict) -> str:
    s = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    if len(s.encode()) > DATA_MAX:
        raise WidgetError(f"widget state is {len(s.encode())} bytes, limit {DATA_MAX}")
    return s


def render_html() -> str:
    """The widget code. Lines are stripped; content is otherwise untouched."""
    lines = (ln.strip() for ln in TEMPLATE.read_text(encoding="utf-8").splitlines())
    return "\n".join(ln for ln in lines if ln)


def sizes(html: str) -> dict:
    raw = html.encode("utf-8")
    return {"html_bytes": len(raw), "html_limit": HTML_MAX,
            "zlib9_bytes": len(zlib.compress(raw, 9)), "zlib9_limit": ZIP_MAX}


def check_html(html: str) -> dict:
    s = sizes(html)
    if "\x00" in html:
        raise WidgetError("widget HTML contains NUL")
    if s["html_bytes"] > HTML_MAX or s["zlib9_bytes"] > ZIP_MAX:
        raise WidgetError(f"widget too large: {s}")
    return s


def caption(state: dict) -> str:
    now = state["h"][0]
    icon = now[0] % 20
    d = state["d"][0]

    def tf(t: int) -> str:
        return f"{'+' if t > 0 else '−' if t < 0 else ''}{abs(t)}°"
    return (f"{ICON_EMOJI[icon]} {state.get('c', '')} сейчас {tf(now[1])}, {ICON_TEXT[icon].lower()}. "
            f"Сегодня {tf(d[1])}…{tf(d[2])}. Прогноз на 7 дней · данные meteoblue")


# ---------------------------------------------------------------- preview

STUB = """<style>:root{--tg-theme-bg-color:#fff;--tg-theme-text-color:#000;--tg-theme-hint-color:#707579}
html{background:#d9e6c3}body{width:%dpx;margin:16px auto!important;border-radius:16px}</style>
<script>window.TelegramWidget={publicData:%s,chatData:null,onEvent(){},ready(){},themeParams:{},colorScheme:'light'};</script>
"""


def render_preview(html: str, state: str, out: Path, width: int = 360, height: int = 700) -> Path:
    """Local look only (a browser draws more than Telegram's widget profile)."""
    chrome = next((shutil.which(c) for c in ("google-chrome", "chromium", "chromium-browser")
                   if shutil.which(c)), None)
    if not chrome:
        raise WidgetError("no headless Chrome/Chromium for the preview")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "preview.html"
        page.write_text("<!doctype html><meta charset=utf-8>" + (STUB % (width, json.dumps(state)))
                        + html, encoding="utf-8")
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=2", f"--window-size={width + 32},{height}",
                        f"--screenshot={out}", page.as_uri()],
                       check=True, capture_output=True, timeout=60)
    return out


# ---------------------------------------------------------------- Telegram

def _state_file(cfg) -> Path:
    return Path(cfg.state_dir) / "weather_widget.json"


async def _send(cfg, html: str, data: str, cap: str, chat_id: int, thread: int | None, bot=None) -> dict:
    from ..log import Redactor
    from ..tgapi import api_retry, make_bot
    if chat_id not in cfg.allowed_user_ids:
        raise WidgetError(f"chat {chat_id} is not allowlisted")
    red = Redactor(cfg.secrets + [os.environ.get("METEOBLUE_API_KEY", "")])
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base)
    try:
        async for attempt in api_retry():
            with attempt:
                msg = await bot.send_widget(chat_id=chat_id, html=html, public_data=data,
                                            caption=cap, message_thread_id=thread)
    except Exception as e:  # noqa: BLE001
        if "WIDGET_NOT_ALLOWED" in str(e):
            raise WidgetNotAllowed(red(f"sendWidget refused: {e}")) from None
        raise WidgetError(red(f"sendWidget failed: {e}")) from None
    finally:
        if own:
            await bot.session.close()
    rec = {"chat_id": chat_id, "message_id": msg.message_id, "thread": thread,
           "sent_at": datetime.now(TZ).isoformat(timespec="seconds")}
    p = _state_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return {"ok": True, "method": "sendWidget", **rec}


async def _send_photo(cfg, png: Path, cap: str, chat_id: int, thread: int | None, bot=None) -> dict:
    """Fallback when widgets are refused: the preview PNG as an ordinary photo."""
    from aiogram.types import FSInputFile
    from ..log import Redactor
    from ..tgapi import api_retry, make_bot
    if chat_id not in cfg.allowed_user_ids:
        raise WidgetError(f"chat {chat_id} is not allowlisted")
    red = Redactor(cfg.secrets + [os.environ.get("METEOBLUE_API_KEY", "")])
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base)
    try:
        async for attempt in api_retry():
            with attempt:
                msg = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(str(png)),
                                           caption=cap[:1024], message_thread_id=thread)
    except Exception as e:  # noqa: BLE001
        raise WidgetError(red(f"sendPhoto failed: {e}")) from None
    finally:
        if own:
            await bot.session.close()
    return {"method": "sendPhoto", "chat_id": chat_id, "message_id": msg.message_id}


async def _update(cfg, data: str, cap: str, bot=None) -> dict:
    from ..log import Redactor
    from ..tgapi import api_retry, make_bot
    p = _state_file(cfg)
    if not p.exists():
        raise WidgetError("no widget sent yet; run `send` first")
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec["chat_id"] not in cfg.allowed_user_ids:
        raise WidgetError("stored chat is not allowlisted")
    red = Redactor(cfg.secrets + [os.environ.get("METEOBLUE_API_KEY", "")])
    own = bot is None
    bot = bot or make_bot(cfg.token, cfg.api_base)
    try:
        async for attempt in api_retry():
            with attempt:
                await bot.edit_message_widget_data(chat_id=rec["chat_id"], message_id=rec["message_id"],
                                                   public_data=data)
        try:
            await bot.edit_message_caption(chat_id=rec["chat_id"], message_id=rec["message_id"], caption=cap)
        except Exception as e:  # noqa: BLE001 - "message is not modified" is fine
            if "not modified" not in str(e):
                raise
    except Exception as e:  # noqa: BLE001
        raise WidgetError(red(f"update failed: {e}")) from None
    finally:
        if own:
            await bot.session.close()
    return {"ok": True, "method": "editMessageWidgetData", "chat_id": rec["chat_id"],
            "message_id": rec["message_id"]}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tg-weather-widget", description="Weather Telegram widget")
    ap.add_argument("--city", help=f"preset: {', '.join(CITIES)} (env TG_WEATHER_CITY; default {DEFAULT_CITY})")
    ap.add_argument("--name", help="city name shown in the widget")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--asl", type=int, help="elevation, m")
    ap.add_argument("--slug", help="meteoblue page slug, e.g. kyiv_ukraine_703448")
    ap.add_argument("--fixture", help="meteoblue JSON file instead of a live request (tests, offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="build the widget, print sizes, write HTML/preview")
    r.add_argument("--html-out")
    r.add_argument("--preview", nargs="?", const=str(DEFAULT_PREVIEW))
    r.add_argument("--width", type=int, default=360)
    s = sub.add_parser("send", help="send a new widget message (owner chat by default)")
    s.add_argument("--chat", type=int)
    s.add_argument("--thread", type=int)
    s.add_argument("--preview", nargs="?", const=str(DEFAULT_PREVIEW))
    s.add_argument("--no-fallback", action="store_true",
                   help="if Telegram refuses widgets, do not send the preview PNG as a photo")
    sub.add_parser("update", help="refresh the forecast in the last sent widget (editMessageWidgetData)")
    return ap


def main(argv: list[str] | None = None, bot=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        city = resolve_city(args.city, name=args.name, lat=args.lat, lon=args.lon,
                            asl=args.asl, slug=args.slug)
        if args.fixture:
            mb = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        else:
            mb = fetch_meteoblue(os.environ.get("METEOBLUE_API_KEY", ""), city)
        state = build_state(mb, city=city)
        data = state_json(state)
        html = render_html()
        info = {"sizes": check_html(html), "state_bytes": len(data.encode()), "state_limit": DATA_MAX}
        cap = caption(state)
        if args.cmd in ("render", "send") and getattr(args, "preview", None):
            info["preview"] = str(render_preview(html, data, Path(args.preview),
                                                 getattr(args, "width", 360)))
        if args.cmd == "render":
            if args.html_out:
                Path(args.html_out).write_text(html, encoding="utf-8")
            result = {"ok": True, "caption": cap, **info}
        else:
            from ..config import ConfigError, load_config
            try:
                cfg = load_config(need_routine=False)
            except ConfigError as e:
                raise WidgetError(f"config: {e}") from None
            if args.cmd == "send":
                chat = args.chat or cfg.owner_id
                try:
                    res = asyncio.run(_send(cfg, html, data, cap, chat, args.thread, bot))
                except WidgetNotAllowed as e:
                    if args.no_fallback:
                        raise
                    png = Path(info.get("preview") or render_preview(html, data, DEFAULT_PREVIEW))
                    photo = asyncio.run(_send_photo(cfg, png, cap + NOT_ALLOWED_NOTE, chat, args.thread, bot))
                    print(json.dumps({"ok": False, "error": str(e), "fallback": photo, **info},
                                     ensure_ascii=False))
                    return 3
            else:
                res = asyncio.run(_update(cfg, data, cap, bot))
            result = {**res, **info}
    except WidgetError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
