"""tg-ensure / tg-stop / tg-ctl: run the bridge under supervisord.

supervisord (pip package `supervisor`, http://supervisord.org) is the process
supervisor: it restarts the relay on any unexpected exit and hosts the
tg-watchdog event listener that kills a hung relay. This box has no systemd
(PID 1 is tini), so a per-user supervisord is the standard choice.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xmlrpc.client
from pathlib import Path

from .config import ConfigError, load_config
from .health import supervisor_rpc
from .state import ensure_private_dir

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "supervisord.conf"
BIN = Path(sys.executable).parent
WANTED = ("tg-bridge", "tg-watchdog")


def _env(cfg) -> dict:
    env = dict(os.environ)
    env["TG_BRIDGE_STATE"] = str(cfg.state_dir)
    env["TG_WATCHDOG_SECONDS"] = str(cfg.watchdog_seconds)
    return env


def _alive(rpc) -> bool:
    try:
        rpc.supervisor.getState()
        return True
    except (OSError, xmlrpc.client.Error):
        return False


def _states(rpc) -> dict:
    return {p["name"]: p["statename"] for p in rpc.supervisor.getAllProcessInfo()}


def _wait_running(rpc, timeout: float = 30) -> dict:
    end = time.time() + timeout
    states = {}
    while time.time() < end:
        try:
            states = _states(rpc)
        except (OSError, xmlrpc.client.Error):
            states = {}
        if all(states.get(n) == "RUNNING" for n in WANTED):
            return states
        time.sleep(0.5)
    return states


def _load():
    try:
        return load_config(need_routine=True)
    except ConfigError as e:
        print(f"tg-bridge: config error, not starting: {e}", file=sys.stderr)
        print("tg-bridge: fix ~/.config/tg-bridge/env and run bin/tg-health --offline", file=sys.stderr)
        sys.exit(2)


def ensure() -> int:
    cfg = _load()
    from .cloud import get_mode
    if get_mode(cfg.state_dir) == "serverless":
        print("tg-bridge: intake runs on Telegram Serverless (v4); the box relay is not needed. "
              "Check: bin/tg-health. Fallback to the box relay: bin/tg-mode polling")
        return 0
    ensure_private_dir(cfg.state_dir)
    rpc = supervisor_rpc(cfg.state_dir)
    if rpc is not None and _alive(rpc):
        states = _states(rpc)
        started = []
        for name in WANTED:
            if states.get(name) not in ("RUNNING", "STARTING"):
                try:
                    rpc.supervisor.startProcess(name, False)
                    started.append(name)
                except xmlrpc.client.Fault as e:
                    print(f"tg-bridge: could not start {name}: {e.faultString}", file=sys.stderr)
        if not started:
            return 0
        states = _wait_running(rpc)
        print(f"tg-bridge: restarted {', '.join(started)}: {states}")
        return 0 if all(states.get(n) == "RUNNING" for n in WANTED) else 1
    sock = cfg.state_dir / "supervisor.sock"
    if sock.exists():
        sock.unlink()  # stale socket from a dead supervisord
    proc = subprocess.run([str(BIN / "supervisord"), "-c", str(CONF)], env=_env(cfg),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"tg-bridge: supervisord failed to start: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    for _ in range(40):
        rpc = supervisor_rpc(cfg.state_dir)
        if rpc is not None and _alive(rpc):
            break
        time.sleep(0.25)
    else:
        print("tg-bridge: supervisord did not come up; see supervisord.log", file=sys.stderr)
        return 1
    states = _wait_running(rpc)
    ok = all(states.get(n) == "RUNNING" for n in WANTED)
    print(f"tg-bridge: started under supervisord: {states}")
    return 0 if ok else 1


def stop() -> int:
    from .config import state_dir_only
    sd = state_dir_only()
    rpc = supervisor_rpc(sd)
    if rpc is None or not _alive(rpc):
        print("tg-bridge: not running")
        return 0
    rpc.supervisor.shutdown()
    for _ in range(60):
        if not _alive(supervisor_rpc(sd) or rpc):
            print("tg-bridge: stopped")
            return 0
        time.sleep(0.5)
    print("tg-bridge: supervisord still running; see supervisord.log", file=sys.stderr)
    return 1


def ctl(args: list[str]) -> int:
    from .config import state_dir_only
    env = dict(os.environ)
    env["TG_BRIDGE_STATE"] = str(state_dir_only())
    return subprocess.call([str(BIN / "supervisorctl"), "-c", str(CONF), *args], env=env)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "ensure":
        return ensure()
    if cmd == "stop":
        return stop()
    if cmd == "ctl":
        return ctl(rest)
    print("usage: python -m tgbridge.ctl ensure|stop|ctl [supervisorctl args]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
