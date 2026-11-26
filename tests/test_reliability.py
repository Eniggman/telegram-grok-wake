"""v2 reliability: key source, no-loss offset, retries/dead letters, hang watchdogs."""

import asyncio
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import OWNER, ROUTINE_KEY, make_env, msg
from tests.test_bridge import Base, SyncBase
from tgbridge import redeliver
from tgbridge.config import ConfigError, load_config
from tgbridge.state import list_deadletters
from tgbridge.watchdog import EXIT_HUNG, FreezeDetector, Liveness, decide, read_heartbeat

OTHER_KEY = "someone-elses-routine-key-0000000000"
OUR_KEY = "our-own-routine-key-1111111111111111"


def _env_without_key(tmp, tg, routine, extra):
    env = make_env(tmp, tg, routine, extra)
    # make_env writes ROUTINE_KEY into the file; drop it for key-source tests
    f = Path(env["TG_BRIDGE_ENV_FILE"])
    f.write_text("\n".join(l for l in f.read_text().splitlines() if not l.startswith("ROUTINE_KEY=")) + "\n")
    return env


class KeySourceTests(SyncBase):
    def test_key_env_indirection_ignores_generic_routine_key(self):
        env = _env_without_key(self.tmp, self.tg, self.routine, {"ROUTINE_KEY_ENV": "MY_BRIDGE_KEY"})
        cfg = load_config(need_routine=True, environ={**env, "ROUTINE_KEY": OTHER_KEY, "MY_BRIDGE_KEY": OUR_KEY})
        self.assertEqual(cfg.routine_key, OUR_KEY)
        self.assertEqual(cfg.secret_sources["ROUTINE_KEY"], "env:MY_BRIDGE_KEY")

    def test_key_in_file_beats_generic_env(self):
        cfg = load_config(need_routine=True, environ={**self.env, "ROUTINE_KEY": OTHER_KEY})
        self.assertEqual(cfg.routine_key, ROUTINE_KEY)
        self.assertEqual(cfg.secret_sources["ROUTINE_KEY"], "file")

    def test_missing_referenced_env_var_is_config_error(self):
        env = _env_without_key(self.tmp, self.tg, self.routine, {"ROUTINE_KEY_ENV": "MY_BRIDGE_KEY"})
        with self.assertRaisesRegex(ConfigError, "MY_BRIDGE_KEY"):
            load_config(need_routine=True, environ={**env, "ROUTINE_KEY": OTHER_KEY})

    def test_both_key_and_key_env_in_file_is_ambiguous(self):
        env = make_env(self.tmp, self.tg, self.routine, {"ROUTINE_KEY_ENV": "MY_BRIDGE_KEY"})
        with self.assertRaisesRegex(ConfigError, "not both"):
            load_config(need_routine=True, environ={**env, "MY_BRIDGE_KEY": OUR_KEY})

    def test_file_settings_win_over_environment(self):
        cfg = load_config(need_routine=True, environ={**self.env, "TG_OWNER_ID": "1"})
        self.assertEqual(cfg.owner_id, OWNER)

    def test_error_messages_never_contain_the_key(self):
        env = _env_without_key(self.tmp, self.tg, self.routine, {"ROUTINE_KEY_ENV": "MY_BRIDGE_KEY"})
        try:
            load_config(need_routine=True, environ={**env, "MY_BRIDGE_KEY": "short"})
        except ConfigError as e:
            self.assertNotIn("short", str(e).replace("too short", ""))


class KeySourceDeliveryTests(Base):
    async def test_webhook_gets_the_referenced_key(self):
        self.env = _env_without_key(self.tmp, self.tg, self.routine, {"ROUTINE_KEY_ENV": "MY_BRIDGE_KEY"})
        self.env.update({"ROUTINE_KEY": OTHER_KEY, "MY_BRIDGE_KEY": OUR_KEY})
        await self.relay().process([msg(1, "task")])
        self.assertEqual(self.routine.posts[0][0]["Authorization"], f"Bearer {OUR_KEY}")


