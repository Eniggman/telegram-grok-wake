"""Liveness: detect a genuinely hung relay without being fooled by VM freezes.

Why clocks matter (root cause of the false "hangs" seen on 2026-11-22):
this box is a VM that is frozen (snapshot / "virtual machine fork") while the
agent is idle. While frozen NOTHING runs, including the relay and this
watchdog; on resume the wall clock (CLOCK_REALTIME) jumps forward by the
frozen time, but CLOCK_MONOTONIC does not advance at all. A watchdog that
compares wall-clock timestamps (file mtime) then sees "no heartbeat for 195 s"
at the instant of resume and kills a healthy process. So all liveness maths
here uses time.monotonic() (system-wide, so the supervisord listener can read
the value the relay wrote), and a FreezeDetector notices the wall/monotonic
divergence so the relay can drop its now-dead TCP connections at once.

Layers:
1. In-process (Liveness): an OS thread (works even if the event loop is
   blocked) hard-exits with code 70 (after dumping all thread stacks with
   faulthandler) if the relay made no progress for TG_WATCHDOG_SECONDS of
   monotonic time. Progress = poll returned, delivery attempt, retry pause...
2. Out-of-process (supervisord event listener, `python -m tgbridge.watchdog`):
   on every TICK_5 it reads the relay's heartbeat file ("<pid> <monotonic>")
   and SIGKILLs a RUNNING relay whose heartbeat is older than the threshold.
   Catches what layer 1 cannot: a SIGSTOPped or wedged process.
   Protocol: http://supervisord.org/events.html (supervisor.childutils).
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

EXIT_HUNG = 70
PROGRAM = "tg-bridge"
FREEZE_TOLERANCE = 15.0  # seconds of wall-vs-monotonic divergence that counts as a VM freeze


def read_heartbeat(path: Path) -> tuple[int, float] | None:
    """Parse "<pid> <monotonic>" written by Liveness.beat(); None if absent/garbled."""
    try:
        pid_s, mono_s = path.read_text().split()
        return int(pid_s), float(mono_s)
    except (OSError, ValueError):
        return None


class FreezeDetector:
    """Detects that the VM was frozen: wall clock advanced much more than monotonic."""

    def __init__(self, tolerance: float = FREEZE_TOLERANCE, wall=time.time, mono=time.monotonic):
        self.tolerance = tolerance
        self.wall = wall
        self.mono = mono
        self._w = wall()
        self._m = mono()

    def check(self) -> float:
        """Return the frozen seconds since the last check (0.0 if none)."""
        w, m = self.wall(), self.mono()
        jump = (w - self._w) - (m - self._m)
        self._w, self._m = w, m
        return jump if jump > self.tolerance else 0.0


class Liveness:
    def __init__(self, heartbeat_file: Path, threshold: float, log, clock=time.monotonic,
                 exit_fn=os._exit, interval: float = 5.0, dump=None):
        self.heartbeat_file = heartbeat_file
        self.threshold = threshold
        self.log = log
        self.clock = clock
        self.exit_fn = exit_fn
        self.interval = interval
        self.dump = dump or (lambda: faulthandler.dump_traceback(file=sys.stderr, all_threads=True))
        self._last = clock()
        self._written = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        now = self.clock()
        self._last = now
        if now - self._written < 1.0:  # the file is for other processes; 1 s is plenty
            return
        self._written = now
        tmp = self.heartbeat_file.with_name(self.heartbeat_file.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {now:.3f}\n")
            os.replace(tmp, self.heartbeat_file)
        except OSError:
            pass

    def stale_for(self) -> float:
        return self.clock() - self._last

    def check(self) -> bool:
        """Return True if healthy; otherwise dump stacks, log and exit the process."""
        age = self.stale_for()
        if age <= self.threshold:
            return True
        self.log("watchdog: no progress, dumping stacks and exiting so the supervisor restarts the relay",
                 stale_seconds=int(age), threshold=int(self.threshold))
        try:
            self.dump()
        except Exception:  # noqa: BLE001 - diagnostics must never block the exit
            pass
        self.exit_fn(EXIT_HUNG)
        return False

    def start(self) -> None:
        def run():
            while not self._stop.wait(self.interval):
                self.check()
        self._thread = threading.Thread(target=run, name="tg-liveness", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# supervisord event listener
# ---------------------------------------------------------------------------

def decide(proc_info: dict, heartbeat: tuple[int, float] | None, now_mono: float, threshold: float,
           first_seen_mono: float) -> str | None:
    """Return a reason to kill the relay, or None if it looks alive.

    Everything is on the monotonic clock, so a VM freeze (wall clock jumps,
    monotonic stands still) can never look like a hang.
    """
    if proc_info.get("state") != 20:  # supervisor ProcessStates.RUNNING
        return None
    if now_mono - first_seen_mono < threshold:  # grace for a freshly started relay
        return None
    pid = proc_info.get("pid")
    if heartbeat is None or heartbeat[0] != pid:
        return f"no heartbeat from pid {pid} in {int(now_mono - first_seen_mono)}s"
    age = now_mono - heartbeat[1]
    if age < 0 or age > threshold:  # age < 0: heartbeat from before a reboot
        return f"heartbeat stale for {int(age)}s"
    return None


def _log(msg: str) -> None:
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S") + " tg-watchdog: " + msg + "\n")
    sys.stderr.flush()


def listener_main() -> int:
    from supervisor import childutils

    state_dir = Path(os.environ["TG_BRIDGE_STATE"])
    threshold = float(os.environ.get("TG_WATCHDOG_SECONDS") or 180)
    hb = state_dir / "heartbeat"
    rpc = childutils.getRPCInterface(os.environ)
    first_seen: dict[int, float] = {}
    killed: tuple[int, float] | None = None
    _log(f"started (threshold {int(threshold)}s, monotonic clock)")
    while True:
        headers, _payload = childutils.listener.wait(sys.stdin, sys.stdout)
        try:
            if headers.get("eventname", "").startswith("TICK"):
                info = rpc.supervisor.getProcessInfo(PROGRAM)
                pid = info.get("pid")
                now = time.monotonic()
                if info.get("state") == 20 and pid:
                    first_seen.setdefault(pid, now)
                for old in [p for p in first_seen if p != pid]:
                    del first_seen[old]
                reason = decide(info, read_heartbeat(hb), now, threshold, first_seen.get(pid, now))
                if reason and killed and killed[0] == pid and now - killed[1] < 30:
                    reason = None  # already killed this pid; supervisord is restarting it
                if reason:
                    _log(f"{PROGRAM} pid {pid} looks hung ({reason}); sending SIGKILL, "
                         "autorestart will start a fresh one")
                    rpc.supervisor.signalProcess(PROGRAM, "KILL")
                    killed = (pid, now)
        except Exception as e:  # never let the listener die on a transient RPC error
            _log(f"check failed: {type(e).__name__}: {e}")
        childutils.listener.ok(sys.stdout)


if __name__ == "__main__":
    sys.exit(listener_main())
