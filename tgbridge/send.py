"""tg-send: the only way the routine talks back to Telegram.

Commands reach the owner's chat by default, or another allowlisted private
chat if --chat is set to that numeric id (TG_OWNER_ID or TG_ALLOWED_USER_IDS).
Text is read from an argument or from stdin ("-" or nothing).
Prints a JSON result to stdout. Exit codes: 0 ok, 1 Telegram/network error,
2 config error, 4 refused (wrong chat / unsafe file), 5 refused (secret leak).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from aiogram import Bot
from aiogram.types import (FSInputFile, InputProfilePhotoStatic, InputRichMessage,
                           ReactionTypeEmoji, ReplyParameters)

from .config import ConfigError, load_config, state_dir_only
from .rich import RichLimitError, build_rich_message, looks_rich, should_send_rich, validate_rich
from .control import ControlError, submit_request
from .cloud import get_mode, queue_record, spawn_flush, wait_stop
from .history import History
from . import placeholder as ph_mod
from .log import Redactor
from .state import mark_outbound
from .textfmt import TG_LIMIT, render_parts, split_plain, utf16_len
from .tgapi import SetMySettings, TelegramAPIError, TelegramBadRequest, api_retry, make_bot

PHOTO_MAX = 10 * 1024 * 1024
FILE_MAX = 50 * 1024 * 1024
TOPIC_ICON_COLORS = (7322096, 16766590, 13338331, 9367192, 16749490, 16478047)



class Refused(Exception):
    def __init__(self, msg: str, code: int = 4):
        super().__init__(msg)
        self.code = code


def _read_text(arg: str | None) -> str:
    if arg is None or arg == "-":
        data = sys.stdin.read()
    else:
        data = arg
    return data.rstrip("\n")


def _thread_field(thread: int | None) -> int | None:
    if thread is None:
        return None
    return int(thread)


def me_info(me) -> dict:
    """getMe result as a plain dict, incl. Bot API 10.5 fields aiogram keeps as extras."""
    d = me.model_dump(exclude_none=True) if hasattr(me, "model_dump") else dict(me)
    return {
        "username": d.get("username"),
        "bot_id": d.get("id"),
        "first_name": d.get("first_name"),
        "has_topics_enabled": bool(d.get("has_topics_enabled")),
        "has_topic_sidebar": bool(d.get("has_topic_sidebar")),
        "allows_users_to_create_topics": bool(d.get("allows_users_to_create_topics")),
    }


class Sender:
    def __init__(self, cfg, bot: Bot | None = None, sleep=asyncio.sleep):
        self.cfg = cfg
        self.redact = Redactor(cfg.secrets)
        self.bot = bot or make_bot(cfg.token, cfg.api_base)
        self.chat_id = cfg.owner_id
        self.sleep = sleep
        self.history = History(cfg.state_dir)
        self.mode = get_mode(cfg.state_dir)
        self._queued = False
        # v4.3 placeholder hand-off (see tgbridge/placeholder.py)
        self.use_placeholder = True
        self._ph: ph_mod.Placeholder | None = None
        self._ph_checked = False
        self._ph_reply_to: int | None = None

    def _remember(self, text: str, message_id: int | None, thread: int | None) -> None:
        """Record the bot's reply in the conversation history. Never fails a send.

        v4 (serverless mode): memory lives in the Serverless database. The reply is
        appended to a local outbox and a detached `tg-cloud flush` stores it, so
        tg-send does not wait for the cloud. polling mode: local SQLite as in v3."""
        try:
            if self.mode == "serverless":
                queue_record(self.cfg.state_dir, {"role": "assistant", "chat_id": self.chat_id,
                                                  "thread_id": int(thread or 0), "text": text,
                                                  "tg_message_id": message_id, "ts": int(time.time())})
                self._queued = True
            else:
                self.history.add(self.chat_id, thread, "assistant", text, tg_message_id=message_id)
        except (sqlite3.Error, OSError):
            pass

    def flush_memory(self) -> None:
        if self._queued:
            spawn_flush(self.cfg.state_dir)
            self._queued = False

    # -- guards -----------------------------------------------------------
    def check_chat(self, chat: str | None) -> None:
        if chat is None or str(chat).strip() == "":
            self.chat_id = self.cfg.owner_id
            return
        raw = str(chat).strip()
        try:
            cid = int(raw)
        except ValueError:
            raise Refused("refused: --chat must be a numeric Telegram user id") from None
        if cid not in self.cfg.allowed_user_ids:
            raise Refused("refused: tg-send can only send to the owner or an allowed user")
        self.chat_id = cid

    def check_secret_free(self, data: str | bytes) -> None:
        for secret in self.cfg.secrets:
            needle = secret.encode() if isinstance(data, bytes) else secret
            if needle in data:
                raise Refused("refused: the content contains a bridge secret", 5)

    def check_file(self, path_str: str, limit: int) -> Path:
        path = Path(path_str).expanduser()
        try:
            real = path.resolve(strict=True)
        except OSError:
            raise Refused(f"refused: file not found: {path_str}") from None
        if not real.is_file():
            raise Refused("refused: not a regular file")
        protected = [self.cfg.state_dir.resolve()]
        if self.cfg.env_file:
            protected.append(self.cfg.env_file.resolve().parent)
        for prot in protected:
            if real == prot or prot in real.parents:
                inbox = self.cfg.inbox_dir.resolve()
                if not (inbox == real.parent or inbox in real.parents):
                    raise Refused("refused: that file belongs to the bridge configuration")
        size = real.stat().st_size
        if size > limit:
            raise Refused(f"refused: file is too large for Telegram ({size} bytes > {limit})")
        self.check_secret_free(real.read_bytes())
        return real

    # -- helpers ----------------------------------------------------------
    async def _call(self, factory):
        """Run one Bot API call, retrying 429 (retry_after), 5xx and network errors."""
        async for attempt in api_retry(sleep=self.sleep):
            with attempt:
                return await factory()

    @staticmethod
    def _reply_params(reply_to: int | None) -> ReplyParameters | None:
        # v4.3.1: answers go out as plain messages (no quote) unless TG_REPLY_QUOTE=1.
        # reply_to is still used to find and replace the placeholder.
        if not reply_to or os.environ.get("TG_REPLY_QUOTE", "").strip().lower() not in ("1", "on", "true", "yes"):
            return None
        return ReplyParameters(message_id=int(reply_to), allow_sending_without_reply=True)

    # -- placeholder (v4.3) -----------------------------------------------
    async def _take_placeholder(self, reply_to: int | None, thread: int | None) -> ph_mod.Placeholder | None:
        """Claim the bridge's "🗨️ Думаю..." reply to `reply_to` once per tg-send run."""
        if not self._ph_checked:
            self._ph_checked = True
            if self.use_placeholder:
                self._ph_reply_to = int(reply_to) if reply_to else None
                self._ph = await asyncio.to_thread(ph_mod.claim, self.cfg.state_dir, self.mode, self.chat_id,
                                                   self._ph_reply_to, _thread_field(thread))
        if self._ph is not None and not self._ph.done:
            return self._ph
        return None

    def _can_edit(self, ph: ph_mod.Placeholder | None, effect: str | None = None) -> bool:
        """An edit cannot carry a message effect, and an edit does not notify: past
        TG_PLACEHOLDER_EDIT_MAX_AGE the answer is sent as a new message instead."""
        if ph is None or ph.done or effect:
            return False
        if getattr(self.cfg, "placeholder_mode", "done") != "edit":
            return False  # v4.3.2: answer is always a new message (notifies)
        limit = self.cfg.placeholder_edit_max_age
        return not (limit and ph.age > limit)

    async def _edit_placeholder(self, ph: ph_mod.Placeholder, **fields) -> bool:
        """editMessageText on the placeholder (text+parse_mode or rich_message).
        True if it now shows the answer; False if Telegram refused (caller falls back)."""
        try:
            await self._call(lambda: self.bot.edit_message_text(chat_id=self.chat_id, message_id=ph.message_id,
                                                                **fields))
        except TelegramBadRequest as e:
            low = e.message.lower()
            if "not modified" in low:
                pass
            elif fields.get("parse_mode") and "parse" in low:
                try:
                    await self._call(lambda: self.bot.edit_message_text(
                        chat_id=self.chat_id, message_id=ph.message_id,
                        text=_strip_tags(fields.get("text") or "")[:4096], parse_mode=None))
                except TelegramAPIError:
                    return False
            else:
                return False
        except TelegramAPIError:
            return False
        ph.done = True
        await self._drop_sticker()
        return True

    async def _drop_sticker(self) -> None:
        """v4.4: delete the thinking sticker that was sent with the placeholder.
        Must never break the answer."""
        ph = self._ph
        mid = getattr(ph, "sticker_message_id", None) if ph is not None else None
        if not mid:
            return
        ph.sticker_message_id = None  # try only once
        try:
            await self._call(lambda: self.bot.delete_message(chat_id=self.chat_id, message_id=int(mid)))
        except TelegramAPIError:
            pass

    async def _drop_placeholder(self) -> None:
        """The answer went out as new message(s): remove the placeholder. If Telegram
        will not delete it, at least stop it saying "Думаю..."."""
        ph = self._ph
        if ph is None or ph.done:
            return
        ph.done = True
        await self._drop_sticker()
        mode = getattr(self.cfg, "placeholder_mode", "done")
        if mode == "keep":
            # v4.4.1: leave "Думаю..." exactly as it is (only the sticker goes).
            return
        if mode == "done":
            # v4.3.2 default: keep the placeholder, just mark it finished.
            try:
                await self._call(lambda: self.bot.edit_message_text(chat_id=self.chat_id, message_id=ph.message_id,
                                                                    text="\u2705", parse_mode=None))
            except TelegramAPIError:
                pass
            return
        try:
            await self._call(lambda: self.bot.delete_message(chat_id=self.chat_id, message_id=ph.message_id))
        except TelegramAPIError:
            try:
                await self._call(lambda: self.bot.edit_message_text(chat_id=self.chat_id, message_id=ph.message_id,
                                                                    text="\u2705", parse_mode=None))
            except TelegramAPIError:
                pass

    def finish_placeholder(self) -> None:
        """End of a tg-send run: a claimed placeholder that was not used (the send
        failed) is given back, so the routine's next attempt can still replace it."""
        ph = self._ph
        if ph is not None and not ph.done:
            ph_mod.release(self.cfg.state_dir, self.chat_id, ph, self._ph_reply_to)
            self._ph = None

    # -- commands ---------------------------------------------------------
    async def send_rich(self, text: str, *, html: bool = False, reply_to: int | None = None,
                        silent: bool = False, effect: str | None = None, thread: int | None = None,
                        fallback: bool = True) -> list[int]:
        """Send one Rich Message (sendRichMessage). On a documented limit or an API
        error, fall back to the classic sendMessage path (with splitting) unless
        fallback is False."""
        if not text.strip():
            raise Refused("refused: empty message")
        self.check_secret_free(text)
        try:
            validate_rich(text)
        except RichLimitError as e:
            if not fallback:
                raise Refused(f"refused: {e}") from e
            return await self._send_text_classic(text, "html" if html else "md",
                                                 reply_to, silent, effect, thread)
        thread = _thread_field(thread)
        rm = build_rich_message(text, html=html)
        ph = await self._take_placeholder(reply_to, thread)
        await self._safe_action("typing", thread)
        if self._can_edit(ph, effect) and await self._edit_placeholder(ph, rich_message=rm):
            # Bot API 10.1: editMessageText accepts rich_message. The placeholder already
            # replies to the user's message, so the answer keeps that reply.
            mark_outbound(self.cfg.state_dir)
            self._remember(text, ph.message_id, thread)
            return [ph.message_id]
        common = dict(chat_id=self.chat_id, disable_notification=silent or None,
                      message_thread_id=thread, reply_parameters=self._reply_params(reply_to),
                      message_effect_id=effect)
        try:
            msg = await self._call(lambda: self.bot.send_rich_message(rich_message=rm, **common))
        except TelegramAPIError:
            if not fallback:
                raise
            return await self._send_text_classic(text, "html" if html else "md",
                                                 reply_to, silent, effect, thread)
        await self._drop_placeholder()
        mark_outbound(self.cfg.state_dir)
        self._remember(text, msg.message_id, thread)
        return [msg.message_id]

    async def send_rich_draft(self, text: str, draft_id: int, *, html: bool = False,
                              can_stop: bool = False, keep_on_stop: bool = False,
                              thread: int | None = None) -> None:
        """sendRichMessageDraft: a 30-second streaming preview of a rich message."""
        self.check_secret_free(text)
        if text.strip():
            rm = build_rich_message(text, html=html)
        else:
            # Official draft-only tag: a "Thinking…" placeholder.
            rm = InputRichMessage(html="<tg-thinking>Thinking…</tg-thinking>")
        await self._call(lambda: self.bot.send_rich_message_draft(
            chat_id=self.chat_id, draft_id=int(draft_id), rich_message=rm,
            message_thread_id=_thread_field(thread),
            can_stop=can_stop or None, keep_on_stop=keep_on_stop or None))

    async def send_text(self, text: str, fmt: str = "md", reply_to: int | None = None, silent: bool = False,
                        effect: str | None = None, thread: int | None = None,
                        classic: bool = False, rich: bool = False) -> list[int]:
        """Send a text message. Default is classic sendMessage (Markdown rendered to
        Telegram HTML, split at 4096, plain fallback). Rich (sendRichMessage) is used
        only when `--rich` is set, or when the text actually contains headings, tables
        or <details> (auto-detect). `--classic` forces the classic path (disables
        auto-detect; a no-op for ordinary short replies). fmt=plain is never Rich."""
        if should_send_rich(text, rich=rich, classic=classic, fmt=fmt):
            return await self.send_rich(text, html=(fmt == "html"), reply_to=reply_to,
                                        silent=silent, effect=effect, thread=thread, fallback=True)
        return await self._send_text_classic(text, fmt, reply_to, silent, effect, thread)

    async def _send_text_classic(self, text: str, fmt: str = "md", reply_to: int | None = None,
                                 silent: bool = False, effect: str | None = None,
                                 thread: int | None = None) -> list[int]:
        if not text.strip():
            raise Refused("refused: empty message")
        self.check_secret_free(text)
        ids: list[int] = []
        parts = render_parts(text, fmt)
        thread = _thread_field(thread)
        ph = await self._take_placeholder(reply_to, thread)
        await self._safe_action("typing", thread)
        for n, (part, mode) in enumerate(parts):
            if n == 0 and self._can_edit(ph, effect if len(parts) == 1 else None):
                if await self._edit_placeholder(ph, text=part, parse_mode=mode):
                    ids.append(ph.message_id)
                    continue
            common = dict(chat_id=self.chat_id, disable_notification=silent or None,
                          message_thread_id=thread,
                          reply_parameters=self._reply_params(reply_to) if n == 0 else None,
                          message_effect_id=effect if n == len(parts) - 1 else None)
            try:
                msg = await self._call(lambda: self.bot.send_message(text=part, parse_mode=mode, **common))
            except TelegramBadRequest as e:
                if mode and "parse" in e.message.lower():
                    # Formatting rejected: resend this part as plain text.
                    for sub in split_plain(_strip_tags(part)):
                        m2 = await self._call(lambda: self.bot.send_message(text=sub, parse_mode=None, **common))
                        ids.append(m2.message_id)
                        await self._drop_placeholder()
                    continue
                raise
            ids.append(msg.message_id)
            await self._drop_placeholder()
        mark_outbound(self.cfg.state_dir)
        self._remember(text, ids[0] if ids else None, thread)
        return ids

    async def typing(self, action: str = "typing", thread: int | None = None) -> None:
        await self._call(lambda: self.bot.send_chat_action(chat_id=self.chat_id, action=action,
                                                           message_thread_id=_thread_field(thread)))

    async def react(self, message_id: int, emoji: str | None, clear: bool = False, big: bool = False) -> None:
        reaction = [] if clear else [ReactionTypeEmoji(emoji=emoji or "\U0001F44D")]
        await self._call(lambda: self.bot.set_message_reaction(chat_id=self.chat_id, message_id=int(message_id),
                                                               reaction=reaction, is_big=big or None))

    async def send_file(self, path_str: str, kind: str, caption: str | None, reply_to: int | None,
                        thread: int | None = None) -> int:
        limit = PHOTO_MAX if kind == "photo" else FILE_MAX
        path = self.check_file(path_str, limit)
        if caption:
            self.check_secret_free(caption)
        common = dict(chat_id=self.chat_id, caption=(caption or None) and caption[:1024],
                      message_thread_id=_thread_field(thread), reply_parameters=self._reply_params(reply_to),
                      request_timeout=180)
        # A text placeholder cannot become a photo/file (editMessageMedia edits media
        # messages only): claim it, send the file, then delete the placeholder.
        await self._take_placeholder(reply_to, thread)
        await self._safe_action("upload_photo" if kind == "photo" else "upload_document", thread)
        if kind == "photo":
            msg = await self._call(lambda: self.bot.send_photo(photo=FSInputFile(path), **common))
        else:
            msg = await self._call(lambda: self.bot.send_document(document=FSInputFile(path), **common))
        await self._drop_placeholder()
        mark_outbound(self.cfg.state_dir)
        label = f"[{'photo' if kind == 'photo' else 'file'}: {path.name}]"
        self._remember(f"{label} {caption}".strip() if caption else label, msg.message_id, thread)
        return msg.message_id

    async def _safe_action(self, action: str, thread: int | None = None) -> None:
        try:
            await self.typing(action, thread=thread)
        except TelegramAPIError:
            pass

    async def edit(self, message_id: int, text: str, fmt: str = "md") -> None:
        self.check_secret_free(text)
        part, mode = render_parts(text, fmt)[0]
        if utf16_len(part) > TG_LIMIT:
            raise Refused("refused: edited text is too long")
        try:
            await self._call(lambda: self.bot.edit_message_text(chat_id=self.chat_id, message_id=int(message_id),
                                                                text=part, parse_mode=mode))
        except TelegramBadRequest as e:
            if mode and "parse" in e.message.lower():
                await self._call(lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=int(message_id), text=_strip_tags(part)[:4096],
                    parse_mode=None))
            elif "not modified" in e.message.lower():
                pass
            else:
                raise
        mark_outbound(self.cfg.state_dir)

    async def delete(self, message_id: int) -> None:
        await self._call(lambda: self.bot.delete_message(chat_id=self.chat_id, message_id=int(message_id)))

    async def draft(self, text: str, draft_id: int, fmt: str = "plain", can_stop: bool = False,
                    thread: int | None = None) -> None:
        """sendMessageDraft (Bot API 9.3+): a 30-second streaming preview. Empty text = "Thinking…"."""
        self.check_secret_free(text)
        if text:
            part, mode = render_parts(text, fmt)[-1] if fmt != "plain" else (split_plain(text)[-1], None)
        else:
            part, mode = "", None
        await self._call(lambda: self.bot.send_message_draft(
            chat_id=self.chat_id, draft_id=int(draft_id), text=part, parse_mode=mode,
            can_stop=can_stop or None, message_thread_id=_thread_field(thread)))

    async def stream(self, source, fmt: str = "md", reply_to: int | None = None, interval: float = 1.0,
                     clock=time.monotonic, thread: int | None = None,
                     classic: bool = False, rich: bool = False) -> list[int]:
        """Read text incrementally (e.g. from stdin), show it as a live draft, then send it for real.
        Drafts follow the same format choice as send_text (classic by default; Rich if
        `--rich` or the buffer auto-detects headings/tables/<details>)."""
        draft_id = int(time.time() * 1000) % 2_000_000_000 or 1
        stop_marker = self.cfg.state_dir / "stopped" / str(draft_id)
        buf = ""
        last = 0.0
        use_rich = bool(rich and fmt != "plain")
        await self._safe_action("typing", thread)
        try:
            if use_rich:
                await self.send_rich_draft("", draft_id, can_stop=True, thread=thread)
            else:
                await self.draft("", draft_id, can_stop=True, thread=thread)
        except TelegramAPIError:
            use_rich = False
            try:
                await self.draft("", draft_id, can_stop=True, thread=thread)
            except TelegramAPIError:
                pass
        stop_flag = threading.Event()
        poller = None
        if self.mode == "serverless":
            # "Stop" taps arrive at Serverless (stopped_message_generation); poll it.
            chat = self.chat_id

            def poll():
                while not stop_flag.is_set():
                    if wait_stop(chat, draft_id):
                        stop_flag.set()
                        return
                    stop_flag.wait(3)
            poller = threading.Thread(target=poll, daemon=True)
            poller.start()
        for chunk in source:
            buf += chunk
            if stop_marker.exists() or (poller is not None and stop_flag.is_set()):
                break
            now = clock()
            if (not use_rich and not classic and fmt != "plain" and looks_rich(buf)):
                use_rich = True
            if now - last >= interval and buf.strip():
                last = now
                await self._safe_action("typing", thread)
                tail = buf[-4000:]
                try:
                    if use_rich:
                        await self.send_rich_draft(tail, draft_id, can_stop=True, thread=thread)
                    else:
                        await self.draft(tail, draft_id, fmt="plain", can_stop=True, thread=thread)
                except TelegramAPIError:
                    if use_rich:
                        use_rich = False
                        try:
                            await self.draft(tail, draft_id, fmt="plain", can_stop=True, thread=thread)
                        except TelegramAPIError:
                            pass
        stop_flag.set()
        if not buf.strip():
            raise Refused("refused: nothing to send")
        return await self.send_text(buf.rstrip("\n"), fmt=fmt, reply_to=reply_to, thread=thread,
                                    classic=classic, rich=rich)

    async def create_topic(self, name: str, icon_color: int | None = None) -> dict:
        name = (name or "").strip()
        if not (1 <= len(name) <= 128):
            raise Refused("refused: topic name must be 1-128 characters")
        self.check_secret_free(name)
        if icon_color is not None and icon_color not in TOPIC_ICON_COLORS:
            raise Refused("refused: icon_color is not one of the colours Telegram allows")
        result = await self._call(lambda: self.bot.create_forum_topic(chat_id=self.chat_id, name=name,
                                                                      icon_color=icon_color))
        thread_id = result.message_thread_id
        if thread_id and self.mode == "serverless":
            try:
                queue_record(self.cfg.state_dir, {"kind": "topic", "chat_id": self.chat_id,
                                                  "thread_id": int(thread_id), "name": result.name or name})
                self._queued = True
            except OSError:
                pass
        if thread_id:
            from .state import State
            State(self.cfg.state_dir).remember_topic(int(thread_id), name=result.name or name,
                                                     icon_color=result.icon_color or icon_color)
        mark_outbound(self.cfg.state_dir)
        return {"message_thread_id": thread_id, "name": result.name or name,
                "icon_color": result.icon_color}

    async def enable_topics(self) -> dict:
        await self._call(lambda: self.bot(SetMySettings(has_topics_enabled=True, has_topic_sidebar=True,
                                                        allows_users_to_create_topics=True)))
        return await self.topic_status()

    async def topic_status(self) -> dict:
        info = me_info(await self._call(self.bot.get_me))
        info.pop("first_name", None)
        return info

    async def set_photo(self, path_str: str) -> dict:
        path = self.check_file(path_str, PHOTO_MAX)
        jpg, cleanup = as_jpeg(path)
        try:
            await self._call(lambda: self.bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(photo=FSInputFile(jpg, filename="avatar.jpg"))))
        finally:
            if cleanup:
                try:
                    jpg.unlink()
                except OSError:
                    pass
        me = await self._call(self.bot.get_me)
        photos = await self._call(lambda: self.bot.get_user_profile_photos(user_id=me.id, limit=1))
        return {"bot_id": me.id, "username": me.username, "total_count": photos.total_count}