class NoLossTests(Base):
    async def test_offset_not_advanced_if_interrupted_mid_delivery(self):
        r = self.relay()

        async def interrupted(event):
            raise asyncio.CancelledError()  # e.g. SIGTERM / watchdog restart while posting

        r.webhook.deliver = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await r.process([msg(1, "important")])
        self.assertEqual(r.state.offset, 0)
        r2 = self.relay()  # restart: Telegram re-sends update 1
        await r2.process([msg(1, "important")])
        self.assertEqual([p[1]["message"]["text"] for p in self.routine.posts], ["important"])
        self.assertEqual(r2.state.offset, 2)

    async def test_permanent_failure_is_dead_lettered_then_redelivered(self):
        self.routine.statuses = [401]
        r = self.relay()
        await r.process([msg(1, "do not lose me")])
        self.assertEqual(r.state.offset, 2)
        dead = list_deadletters(r.cfg.state_dir)
        self.assertEqual([p.name for p in dead], ["1.json"])
        self.assertEqual(oct(dead[0].stat().st_mode & 0o777), "0o600")
        saved = json.loads(dead[0].read_text())
        self.assertEqual(saved["http"], 401)
        self.assertEqual(saved["event"]["message"]["text"], "do not lose me")
        notes = [c[1]["text"] for c in self.tg.calls if c[0] in ("sendMessage", "editMessageText")]
        self.assertTrue(any("401" in t and "сохранено" in t for t in notes))
        # key fixed -> tg-redeliver --send delivers and removes it
        cfg = load_config(need_routine=True, environ=self.env)
        code = await redeliver._send(cfg, dead)
        self.assertEqual(code, 0)
        self.assertEqual(list_deadletters(r.cfg.state_dir), [])
        self.assertEqual(self.routine.posts[-1][1]["message"]["text"], "do not lose me")

    async def test_transient_errors_retry_then_dead_letter(self):
        self.routine.statuses = [503] * 6
        r = self.relay()
        await r.process([msg(1, "x")])
        self.assertEqual(len(self.routine.posts), 6)  # TG_DELIVERY_ATTEMPTS default
        self.assertEqual(len(list_deadletters(r.cfg.state_dir)), 1)
        self.assertEqual(r.state.offset, 2)

    async def test_network_error_is_retried(self):
        r = self.relay()
        good_url = r.webhook.url
        r.webhook.url = "http://127.0.0.1:9/unreachable"
        calls = []
        orig = r.webhook._post_once

        async def flaky(body):
            calls.append(1)
            if len(calls) == 2:  # second attempt: network is back
                r.webhook.url = good_url
            return await orig(body)

        r.webhook._post_once = flaky
        await r.process([msg(1, "x")])
        self.assertEqual(len(calls), 2)  # 1 network error, then success
        self.assertEqual(len(self.routine.posts), 1)
        self.assertEqual(r.state.data["last_delivery"]["status"], "ok")


class CliTests(SyncBase):
    def test_chat_flag_after_subcommand_as_in_how_to_reply(self):
        from tgbridge.send import build_parser
        a = build_parser().parse_args(["text", "--chat", "7777", "--thread", "5", "--reply-to", "3", "hi"])
        self.assertEqual((a.chat, a.thread, a.reply_to, a.text), ("7777", 5, 3, "hi"))
        a = build_parser().parse_args(["--chat", "7777", "photo", "x.png"])
        self.assertEqual(a.chat, "7777")
        a = build_parser().parse_args(["text", "hi"])
        self.assertIsNone(a.chat)


