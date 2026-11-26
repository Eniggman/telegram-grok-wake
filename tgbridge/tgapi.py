"""Telegram Bot API access through aiogram 3 (https://github.com/aiogram/aiogram).

aiogram owns HTTP, (de)serialisation, multipart uploads, file downloads and
error mapping. This module only:
* builds a Bot bound to api.telegram.org (or a loopback fake in tests) with a
  hard total timeout per request (aiohttp ClientTimeout(total=…) covers DNS,
  connect, TLS and read, which is what the old urllib client lacked);
* adds the Bot API 10.5 method setMySettings, which aiogram 3.33 does not ship
  yet, the documented way (a TelegramMethod subclass);
* provides one tenacity retry policy for transient errors (429/5xx/network).
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods.base import TelegramMethod
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

__all__ = [
    "TelegramAPIError", "TelegramBadRequest", "TelegramConflictError", "TelegramNetworkError",
    "TelegramRetryAfter", "TelegramServerError", "TelegramUnauthorizedError",
    "SetMySettings", "make_bot", "api_retry", "error_code", "TRANSIENT",
]

DEFAULT_TIMEOUT = 30  # seconds, total per request (getUpdates adds its long-poll time)
TRANSIENT = (TelegramRetryAfter, TelegramServerError, TelegramNetworkError)


class SetMySettings(TelegramMethod[bool]):
    """Bot API 10.5 setMySettings (private-chat topics). Not yet in aiogram 3.33."""

    __returning__ = bool
    __api_method__ = "setMySettings"

    has_topics_enabled: bool | None = None
    has_topic_sidebar: bool | None = None
    allows_users_to_create_topics: bool | None = None


def make_bot(token: str, api_base: str, timeout: int = DEFAULT_TIMEOUT) -> Bot:
    session = AiohttpSession(api=TelegramAPIServer.from_base(api_base), timeout=timeout)
    return Bot(token=token, session=session)


def error_code(e: BaseException) -> int | None:
    if isinstance(e, TelegramUnauthorizedError):
        return 401
    if isinstance(e, TelegramConflictError):
        return 409
    if isinstance(e, TelegramRetryAfter):
        return 429
    if isinstance(e, TelegramBadRequest):
        return 400
    if isinstance(e, TelegramServerError):
        return 500
    if isinstance(e, TelegramNetworkError):
        return None
    if isinstance(e, TelegramAPIError):
        # aiogram keeps the HTTP code only in the class; the description is enough here.
        return None
    return None


class _wait_retry_after(wait_base):
    """Honour Telegram's retry_after (capped), else exponential backoff with jitter."""

    def __init__(self, cap: float = 30):
        self.cap = cap
        self.fallback = wait_exponential_jitter(multiplier=1, max=20)

    def __call__(self, retry_state) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, TelegramRetryAfter):
            return float(min(exc.retry_after or 3, self.cap))
        return self.fallback(retry_state)


def api_retry(attempts: int = 4, sleep=None) -> AsyncRetrying:
    kw: dict[str, Any] = {}
    if sleep is not None:
        kw["sleep"] = sleep
    return AsyncRetrying(
        retry=retry_if_exception_type(TRANSIENT),
        stop=stop_after_attempt(attempts),
        wait=_wait_retry_after(),
        reraise=True,
        **kw,
    )
