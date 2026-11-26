"""Configuration and secret loading.

Settings live in one private env file (default ~/.config/tg-bridge/env), which
must be a regular file owned by the current user with no group/other
permissions (chmod 600). Anything looser and the bridge refuses to start.

Precedence (v2): the file is authoritative. The process environment only fills
settings the file does not set, because this box is shared and generic names
such as ROUTINE_KEY may belong to someone else.

Secrets (TG_BOT_TOKEN, ROUTINE_KEY) are resolved explicitly, see _secret():
1. If the file names a variable (TG_BOT_TOKEN_ENV / ROUTINE_KEY_ENV), the
   secret is read from exactly that process env variable and nowhere else.
2. Otherwise a value written in the file itself is used.
3. Otherwise the generic process env variable (legacy behaviour).
cfg.secret_sources records which one was used (never the value).
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENV_FILE = "~/.config/tg-bridge/env"
DEFAULT_STATE_DIR = "~/.local/state/tg-bridge"
OFFICIAL_API_BASE = "https://api.telegram.org"
DEFAULT_PLACEHOLDER = "\U0001F5E8\uFE0F Думаю..."  # 🗨️ Думаю...
DEFAULT_STICKER_PACK = "VikostVSpack"
OFF_VALUES = ("", "off", "none", "0", "false", "no")
_STICKER_PACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_STICKER_URL_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/addstickers/([A-Za-z][A-Za-z0-9_]{0,63})/?$", re.I)

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
KEY_RE = re.compile(r"^\S{16,512}$")


def normalize_sticker_pack(value: str | None, default: str = DEFAULT_STICKER_PACK) -> str:
    """Pack name, or '' to disable. Accepts a t.me/addstickers/… URL. Invalid → off."""
    if value is None:
        return default
    s = str(value).strip()
    if s.lower() in OFF_VALUES:
        return ""
    m = _STICKER_URL_RE.match(s)
    if m:
        s = m.group(1)
    if not _STICKER_PACK_RE.match(s):
        return ""
    return s


class ConfigError(Exception):

    """Configuration is missing, malformed, or insecure. Exit code 2."""


@dataclass
class Config:
    token: str
    owner_id: int
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    routine_url: str | None = None
    routine_key: str | None = None
    api_base: str = OFFICIAL_API_BASE
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR).expanduser())
    env_file: Path | None = None
    reaction: str = ""  # v4.3: off by default (was 👀); TG_REACTION=👀 brings it back
    placeholder: str = DEFAULT_PLACEHOLDER  # TG_PLACEHOLDER; "off" disables
    placeholder_edit_max_age: int = 0  # TG_PLACEHOLDER_EDIT_MAX_AGE; 0 = always edit
    placeholder_mode: str = "keep"  # TG_PLACEHOLDER_MODE: keep (answer = new message, placeholder stays "Думаю...") | done (placeholder -> ✅) | delete | edit
    sticker_pack: str = DEFAULT_STICKER_PACK  # TG_STICKER_PACK; "off" disables the thinking sticker
    max_wakes_per_min: int = 12
    max_file_bytes: int = 20 * 1024 * 1024
    inbox_keep_days: int = 7
    typing_max_seconds: int = 120
    delivery_attempts: int = 6
    watchdog_seconds: int = 180
    history_messages: int = 20
    history_max_chars: int = 12000
    history_days: int = 7
    secret_sources: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The owner is always allowed, even if TG_ALLOWED_USER_IDS is unset.
        self.allowed_user_ids = frozenset(set(self.allowed_user_ids or ()) | {self.owner_id})

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.token, self.routine_key) if s]

    @property
    def inbox_dir(self) -> Path:
        return self.state_dir / "inbox"


def _check_private_file(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(st.st_mode):
        raise ConfigError(f"{path}: must not be a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{path}: must be a regular file")
    if st.st_uid != os.getuid():
        raise ConfigError(f"{path}: must be owned by the current user")
    if st.st_mode & 0o077:
        raise ConfigError(
            f"{path}: permissions too open ({oct(st.st_mode & 0o777)}); run: chmod 600 {path}"
        )
    parent = os.stat(path.parent)
    if parent.st_mode & 0o022 or parent.st_uid != os.getuid():
        raise ConfigError(
            f"{path.parent}: folder must be owned by you and not writable by others; run: chmod 700 {path.parent}"
        )


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            out[key] = value
    return out


def _is_loopback_http(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "http" or not p.hostname:
        return False
    if p.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:
        return False


def _validate_url(name: str, url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.username or p.password:
        raise ConfigError(f"{name} must not contain credentials")
    if p.scheme == "https" and p.hostname:
        return url
    if _is_loopback_http(url):  # used by the test suite only
        return url
    raise ConfigError(f"{name} must be an https:// URL")


def _id_set(name: str, raw: str) -> set[int]:
    if not raw or not raw.strip():
        return set()
    out: set[int] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ConfigError(f"{name} must be a comma-separated list of numeric Telegram user ids")
        out.add(int(part))
    return out


def _int(values: dict[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = values.get(name, "")
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _secret(name: str, file_values: dict[str, str], env: dict[str, str],
            sources: dict[str, str]) -> str:
    """Resolve a secret from one explicit source. Never logs or returns where it is not asked."""
    ref = file_values.get(f"{name}_ENV", "")
    if ref:
        if not _ENV_NAME_RE.match(ref):
            raise ConfigError(f"{name}_ENV must be the name of an environment variable")
        if file_values.get(name):
            raise ConfigError(f"set either {name} or {name}_ENV in the config file, not both")
        val = env.get(ref, "")
        if not val:
            raise ConfigError(f"{name}_ENV={ref}, but the environment variable {ref} is empty or missing")
        sources[name] = f"env:{ref}"
        return val
    if file_values.get(name):
        sources[name] = "file"
        return file_values[name]
    if env.get(name):
        sources[name] = f"env:{name}"
        return env[name]
    return ""


def load_config(need_routine: bool, environ: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if environ is None else environ)
    env_file = Path(env.get("TG_BRIDGE_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()
    file_values: dict[str, str] = {}
    used_file: Path | None = None
    if env_file.exists() or os.path.islink(env_file):
        _check_private_file(env_file)
        file_values = parse_env_file(env_file.read_text(encoding="utf-8"))
        used_file = env_file
    # The private file is authoritative; the environment only fills gaps.
    values: dict[str, str] = {}
    for k, v in env.items():
        if k.startswith(("TG_", "ROUTINE_")) and v != "":
            values[k] = v
    values.update({k: v for k, v in file_values.items() if v != ""})
    sources: dict[str, str] = {}
    values["TG_BOT_TOKEN"] = _secret("TG_BOT_TOKEN", file_values, env, sources)
    values["ROUTINE_KEY"] = _secret("ROUTINE_KEY", file_values, env, sources)

    token = values.get("TG_BOT_TOKEN", "")
    if not TOKEN_RE.match(token):
        raise ConfigError("TG_BOT_TOKEN is missing or does not look like a BotFather token")
    owner_raw = values.get("TG_OWNER_ID", "")
    if not owner_raw.isdigit() or int(owner_raw) <= 0:
        raise ConfigError("TG_OWNER_ID must be your numeric Telegram user id (digits only)")

    allowed = _id_set("TG_ALLOWED_USER_IDS", values.get("TG_ALLOWED_USER_IDS", ""))
    cfg = Config(token=token, owner_id=int(owner_raw), allowed_user_ids=frozenset(allowed),
                 env_file=used_file, secret_sources=sources)

    url = values.get("ROUTINE_URL", "")
    key = values.get("ROUTINE_KEY", "")
    if url:
        cfg.routine_url = _validate_url("ROUTINE_URL", url)
    if key:
        if not KEY_RE.match(key):
            raise ConfigError("ROUTINE_KEY looks wrong (too short or contains spaces)")
        cfg.routine_key = key
    if need_routine and not (cfg.routine_url and cfg.routine_key):
        raise ConfigError("ROUTINE_URL and ROUTINE_KEY are required to run the bridge")

    api_base = values.get("TG_API_BASE", OFFICIAL_API_BASE).rstrip("/")
    if api_base != OFFICIAL_API_BASE and not _is_loopback_http(api_base):
        raise ConfigError("TG_API_BASE may only be https://api.telegram.org (or loopback for tests)")
    cfg.api_base = api_base

    if values.get("TG_BRIDGE_HOME"):
        cfg.state_dir = Path(values["TG_BRIDGE_HOME"]).expanduser()
    reaction = values.get("TG_REACTION", cfg.reaction).strip()
    cfg.reaction = "" if reaction.lower() in OFF_VALUES else reaction
    placeholder = values.get("TG_PLACEHOLDER", cfg.placeholder).strip()
    cfg.placeholder = "" if placeholder.lower() in OFF_VALUES else placeholder[:200]
    cfg.placeholder_edit_max_age = _int(values, "TG_PLACEHOLDER_EDIT_MAX_AGE", 0, 0, 86400)
    pm = values.get("TG_PLACEHOLDER_MODE", cfg.placeholder_mode).strip().lower()
    cfg.placeholder_mode = pm if pm in ("keep", "done", "delete", "edit") else "keep"
    cfg.max_wakes_per_min = _int(values, "TG_MAX_WAKES_PER_MIN", 12, 1, 120)
    cfg.max_file_bytes = _int(values, "TG_MAX_FILE_MB", 20, 1, 20) * 1024 * 1024
    cfg.inbox_keep_days = _int(values, "TG_INBOX_KEEP_DAYS", 7, 1, 365)
    cfg.typing_max_seconds = _int(values, "TG_TYPING_MAX_SECONDS", 120, 0, 900)
    cfg.delivery_attempts = _int(values, "TG_DELIVERY_ATTEMPTS", 6, 1, 12)
    cfg.watchdog_seconds = _int(values, "TG_WATCHDOG_SECONDS", 180, 90, 3600)
    cfg.history_messages = _int(values, "TG_HISTORY_MESSAGES", 20, 0, 200)
    cfg.history_max_chars = _int(values, "TG_HISTORY_MAX_CHARS", 12000, 500, 100000)
    cfg.history_days = _int(values, "TG_HISTORY_DAYS", 7, 1, 3650)
    cfg.sticker_pack = normalize_sticker_pack(values.get("TG_STICKER_PACK", cfg.sticker_pack))
    return cfg


def state_dir_only(environ: dict[str, str] | None = None) -> Path:
    """Best-effort state dir lookup without requiring secrets (for scripts).

    Same precedence as load_config: the private file first, then the environment.
    """
    env = dict(os.environ if environ is None else environ)
    env_file = Path(env.get("TG_BRIDGE_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()
    try:
        _check_private_file(env_file)
        v = parse_env_file(env_file.read_text(encoding="utf-8")).get("TG_BRIDGE_HOME")
        if v:
            return Path(v).expanduser()
    except (OSError, ConfigError):
        pass
    if env.get("TG_BRIDGE_HOME"):
        return Path(env["TG_BRIDGE_HOME"]).expanduser()
    return Path(DEFAULT_STATE_DIR).expanduser()