class HangTests(Base):
    async def test_stalled_getupdates_is_cut_off_by_timeouts(self):
        self.tg.hang["getUpdates"] = 5
        r = self.relay()
        t0 = time.monotonic()
        with mock.patch("tgbridge.relay.POLL_HARD_MARGIN", 1):
            with self.assertRaises(TimeoutError):
                await r.get_updates(0, timeout=0)
        self.assertLess(time.monotonic() - t0, 4)

    async def test_poll_loop_recovers_after_stall(self):
        self.tg.hang["getUpdates"] = 3
        r = self.relay()
        with mock.patch("tgbridge.relay.POLL_HARD_MARGIN", 1), mock.patch("tgbridge.relay.POLL_TIMEOUT", 0):
            async def poll_then_unstall():
                try:
                    await r.poll_once()
                except TimeoutError:
                    self.tg.hang.clear()
                    self.tg.updates = [msg(1, "after stall")]
                    await r.poll_once()

            await poll_then_unstall()
        self.assertEqual(self.routine.posts[0][1]["message"]["text"], "after stall")


class LivenessTests(unittest.TestCase):
    def test_exits_when_no_progress(self):
        now = [0.0]
        exits = []
        log = io.StringIO()
        lv = Liveness(Path(os.devnull + "-nope"), threshold=180, log=lambda m, **k: log.write(m),
                      clock=lambda: now[0], exit_fn=exits.append)
        lv.beat()
        now[0] = 100
        self.assertTrue(lv.check())
        lv.beat()
        now[0] = 270
        self.assertTrue(lv.check())
        now[0] = 400
        self.assertFalse(lv.check())
        self.assertEqual(exits, [EXIT_HUNG])
        self.assertIn("watchdog", log.getvalue())

    def test_thread_fires_even_if_event_loop_is_blocked(self):
        exits = []
        lv = Liveness(Path(os.devnull + "-nope"), threshold=0.2, log=lambda m, **k: None,
                      exit_fn=exits.append, interval=0.05)
        lv.beat()
        lv.start()
        time.sleep(0.6)  # simulates a blocked event loop: nobody calls beat()
        lv.stop()
        self.assertIn(EXIT_HUNG, exits)

    def test_relay_sleep_counts_as_progress(self):
        async def run():
            from tests.fakes import FakeRoutine, FakeTelegram
            import tempfile
            from tgbridge.relay import Relay
            with tempfile.TemporaryDirectory() as d:
                tg, rt = FakeTelegram(), FakeRoutine()
                try:
                    env = make_env(Path(d), tg, rt)
                    r = Relay(load_config(need_routine=True, environ=env), log=lambda *a, **k: None)
                    r.liveness._last -= 1000
                    await r.sleep(0.01)
                    self.assertLess(r.liveness.stale_for(), 5)
                    await r.close()
                finally:
                    tg.close()
                    rt.close()
        asyncio.run(run())


class SupervisorListenerDecisionTests(unittest.TestCase):
    """The listener works on the monotonic clock only (see watchdog.py docstring)."""
    RUNNING = 20

    def info(self, pid=7):
        return {"state": self.RUNNING, "pid": pid}

    def test_kills_running_relay_with_stale_heartbeat(self):
        self.assertIsNotNone(decide(self.info(), (7, 1100.0), now_mono=1500.0, threshold=180, first_seen_mono=1000.0))

    def test_fresh_heartbeat_is_fine(self):
        self.assertIsNone(decide(self.info(), (7, 1450.0), now_mono=1500.0, threshold=180, first_seen_mono=1000.0))

    def test_grace_after_start_even_with_old_heartbeat_file(self):
        self.assertIsNone(decide(self.info(), (6, 10.0), now_mono=1500.0, threshold=180, first_seen_mono=1400.0))

    def test_heartbeat_from_a_previous_pid_does_not_count(self):
        self.assertIsNotNone(decide(self.info(7), (6, 1499.0), now_mono=1500.0, threshold=180, first_seen_mono=1000.0))

    def test_not_running_is_left_to_supervisord(self):
        self.assertIsNone(decide({"state": 30, "pid": 0}, None, 5000.0, 180, 0.0))

    def test_missing_heartbeat_after_grace(self):
        self.assertIsNotNone(decide(self.info(), None, now_mono=1500.0, threshold=180, first_seen_mono=1000.0))

    def test_heartbeat_from_before_a_reboot_is_stale(self):
        self.assertIsNotNone(decide(self.info(), (7, 99999.0), now_mono=1500.0, threshold=180, first_seen_mono=1000.0))


