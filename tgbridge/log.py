"""Minimal logger that never prints secrets."""

from __future__ import annotations

import re
import sys
import time

_TOKEN_SHAPE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,64}")


class Redactor:
    def __init__(self, secrets: list[str] | None = None):
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 8]

    def __call__(self, text: object) -> str:
        s = str(text)
        for secret in self.secrets:
            s = s.replace(secret, "<redacted>")
        return _TOKEN_SHAPE.sub("<redacted-token>", s)


class Logger:
    def __init__(self, redactor: Redactor, stream=None):
        self.redact = redactor
        self.stream = stream or sys.stderr

    def __call__(self, msg: str, **fields: object) -> None:
        parts = [time.strftime("%Y-%m-%d %H:%M:%S"), msg]
        parts += [f"{k}={v}" for k, v in fields.items()]
        try:
            self.stream.write(self.redact(" ".join(parts)) + "\n")
            self.stream.flush()
        except Exception:
            pass