def as_jpeg(path: Path) -> tuple[Path, bool]:
    """Return a JPEG path and whether the caller should delete it.

    Bot API setMyProfilePhoto accepts a static InputProfilePhoto only as JPG.
    """
    raw = path.read_bytes()[:3]
    if raw[:2] == b"\xff\xd8" and path.suffix.lower() in (".jpg", ".jpeg"):
        return path, False
    fd, name = tempfile.mkstemp(prefix="tg-avatar-", suffix=".jpg")
    os.fchmod(fd, 0o600)
    os.close(fd)
    dest = Path(name)
    try:
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.save(dest, "JPEG", quality=95)
            return dest, True
        except Exception:
            ffmpeg = _which("ffmpeg")
            if not ffmpeg:
                raise Refused("refused: profile photo must be a JPEG (or install Pillow/ffmpeg to convert)")
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                 "-frames:v", "1", "-q:v", "2", str(dest)],
                check=False, capture_output=True, text=True,
            )
            if proc.returncode != 0 or dest.stat().st_size == 0:
                raise Refused("refused: could not convert the image to JPEG")
            return dest, True
    except Exception:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


def _which(name: str) -> str | None:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(folder) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _strip_tags(s: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _add_thread(p: argparse.ArgumentParser) -> None:
    p.add_argument("--thread", type=int, help="message_thread_id of a private-chat topic")


def _add_placeholder(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-placeholder", action="store_true",
                   help="leave the bridge's '🗨️ Думаю...' placeholder alone (by default the first "
                        "answer with --reply-to replaces it)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg-send", description="Send a message to an allowlisted Telegram chat.")
    p.add_argument("--chat", help="numeric chat id: TG_OWNER_ID (default) or an id from TG_ALLOWED_USER_IDS")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("text", help="send a message (classic sendMessage by default; Rich if --rich or auto-detected)")
    t.add_argument("text", nargs="?", help="message text, or '-'/nothing to read stdin")
    t.add_argument("--format", choices=["md", "html", "plain"], default="md")
    t.add_argument("--rich", action="store_true",
                   help="force a Rich Message (sendRichMessage); also used automatically for headings, tables, <details>")
    t.add_argument("--classic", action="store_true",
                   help="force classic sendMessage (the default for short replies; disables auto-Rich)")
    t.add_argument("--reply-to", type=int)
    t.add_argument("--silent", action="store_true")
    t.add_argument("--effect", help="message_effect_id (private chats only)")
    _add_thread(t)
    _add_placeholder(t)

    rich = sub.add_parser("rich", help="send a Rich Message (Bot API 10.1+): Markdown, or --html")
    rich.add_argument("text", nargs="?", help="message text, or '-'/nothing to read stdin")
    rich.add_argument("--html", action="store_true", help="treat the input as Rich HTML, not Markdown")
    rich.add_argument("--no-fallback", action="store_true",
                      help="do not fall back to sendMessage if sendRichMessage is rejected")
    rich.add_argument("--reply-to", type=int)
    rich.add_argument("--silent", action="store_true")
    rich.add_argument("--effect", help="message_effect_id (private chats only)")
    _add_thread(rich)
    _add_placeholder(rich)

    s = sub.add_parser("stream", help="read stdin gradually, show a live draft, then send")
    s.add_argument("--format", choices=["md", "html", "plain"], default="md")
    s.add_argument("--rich", action="store_true", help="force Rich drafts and a Rich final message")
    s.add_argument("--classic", action="store_true", help="force classic drafts and a classic final message")
    s.add_argument("--reply-to", type=int)
    _add_thread(s)
    _add_placeholder(s)

    d = sub.add_parser("draft", help="update a streaming draft (sendMessageDraft)")
    d.add_argument("text", nargs="?", default="")
    d.add_argument("--draft-id", type=int, required=True)
    _add_thread(d)

    typ = sub.add_parser("typing", help="show 'typing…'")
    typ.add_argument("--action", default="typing",
                     choices=["typing", "upload_photo", "upload_document", "record_voice", "upload_voice"])
    typ.add_argument("--keep", type=int, default=0, metavar="SECONDS",
                     help="accepted for compatibility with older routine prompts; "
                          "sends a single typing action and returns (no background loop)")
    typ.add_argument("--since", type=float, default=None, help=argparse.SUPPRESS)
    _add_thread(typ)

    r = sub.add_parser("react", help="set a reaction on a message")
    r.add_argument("message_id", type=int)
    r.add_argument("--emoji", default="\U0001F44D")
    r.add_argument("--clear", action="store_true")
    r.add_argument("--big", action="store_true")

    for kind in ("photo", "file"):
        f = sub.add_parser(kind, help=f"send a {kind}")
        f.add_argument("path")
        f.add_argument("--caption")
        f.add_argument("--reply-to", type=int)
        _add_thread(f)
        _add_placeholder(f)

    e = sub.add_parser("edit", help="edit a message the bot sent")
    e.add_argument("message_id", type=int)
    e.add_argument("text", nargs="?")
    e.add_argument("--format", choices=["md", "html", "plain"], default="md")

    sub.add_parser("delete", help="delete a message the bot sent").add_argument("message_id", type=int)

    tp = sub.add_parser("topic", help="create a topic (a separate chat) in the owner's private chat")
    tp.add_argument("name", help="topic name, 1-128 characters")
    tp.add_argument("--icon-color", type=int, choices=list(TOPIC_ICON_COLORS))

    sub.add_parser("enable-topics", help="enable private-chat topic mode, sidebar, and user-created topics")
    sp = sub.add_parser("set-photo", help="set the bot's profile photo (PNG is converted to JPEG)")
    sp.add_argument("path")
    # --chat is accepted before or after the subcommand ("tg-send text --chat ID …"
    # is the form printed in how_to_reply). SUPPRESS keeps a value given before it.
    for subparser in sub.choices.values():
        subparser.add_argument("--chat", default=argparse.SUPPRESS,
                               help="numeric chat id: TG_OWNER_ID (default) or an id from TG_ALLOWED_USER_IDS")
    return p


def _control_fallback(cmd: str, args, environ) -> tuple[int, dict] | None:
    """If the token is not in this process, ask the running relay to do it."""
    if cmd not in ("set-photo", "enable-topics", "topic"):
        return None
    payload: dict
    if cmd == "set-photo":
        payload = {"action": "set_photo", "path": str(Path(args.path).expanduser().resolve())}
    elif cmd == "enable-topics":
        payload = {"action": "enable_topics"}
    else:
        payload = {"action": "create_topic", "name": args.name, "icon_color": args.icon_color}
    try:
        data = submit_request(state_dir_only(environ), payload)
    except ControlError as e:
        return 1, {"ok": False, "error": str(e)}
    if not data.get("ok"):
        return 1, data
    return 0, data


async def _run(args, cfg, bot) -> tuple[int, dict]:
    own_bot = bot is None
    s = Sender(cfg, bot=bot)
    try:
        s.check_chat(args.chat)
        s.use_placeholder = not getattr(args, "no_placeholder", False)
        result: dict = {"ok": True}
        thread = getattr(args, "thread", None)
        if args.cmd == "text":
            result["message_ids"] = await s.send_text(_read_text(args.text), args.format, args.reply_to,
                                                      args.silent, args.effect, thread=thread,
                                                      classic=getattr(args, "classic", False),
                                                      rich=getattr(args, "rich", False))
        elif args.cmd == "rich":
            result["message_ids"] = await s.send_rich(_read_text(args.text), html=args.html,
                                                      reply_to=args.reply_to, silent=args.silent,
                                                      effect=args.effect, thread=thread,
                                                      fallback=not args.no_fallback)
        elif args.cmd == "stream":
            result["message_ids"] = await s.stream(iter(lambda: sys.stdin.readline(), ""), args.format,
                                                   args.reply_to, thread=thread,
                                                   classic=getattr(args, "classic", False),
                                                   rich=getattr(args, "rich", False))
        elif args.cmd == "draft":
            await s.draft(args.text, args.draft_id, thread=thread)
        elif args.cmd == "typing":
            # --keep / --since stay accepted so a saved routine prompt that still
            # runs `tg-send typing --keep N` does not break. No background loop:
            # Serverless has no timers, and a keep-alive on the box dies when it freezes.
            if getattr(args, "keep", 0):
                await s._safe_action(args.action, thread=thread)
            else:
                await s.typing(args.action, thread=thread)
        elif args.cmd == "react":
            await s.react(args.message_id, args.emoji, args.clear, args.big)
        elif args.cmd in ("photo", "file"):
            result["message_id"] = await s.send_file(args.path, args.cmd, args.caption, args.reply_to,
                                                     thread=thread)
        elif args.cmd == "edit":
            await s.edit(args.message_id, _read_text(args.text), args.format)
        elif args.cmd == "delete":
            await s.delete(args.message_id)
        elif args.cmd == "topic":
            result.update(await s.create_topic(args.name, args.icon_color))
        elif args.cmd == "enable-topics":
            result.update(await s.enable_topics())
        elif args.cmd == "set-photo":
            result.update(await s.set_photo(args.path))
        return 0, result
    except Refused as e:
        return e.code, {"ok": False, "error": str(e)}
    except TelegramAPIError as e:
        return 1, {"ok": False, "error": s.redact(str(e))}
    finally:
        s.finish_placeholder()
        s.flush_memory()
        if own_bot:
            await s.bot.session.close()


def main(argv: list[str] | None = None, stdin=None, bot=None, environ=None) -> int:
    args = build_parser().parse_args(argv)
    if stdin is not None:
        sys.stdin = stdin
    try:
        cfg = load_config(need_routine=False, environ=environ)
    except ConfigError as e:
        fallback = _control_fallback(args.cmd, args, environ)
        if fallback is not None:
            code, result = fallback
            print(json.dumps(result, ensure_ascii=False))
            return code
        print(json.dumps({"ok": False, "error": f"config: {e}"}))
        return 2
    code, result = asyncio.run(_run(args, cfg, bot))
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
