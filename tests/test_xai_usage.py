"""Offline tests for bin/xai-usage (fixture JSON, no real key)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_USD = Path(__file__).resolve().parent / "fixtures" / "xai_usage_usd.json"
FIXTURE_UNITS = Path(__file__).resolve().parent / "fixtures" / "xai_usage_units.json"
SCRIPT = ROOT / "bin" / "xai-usage"
FAKE_TEAM = "11111111-2222-3333-4444-555555555555"
FAKE_KEY = "xai-test-not-a-real-key-AAAAAAAA"


def load_mod():
    # bin/xai-usage has no .py suffix, so importlib may not pick a loader.
    import types
    mod = types.ModuleType("xai_usage")
    mod.__file__ = str(SCRIPT)
    code = SCRIPT.read_text(encoding="utf-8")
    exec(compile(code, str(SCRIPT), "exec"), mod.__dict__)
    return mod


class PointDateTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_utc_midnight_instant_is_converted_to_kiev_date(self):
        # Winter (UTC+2): 2026-11-21T22:00:00Z is midnight on 22 Nov in Kiev.
        self.assertEqual(self.m.point_date("2026-11-21T22:00:00Z"), date(2026, 11, 22))
        self.assertEqual(self.m.point_date("2026-11-22T22:00:00Z"), date(2026, 11, 23))
        # Taking the UTC prefix would be wrong:
        self.assertNotEqual(self.m.point_date("2026-11-21T22:00:00Z"), date(2026, 11, 21))
        self.assertNotEqual("2026-11-21T22:00:00Z"[:10], "2026-11-22")

    def test_summer_dst_offset(self):
        # EEST (UTC+3): 2026-10-24T21:00:00Z is midnight on 25 Oct in Kiev.
        self.assertEqual(self.m.point_date("2026-10-24T21:00:00Z"), date(2026, 10, 25))


class ParseFixtureTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()
        self.usd = json.loads(FIXTURE_USD.read_text(encoding="utf-8"))
        self.units = json.loads(FIXTURE_UNITS.read_text(encoding="utf-8"))
        self.today = date(2026, 11, 24)
        _, self.days10, _, _ = self.m.date_window(
            datetime(2026, 11, 24, 13, 0, tzinfo=ZoneInfo("Europe/Kyiv")))

    def test_chart_window_is_ten_days_including_today(self):
        self.assertEqual(self.days10[0], date(2026, 11, 15))
        self.assertEqual(self.days10[-1], date(2026, 11, 24))
        self.assertEqual(len(self.days10), 10)

    def test_daily_usd_lands_on_kiev_dates_not_utc_prefix(self):
        rows = {r["date"]: r["usd"] for r in self.m.daily_usd(self.usd, self.days10)}
        self.assertEqual(rows["2026-11-22"], 2.26)
        self.assertEqual(rows["2026-11-23"], 1.58)
        self.assertEqual(rows["2026-11-21"], 0.0)  # would be 2.26 if we sliced [:10]
        self.assertEqual(rows["2026-11-24"], 0.0)
        self.assertEqual(rows["2026-11-15"], 0.0)

    def test_last_three_days_by_token_type(self):
        br = self.m.breakdown(self.usd, self.units, self.days10[-3:])
        self.assertEqual(br["input"]["tokens"], 1_285_000)
        self.assertEqual(br["input"]["usd"], 1.37)
        self.assertEqual(br["output"]["tokens"], 113_000)
        self.assertEqual(br["output"]["usd"], 0.33)
        self.assertEqual(br["cache"]["tokens"], 11_490_000)
        self.assertEqual(br["cache"]["usd"], 2.10)
        self.assertEqual(br["other_usd"], 0.04)
        self.assertEqual(br["total_tokens"], 12_888_000)
        self.assertEqual(br["total_usd"], 3.84)
        self.assertEqual(br["input"]["tokens_text"], "1.3 млн")
        self.assertEqual(br["output"]["tokens_text"], "0.1 млн")
        self.assertEqual(br["cache"]["tokens_text"], "11.5 млн")
        self.assertEqual(br["total_tokens_text"], "12.9 млн токенов")
        self.assertIn("Итого за 3 дня:", br["total_text"])
        self.assertIn("$3.84", br["total_text"])

    def test_non_token_units_excluded_from_token_total(self):
        # Generated image + file storage are in the units fixture but not tokens.
        br = self.m.breakdown(self.usd, self.units, self.days10[-3:])
        self.assertNotEqual(br["total_tokens"], 12_888_000 + 1 + 100)

    def test_classify_unit(self):
        self.assertEqual(self.m.classify_unit("Prompt text tokens"), "input")
        self.assertEqual(self.m.classify_unit("Prompt image tokens"), "input")
        self.assertEqual(self.m.classify_unit("Cached prompt text tokens"), "cache")
        self.assertEqual(self.m.classify_unit("Completion text tokens"), "output")
        self.assertEqual(self.m.classify_unit("Reasoning text tokens"), "output")
        self.assertEqual(self.m.classify_unit("Generated image"), "other")
        self.assertEqual(self.m.classify_unit("File Storage (MB-minute)"), "other")


class TeamIdTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"XAI_TEAM_ID": FAKE_TEAM}, clear=False):
            self.assertEqual(self.m.team_id(), FAKE_TEAM)

    def test_placeholder_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"XAI_TEAM_ID": "<TEAM_ID>"}, clear=False):
            # May fall through to a local file; must not return the placeholder.
            self.assertNotEqual(self.m.team_id(), "<TEAM_ID>")

    def test_local_config_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "xai-team-id"
            cfg.write_text("# comment\n" + FAKE_TEAM + "\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "XAI_TEAM_ID"}
            with mock.patch.dict(os.environ, env, clear=True):
                os.environ.pop("XAI_TEAM_ID", None)
                self.assertEqual(self.m.team_id(files=[cfg]), FAKE_TEAM)

    def test_config_files_skip_placeholder_and_missing(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "empty"
            a.write_text("# only comment\n<TEAM_ID>\n", encoding="utf-8")
            missing = Path(td) / "no-such-file"
            home_style = Path(td) / "team-id"
            home_style.write_text(FAKE_TEAM + "\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "XAI_TEAM_ID"}
            with mock.patch.dict(os.environ, env, clear=True):
                os.environ.pop("XAI_TEAM_ID", None)
                self.assertEqual(self.m.team_id(files=[a, missing, home_style]), FAKE_TEAM)
                self.assertEqual(self.m.team_id(files=[a, missing]), "")

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "xai-team-id"
            cfg.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XAI_TEAM_ID": FAKE_TEAM}, clear=False):
                self.assertEqual(self.m.team_id(files=[cfg]), FAKE_TEAM)

    def test_default_team_id_is_placeholder(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(self.m.DEFAULT_TEAM_ID, "<TEAM_ID>")
        self.assertIn('DEFAULT_TEAM_ID = "<TEAM_ID>"', src)


class MainGuardTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def _run(self, env):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            # Drop leftover team/key if we intend them absent
            to_clear = [k for k in ("XAI_MANAGEMENT_KEY", "XAI_TEAM_ID") if k not in env]
            missing = {k: "" for k in to_clear}
            merged = {**{k: v for k, v in os.environ.items() if k not in to_clear}, **env, **missing}
            with mock.patch.dict(os.environ, merged, clear=True):
                with mock.patch.object(sys, "stdout", buf):
                    with self.assertRaises(SystemExit) as ex:
                        self.m.main()
        out = buf.getvalue()
        self.assertNotIn(FAKE_KEY, out)
        return json.loads(out), ex.exception.code

    def test_missing_key(self):
        env = {k: v for k, v in os.environ.items() if k != "XAI_MANAGEMENT_KEY"}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ.pop("XAI_MANAGEMENT_KEY", None)
            with mock.patch.object(sys, "stdout", buf):
                with self.assertRaises(SystemExit):
                    self.m.main()
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "needs_management_key")
        self.assertEqual(payload["api_note"], "нужен management-ключ")
        self.assertNotIn(FAKE_KEY, buf.getvalue())

    def test_output_never_contains_key_even_on_success_path(self):
        usd = json.loads(FIXTURE_USD.read_text(encoding="utf-8"))
        units = json.loads(FIXTURE_UNITS.read_text(encoding="utf-8"))

        class Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            # never return the key
            if url.endswith("/prepaid/balance"):
                return Resp(200, {"total": "0"})
            if url.endswith("/usage"):
                body = json.loads(req.data.decode())
                name = body["analyticsRequest"]["values"][0]["name"]
                return Resp(200, usd if name == "usd" else units)
            raise AssertionError("unexpected url")

        now = datetime(2026, 11, 24, 13, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        buf = io.StringIO()
        env = {**os.environ, "XAI_MANAGEMENT_KEY": FAKE_KEY, "XAI_TEAM_ID": FAKE_TEAM}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                with mock.patch.object(sys, "stdout", buf):
                    with self.assertRaises(SystemExit) as ex:
                        self.m.main(now=now)
        raw = buf.getvalue()
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn("Authorization", raw)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(ex.exception.code, 0)
        by_date = {r["date"]: r["usd"] for r in payload["api_days"]}
        self.assertEqual(by_date["2026-11-22"], 2.26)
        self.assertEqual(by_date["2026-11-23"], 1.58)
        self.assertEqual(payload["api_breakdown"]["total_usd"], 3.84)
        self.assertEqual(payload["api_balance_text"], "$0.00")
        self.assertEqual(payload["api_days"][0]["date"], "2026-11-15")
        self.assertEqual(len(payload["api_days"]), 10)



class DaysWordTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()

    def test_russian_plurals(self):
        self.assertEqual(self.m.days_word(1), "день")
        self.assertEqual(self.m.days_word(2), "дня")
        self.assertEqual(self.m.days_word(3), "дня")
        self.assertEqual(self.m.days_word(4), "дня")
        self.assertEqual(self.m.days_word(5), "дней")
        self.assertEqual(self.m.days_word(11), "дней")
        self.assertEqual(self.m.days_word(14), "дней")
        self.assertEqual(self.m.days_word(21), "день")
        self.assertEqual(self.m.days_word(22), "дня")


class PartialAndBalanceTest(unittest.TestCase):
    def setUp(self):
        self.m = load_mod()
        self.usd = json.loads(FIXTURE_USD.read_text(encoding="utf-8"))
        self.units = json.loads(FIXTURE_UNITS.read_text(encoding="utf-8"))
        self.now = datetime(2026, 11, 24, 13, 0, tzinfo=ZoneInfo("Europe/Kyiv"))

    def _run(self, request_fn):
        buf = io.StringIO()
        env = {**os.environ, "XAI_MANAGEMENT_KEY": FAKE_KEY, "XAI_TEAM_ID": FAKE_TEAM}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(self.m, "_request", request_fn):
                with mock.patch.object(sys, "stdout", buf):
                    with self.assertRaises(SystemExit) as ex:
                        self.m.main(now=self.now)
        raw = buf.getvalue()
        self.assertNotIn(FAKE_KEY, raw)
        return json.loads(raw), ex.exception.code

    def test_usd_failure_omits_chart_keeps_tokens_and_sets_note(self):
        def req(key, path, body=None):
            if path.endswith("/prepaid/balance"):
                return 200, {"total": "0"}, ""
            if body and body["analyticsRequest"]["values"][0]["name"] == "usd":
                return 500, None, "http 500"
            return 200, self.units, ""
        payload, code = self._run(req)
        self.assertTrue(payload["ok"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["api_days"], [])
        self.assertIsNone(payload["period_spend_usd"])
        self.assertIn("tokens_text", payload["api_breakdown"]["input"])
        self.assertNotIn("usd_text", payload["api_breakdown"]["input"])
        self.assertEqual(payload["api_note"], "данные API частично недоступны")
        self.assertTrue(payload["partial"])

    def test_units_failure_keeps_chart_omits_token_counts(self):
        def req(key, path, body=None):
            if path.endswith("/prepaid/balance"):
                return 200, {"total": "0"}, ""
            if body and body["analyticsRequest"]["values"][0]["name"] == "units":
                return 0, None, "network error"
            return 200, self.usd, ""
        payload, _ = self._run(req)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["api_days"]), 10)
        self.assertEqual({r["date"]: r["usd"] for r in payload["api_days"]}["2026-11-22"], 2.26)
        self.assertIn("usd_text", payload["api_breakdown"]["input"])
        self.assertNotIn("tokens_text", payload["api_breakdown"]["input"])
        self.assertEqual(payload["api_note"], "данные API частично недоступны")

    def test_both_usage_fail_with_balance_has_no_fake_chart(self):
        def req(key, path, body=None):
            if path.endswith("/prepaid/balance"):
                return 200, {"total": {"val": "-1000"}}, ""
            return 503, None, "http 503"
        payload, _ = self._run(req)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_days"], [])
        self.assertIsNone(payload["api_breakdown"])
        self.assertEqual(payload["api_balance_text"], "$10.00")
        self.assertEqual(payload["balance_usd"], 10.0)
        self.assertNotIn("api_note", payload)

    def test_prepaid_val_cents_shape(self):
        def req(key, path, body=None):
            if path.endswith("/prepaid/balance"):
                return 200, {"total": {"val": "-250"}}, ""
            if body and body["analyticsRequest"]["values"][0]["name"] == "usd":
                return 200, self.usd, ""
            return 200, self.units, ""
        payload, _ = self._run(req)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_balance_text"], "$2.50")
        self.assertEqual(payload["balance_usd"], 2.5)
        self.assertNotEqual(payload["api_days"], [])
        self.assertNotIn("api_note", payload)



if __name__ == "__main__":
    unittest.main()
