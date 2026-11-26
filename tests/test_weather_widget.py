"""Weather widget (Bot API 10.4 sendWidget): data packing, limits, send/update."""

import asyncio
import json
import sys
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tgbridge.widgets import weather as w

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "meteoblue_kyiv.json"
NOW = datetime(2026, 11, 24, 12, 40, tzinfo=w.TZ)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_widget(self, **kw):
        self.calls.append(("sendWidget", kw))
        return SimpleNamespace(message_id=4242)

    async def edit_message_widget_data(self, **kw):
        self.calls.append(("editMessageWidgetData", kw))
        return True

    async def send_photo(self, **kw):
        self.calls.append(("sendPhoto", kw))
        return SimpleNamespace(message_id=4343)

    async def edit_message_caption(self, **kw):
        self.calls.append(("editMessageCaption", kw))
        return True


def cfg(tmp):
    return SimpleNamespace(allowed_user_ids=frozenset({7}), owner_id=7, secrets=["x" * 40],
                           state_dir=Path(tmp), token="t", api_base="https://api.telegram.org")


class WeatherWidgetTest(unittest.TestCase):
    def setUp(self):
        self.mb = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.state = w.build_state(self.mb, NOW)

    def test_state_shape_and_size(self):
        s = self.state
        self.assertEqual(s["h0"], 12)
        self.assertEqual(len(s["h"]), 24)
        self.assertEqual(len(s["d"]), 7)
        self.assertTrue(all(len(r) == 6 for r in s["h"]))
        self.assertTrue(all(len(r) == 8 for r in s["d"]))
        self.assertLessEqual(len(w.state_json(s).encode()), w.DATA_MAX)

    def test_hourly_precipitation_probability(self):
        # h row: [icon, temp, mm*10, wind, humidity, probability]
        probs = self.mb["data_1h"]["precipitation_probability"]
        start = self.mb["data_1h"]["time"].index(NOW.strftime("%Y-%m-%d %H:00"))
        self.assertEqual([r[5] for r in self.state["h"]], [round(v) for v in probs[start:start + 24]])
        mb = json.loads(json.dumps(self.mb))
        del mb["data_1h"]["precipitation_probability"]
        self.assertTrue(all(r[5] == 0 for r in w.build_state(mb, NOW)["h"]))

    def test_layout_order(self):
        # owner: temperature + humidity %, then icon, then rain probability, then wind
        html = w.render_html()
        hourly = html[html.index("H.forEach"):html.index("D.d.forEach")]
        daily = html[html.index("D.d.forEach"):html.index("A.innerHTML=s")]
        for part in (hourly, daily):
            pos = [part.index(k) for k in ("tf(r[1])", "💧", "ic(r[0])", "☔", "м/с")]
            self.assertEqual(pos, sorted(pos), part)

    def test_no_forbidden_fields(self):
        # feels-like, pressure, UV and wind direction are not shown (owner's choice)
        data = w.state_json(self.state)
        for word in ("felt", "pressure", "uv", "direction"):
            self.assertNotIn(word, data)

    def test_html_within_limits(self):
        html = w.render_html()
        raw = html.encode()
        self.assertLessEqual(len(raw), 32768)
        self.assertLessEqual(len(zlib.compress(raw, 9)), 4096)
        self.assertNotIn("\x00", html)
        self.assertIn("meteoblue", html)
        for bad in ("http://", "<script src", "@import", "@font-face", "fetch(", " vh", "<input"):
            self.assertNotIn(bad, html)

    def test_pictocode_maps_complete(self):
        self.assertEqual(set(w.HOURLY_ICON), set(range(1, 36)))
        self.assertEqual(set(w.DAILY_ICON), set(range(1, 18)))
        self.assertEqual(len(w.ICON_TEXT), 15)

    def test_default_city_is_kyiv(self):
        self.assertEqual(self.state["c"], "Киев")
        self.assertEqual(self.state["l"], "kyiv_ukraine_703448")
        c = w.resolve_city()
        self.assertEqual((c["lat"], c["lon"], c["asl"]), (50.45, 30.52, 179))
        self.assertEqual(w.resolve_city("kyiv")["name"], "Киев")
        custom = w.resolve_city("x", name="Тест", lat=1.0, lon=2.0)
        self.assertEqual((custom["name"], custom["lat"]), ("Тест", 1.0))
        with self.assertRaises(w.WidgetError):
            w.resolve_city("atlantis")

    def test_no_range_bar(self):
        html = w.render_html()
        self.assertNotIn('class="k"', html)
        self.assertNotIn("Киев", html)

    def test_caption(self):
        c = w.caption(self.state)
        self.assertIn("Киев", c)
        self.assertIn("meteoblue", c)
        self.assertLessEqual(len(c), 1024)

    def test_send_and_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, bot = cfg(tmp), FakeBot()
            data, html = w.state_json(self.state), w.render_html()
            res = asyncio.run(w._send(c, html, data, "cap", 7, None, bot))
            self.assertTrue(res["ok"])
            self.assertEqual(res["message_id"], 4242)
            name, kw = bot.calls[0]
            self.assertEqual(name, "sendWidget")
            self.assertEqual(kw["public_data"], data)
            res = asyncio.run(w._update(c, data, "cap2", bot))
            self.assertEqual(res["message_id"], 4242)
            self.assertEqual(bot.calls[1][0], "editMessageWidgetData")

    def test_refuses_non_allowlisted_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = FakeBot()
            with self.assertRaises(w.WidgetError):
                asyncio.run(w._send(cfg(tmp), "<p>x</p>", "{}", "c", 999, None, bot))
            self.assertEqual(bot.calls, [])

    def test_not_allowed_is_distinct_and_not_retried(self):
        class Refusing(FakeBot):
            async def send_widget(self, **kw):
                self.calls.append(("sendWidget", kw))
                raise RuntimeError("Telegram server says - Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED")
        with tempfile.TemporaryDirectory() as tmp:
            bot = Refusing()
            with self.assertRaises(w.WidgetNotAllowed):
                asyncio.run(w._send(cfg(tmp), "<p>x</p>", "{}", "c", 7, None, bot))
            self.assertEqual(len(bot.calls), 1)
            png = Path(tmp) / "p.png"
            png.write_bytes(b"\x89PNG")
            res = asyncio.run(w._send_photo(cfg(tmp), png, "c", 7, None, bot))
            self.assertEqual(res["message_id"], 4343)


if __name__ == "__main__":
    unittest.main()