class VmFreezeRegressionTests(Base):
    """Root cause of the 2026-11-22 false 'hangs': the VM is frozen while the agent is idle.
    On resume the wall clock jumps, the monotonic clock does not."""

    def test_freeze_detector_sees_wall_jump_but_not_normal_time(self):
        wall, mono = [1000.0], [50.0]
        fd = FreezeDetector(tolerance=15, wall=lambda: wall[0], mono=lambda: mono[0])
        wall[0] += 5; mono[0] += 5
        self.assertEqual(fd.check(), 0.0)
        wall[0] += 2.1; mono[0] += 2.0          # ordinary jitter
        self.assertEqual(fd.check(), 0.0)
        wall[0] += 195.0; mono[0] += 0.01       # frozen for ~195 s
        self.assertAlmostEqual(fd.check(), 194.99, places=1)
        wall[0] += 2; mono[0] += 2              # back to normal
        self.assertEqual(fd.check(), 0.0)

    def test_freeze_is_not_a_hang_for_either_watchdog(self):
        wall, mono = [1000.0], [50.0]
        lv = Liveness(Path("/nonexistent/hb"), threshold=180, log=lambda *a, **k: None,
                      clock=lambda: mono[0], exit_fn=lambda c: self.fail("killed a healthy relay"))
        lv.beat()
        wall[0] += 195; mono[0] += 0.01         # VM freeze: only the wall clock moves
        self.assertTrue(lv.check())
        # listener: heartbeat written on the monotonic clock is still fresh
        self.assertIsNone(decide({"state": 20, "pid": 7}, (7, 50.0), now_mono=mono[0], threshold=180,
                                 first_seen_mono=0.0))

    def test_heartbeat_file_holds_pid_and_monotonic_time(self):
        hb = self.tmp / "heartbeat"
        lv = Liveness(hb, threshold=180, log=lambda *a, **k: None)
        lv.beat()
        pid, mono = read_heartbeat(hb)
        self.assertEqual(pid, os.getpid())
        self.assertLess(abs(time.monotonic() - mono), 5)
        self.assertEqual(oct(hb.stat().st_mode & 0o777), "0o600")
        hb.write_text("garbage")
        self.assertIsNone(read_heartbeat(hb))

    async def test_resume_drops_dead_connection_and_polling_continues_at_once(self):
        """A getUpdates sent before the freeze never answers (dead TCP). After resume the relay
        must not wait for the poll timeout: it drops connections and polls again immediately."""
        self.tg.hang["getUpdates"] = 20
        r = self.relay()
        t0 = time.monotonic()

        async def resume_after_freeze():
            await asyncio.sleep(0.5)
            self.tg.hang.clear()                 # the fresh connection works
            self.tg.updates = [msg(1, "sent while frozen")]
            await r.handle_resume(195)

        resumer = asyncio.create_task(resume_after_freeze())
        code = await r._retry_forever("poll", r.poll_once)
        await resumer
        self.assertIsNone(code)
        self.assertLess(time.monotonic() - t0, 8)    # not 20 s (stall) nor 30+ s (poll timeout)
        self.assertEqual([p[1]["message"]["text"] for p in self.routine.posts], ["sent while frozen"])
        self.assertIn("VM freeze detected", self.logbuf.getvalue())
        self.assertFalse(r.connections_dropped)

    async def test_conflict_after_resume_retries_quickly(self):
        from tgbridge.tgapi import TelegramConflictError
        r = self.relay()
        calls = []

        async def poll():
            calls.append(1)
            if len(calls) == 1:
                raise TelegramConflictError(method=mock.Mock(), message="Conflict: terminated by other getUpdates")
        t0 = time.monotonic()
        await r._retry_forever("poll", poll)
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - t0, 1)  # test sleep is stubbed; real wait is 3 s, not 30


if __name__ == "__main__":
    unittest.main()
