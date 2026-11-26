"""Box ↔ Telegram Serverless (v4).

Inbound messages are handled by the Serverless project in ./serverless (Telegram
runs it from its own webhook). The box talks to that project only through the
official CLI, `tgcloud` (npm package @tgcloud/cli, pinned in
serverless/package-lock.json), authenticated by the TGCLOUD_TOKEN environment
variable (never written to disk, never printed):

  tgcloud run endpoints/box '<json>'   → admin ops on the bot's database
  tgcloud webhook / status / push / migrate

`endpoints/box` refuses calls with Mini App init data, so only `tgcloud run`
can reach it. It answers with "TGB:<hex of JSON>", which is parsed here; the rest
of the CLI output (which echoes the arguments) is discarded and never logged.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "serverless"
TGCLOUD = PROJECT / "node_modules" / ".bin" / "tgcloud"
TOKEN_ENV = "TGCLOUD_TOKEN"
RESULT_RE = re.compile(r"TGB:([0-9a-f]+)")
MAX_ARG = 100_000  # stay well below Linux MAX_ARG_STRLEN (128 KiB)

MODE_FILE = "mode"            # in the state dir: "serverless" or "polling"
OUTBOX = "cloud-outbox.jsonl"  # tg-send replies waiting to be stored in the cloud


class CloudError(Exception):
    pass


def have_token(environ=None) -> bool:
    return bool((environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip())


def _env(environ=None) -> dict:
    env = dict(environ if environ is not None else os.environ)
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"})
    return env


def cli(args: list[str], timeout: float = 90, environ=None) -> subprocess.CompletedProcess:
    """Run the tgcloud CLI in the project folder (output captured, not shown)."""
    if not TGCLOUD.exists():
        raise CloudError(f"tgcloud CLI missing; run: (cd {PROJECT} && npm ci)")
    if not have_token(environ):
        raise CloudError(f"{TOKEN_ENV} is not set in this environment")
    try:
        return subprocess.run([str(TGCLOUD), *args], cwd=PROJECT, env=_env(environ), capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise CloudError(f"tgcloud {args[0]} timed out after {int(timeout)}s") from None


def _error_line(out: str) -> str:
    """A safe one-line error from CLI output: only the CLI's own ✗/Error lines,
    never the echoed argument header (which may contain a secret)."""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("✗") or s.startswith("Error"):
            return s[:300]
    return "no result from tgcloud run"


def call(op: str, timeout: float = 90, environ=None, **params) -> dict:
    payload = json.dumps({"op": op, **params}, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode()) > MAX_ARG:
        raise CloudError("request too large; split it")
    proc = cli(["run", "endpoints/box", payload, "--ctx", "{box:true}"], timeout=timeout, environ=environ)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m = RESULT_RE.search(out)
    if not m:
        raise CloudError(_error_line(out))
    data = json.loads(bytes.fromhex(m.group(1)).decode("utf-8"))
    if not data.get("ok"):
        raise CloudError(str(data.get("error") or "error"))
    return data


# -- intake mode ---------------------------------------------------------------
def get_mode(state_dir: Path) -> str:
    try:
        v = (Path(state_dir) / MODE_FILE).read_text().strip()
    except OSError:
        return "polling"
    return v if v in ("serverless", "polling") else "polling"


def set_mode(state_dir: Path, mode: str) -> None:
    p = Path(state_dir) / MODE_FILE
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(mode + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# -- outbound memory (tg-send) -------------------------------------------------
def queue_record(state_dir: Path, item: dict) -> None:
    """Append one memory item (bot reply or topic) to the local outbox. Cheap and
    never fails a send; flush() moves it to the cloud."""
    p = Path(state_dir) / OUTBOX
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(item, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


def spawn_flush(state_dir: Path) -> None:
    """Flush the outbox in a detached background process (tg-send returns at once)."""
    try:
        subprocess.Popen([sys.executable, "-m", "tgbridge.cloudcli", "flush", "--quiet"],
                         cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def flush(state_dir: Path, environ=None, caller=None) -> dict:
    """Send queued items to the cloud. Single instance (flock); items are removed
    only after the cloud stored them (idempotent on retry)."""
    call_fn = caller or call
    state_dir = Path(state_dir)
    lock_path = state_dir / (OUTBOX + ".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "busy": True}
        total = {"ok": True, "added": 0, "skipped": 0, "sent": 0}
        p = state_dir / OUTBOX
        for _ in range(20):  # new items may arrive while flushing
            if not p.exists() or p.stat().st_size == 0:
                break
            lines = p.read_text(encoding="utf-8").splitlines()
            items, topics = [], []
            for line in lines:
                try:
                    it = json.loads(line)
                except ValueError:
                    continue
                (topics if it.get("kind") == "topic" else items).append(it)
            for chunk in _chunks(items, topics):
                res = call_fn("record", environ=environ, items=chunk[0], topics=chunk[1])
                total["added"] += res.get("added", 0)
                total["skipped"] += res.get("skipped", 0)
                total["sent"] += len(chunk[0]) + len(chunk[1])
            _drop_lines(p, len(lines))
        return total
    finally:
        os.close(fd)


def _chunks(items: list, topics: list):
    cur, size = [], 0
    for it in items:
        it = {k: v for k, v in it.items() if k != "kind"}
        n = len(json.dumps(it, ensure_ascii=False).encode())
        if cur and size + n > MAX_ARG - 5000:
            yield cur, []
            cur, size = [], 0
        cur.append(it)
        size += n
    tops = [{k: v for k, v in t.items() if k != "kind"} for t in topics]
    if cur or tops:
        yield cur, tops


def _drop_lines(p: Path, n: int) -> None:
    """Remove the first n lines (those just stored), keeping anything appended since."""
    fd = os.open(p, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(os.dup(fd), "r+", encoding="utf-8") as f:
            lines = f.read().splitlines(keepends=True)
            f.seek(0)
            f.writelines(lines[n:])
            f.truncate()
    finally:
        os.close(fd)


def outbox_size(state_dir: Path) -> int:
    p = Path(state_dir) / OUTBOX
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def wait_stop(chat_id: int, draft_id: int, environ=None) -> bool:
    try:
        return bool(call("stop_check", timeout=30, environ=environ, chat_id=chat_id, draft_id=draft_id).get("stopped"))
    except CloudError:
        return False


def now() -> int:
    return int(time.time())
