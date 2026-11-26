"""tg-bridge: a small, private Telegram <-> Grok Bot routine bridge.

v4: inbound messages are handled by Telegram Serverless (project in
./serverless, deployed with Telegram's official tgcloud CLI); the box keeps the
tools the routine runs (tg-send, tg-history, tg-fetch-attachments, tg-health).
The v3 long-polling relay (aiogram 3, aiohttp, tenacity, supervisord) stays as a
fallback: bin/tg-mode polling.
"""

__version__ = "4.6.0"
TARGET_BOT_API = "10.5"
