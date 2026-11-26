"""Deliver one event to the Grok Bot routine webhook.

HTTP is aiohttp (already required by aiogram) with explicit connect/read/total
timeouts; retries are tenacity with exponential backoff + jitter. Only
transient failures are retried (network, timeouts, 408/425/429/5xx); any other
4xx (e.g. 401 wrong key) is permanent and returned at once.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import aiohttp
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from . import __version__

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
TIMEOUT = aiohttp.ClientTimeout(total=45, connect=15, sock_connect=15, sock_read=30)


class _Transient(Exception):
    def __init__(self, status: int | None, detail: str):
        super().__init__(detail)
        self.status = status


@dataclass
class DeliveryResult:
    ok: bool
    status: int | None
    attempts: int
    permanent: bool  # True: retrying will not help (e.g. 401)
    detail: str = ""


class WebhookClient:
    def __init__(self, url: str, key: str, attempts: int = 6, sleep=asyncio.sleep,
                 on_attempt=None, max_wait: float = 60):
        self.url = url
        self._key = key
        self.attempts = attempts
        self.sleep = sleep
        self.on_attempt = on_attempt  # called before every attempt (liveness heartbeat)
        self.max_wait = max_wait
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False: ignore proxy variables, talk to the routine URL directly.
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=False)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _post_once(self, body: bytes) -> int:
        if self.on_attempt:
            self.on_attempt()
        session = await self._get_session()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._key}",
                   "User-Agent": f"tg-bridge/{__version__}"}
        try:
            async with session.post(self.url, data=body, headers=headers, allow_redirects=False) as resp:
                await resp.read()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise _Transient(None, type(e).__name__) from None
        if status in RETRYABLE_STATUS:
            raise _Transient(status, f"HTTP {status}")
        return status

    async def deliver(self, event: dict) -> DeliveryResult:
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        attempts = 0
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(_Transient),
            stop=stop_after_attempt(self.attempts),
            wait=wait_exponential_jitter(multiplier=2, max=self.max_wait, jitter=2),
            sleep=self.sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    attempts = attempt.retry_state.attempt_number
                    status = await self._post_once(body)
        except RetryError as e:
            exc = e.last_attempt.exception()
            st = getattr(exc, "status", None)
            return DeliveryResult(False, st, attempts, False, str(exc))
        if 200 <= status < 300:
            return DeliveryResult(True, status, attempts, False)
        return DeliveryResult(False, status, attempts, True, f"HTTP {status}")
