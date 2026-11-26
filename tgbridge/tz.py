"""Timezone helper with Europe/Kyiv <-> Europe/Kiev fallback for older tzdata."""

from __future__ import annotations

from zoneinfo import ZoneInfo


def kyiv_tz(preferred: str = "Europe/Kyiv") -> ZoneInfo:
    """Return ZoneInfo for Kyiv, falling back between Europe/Kyiv and Europe/Kiev."""
    tried: list[str] = []
    for cand in (preferred, "Europe/Kyiv", "Europe/Kiev"):
        if cand in tried:
            continue
        tried.append(cand)
        try:
            return ZoneInfo(cand)
        except Exception:
            continue
    raise RuntimeError(f"no time zone data for {preferred}")
