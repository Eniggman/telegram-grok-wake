# Changelog

Versioning: after each round of repairs the version goes up by 0.1 (4.5 → 4.6 → 4.7 …),
and every release heading carries its date (YYYY-MM-DD, Europe/Kiev).

## 4.6.0 — 2026-11-26
Open-source cleanup and maintenance release.

### Changed
- Weather widget default city is `kyiv` (`tests/fixtures/meteoblue_kyiv.json`).
- Unified Kyiv timezone fallback (`Europe/Kyiv` ↔ `Europe/Kiev`) via `tgbridge/tz.py` and used it in `tgbridge/widgets/weather.py`.
- Updated `README.md` and synced Serverless `VERSION` and box `__version__` to `4.6.0`.

### Removed
- Removed unused `withUpdatingMark` export from `serverless/tgcloud/lib/usage_card.js`.

## 4.5.0 — 2026-11-26
Consolidated release: the 4.4.2 / 4.4.3 repairs, the unreleased items and the
weather widget are now one numbered version.

### Fixed
- A comment plus a forwarded post is still one wake, but saved history no longer
  marks the whole turn as forwarded. The sender's own text is stored as
  `role=user` (not forwarded). The forwarded content is a separate row with
  `forwarded: true` (someone else's data).
### Changed
- Default retention `history_days` / `TG_HISTORY_DAYS` is 7 days.
  The window is `history_messages` 20 and `history_max_chars` 12000.
- A history item longer than `ITEM_MAX_CHARS` (4000) keeps its head and its
  tail, with `… [пропуск] …` in the middle, and the result stays within that
  limit. The same cut is used for the polling-mode history.
- `tg-history --limit` defaults to 30 (maximum stays 500). The routine prompt
  says to read further back only when needed, about 30 messages.
- Serverless `VERSION` and box `__version__` are 4.5.0.

### Fixed
- Forwarding a channel post **with a comment** no longer produces two thinking stickers / «Думаю...» placeholders / wakes. The comment plus the forwarded message(s) (also a forwarded album) that follow within ~1.5 s are now one burst: the first message sends the placeholder+sticker, waits, then claims all parts (new `burst_parts` table, pruned in housekeeping) and delivers once. `message.text` stays the comment; forwarded content goes to `message.forwarded[]` (with `forward_origin`, marked as someone else's data) and forwarded attachments are flagged `forwarded: true`. Plain text messages now wait ~1.5 s before delivery (placeholder is still immediate); commands and albums are unchanged.
### Added
- `/usage` card API block: 10-day daily $ bar chart and a last-3-days table by
  token type (Ввод / Выход / Кэш / Прочее) plus итого, from `bin/xai-usage` and
  `POST /v1/billing/teams/{id}/usage`. Timestamps are converted to Europe/Kiev
  (a UTC midnight instant must not be sliced as `[:10]`). Prepaid balance is
  kept (purchases are negated). Team id comes from `XAI_TEAM_ID` or a local
  gitignored `xai-team-id` file; the Management key only from `XAI_MANAGEMENT_KEY`.
- Setup docs (Russian): `docs/USAGE_CARD.md`, `docs/WEATHER.md`.
- Menu command **/weather** (owner only, relayed to the routine as `command: "weather"`): sends the week weather widget via `bin/tg-weather-widget send`. Registered with `tg-cloud commands`; deployed.

### Changed
- `/usage` card no longer shows the API «Баланс: $…» line (Management prepaid
  is not the grok.com credit balance).
- Usage card review fixes: a failed usd/units half is omitted (never a fake $0
  chart) with `api_note` «данные API частично недоступны»; empty `api_days` /
  `api_breakdown` stubs are not drawn as zeros; refresh no longer replaces the
  HTML card with «⏳ обновляю…» and restores the last card plus «не удалось
  обновить» if the wake fails; `answerCallbackQuery` once, including unknown
  callbacks; correct Russian plural for «Итого за N …».
- Weather widget layout (owner's request): every forecast cell now reads
  temperature + 💧 humidity % → icon → ☔ precipitation probability % → 💨 wind.
  Daily rows: day | min / **max** with 💧 humidity | icon | ☔ probability (mm small
  underneath) | wind min–max, tabular numbers aligned across rows; condition text
  dropped. Hourly strip: time, temp, 💧 humidity, icon, ☔ probability (mm small, only
  when > 0), wind. `build_state` adds hourly `precipitation_probability` as the 6th
  value of each hour (0 if meteoblue omits it). Tests cover the order and the fallback.

### Added — weather widget (first shipped 2026-11-24 under the 4.5.0 label)
- **Weather widget** (Bot API 10.4 `sendWidget`): `bin/tg-weather-widget`
  (`tgbridge/widgets/weather.py` + `weather.html`). Kyiv now / 24 h / 7 days from
  meteoblue (`basic-1h_basic-day`), Frutiger Aero look, inline SVG icons from the
  meteoblue pictocodes, meteoblue attribution link. Code is sent once; the forecast
  lives in `public_data` (≤1024 B) so `update` uses `editMessageWidgetData`.
  Limits (32768 B HTML, 4096 B zlib-9, 1024 B state) are checked before sending.
  `render --preview` writes a headless-Chrome PNG to `widgets/preview.png`.
- Telegram currently refuses widgets for general bots (`INPUT_MEDIA_WIDGET_NOT_ALLOWED`,
  since 2026-11-13, tdlib/telegram-bot-api#947). `send` then posts the preview PNG as a
  photo (exit code 3); `--no-fallback` disables that. Not retried.
- City is configurable: `--city` preset (`kyiv` default) or env
  `TG_WEATHER_CITY`; `--name/--lat/--lon/--asl/--slug` for any place. City name and
  meteoblue page slug travel in the widget data. Daily rows: range bar removed,
  condition text + min / **max** instead.
- Tests: `tests/test_weather_widget.py` (offline fixture, fake bot).

## 4.4.1 — 2026-11-24
### Changed
- New placeholder mode **keep**, now the default (TG_PLACEHOLDER_MODE): after the
  answer the «🗨️ Думаю...» placeholder is left as it is (no ✅ edit); the thinking
  sticker is still deleted. TG_PLACEHOLDER_MODE=done restores the ✅ behaviour.

## 4.4.0 — 2026-11-24
### Added
- **Thinking sticker.** After the «🗨️ Думаю...» placeholder, Serverless sends a
  random sticker from the pack `VikostVSpack` (`getStickerSet`, then `sendSticker`)
  into the same chat and topic. A new sticker is picked for every incoming
  message (owner and guest alike). File ids are cached in the Serverless `kv`
  table (`_sticker_set:<pack>`, 24 h TTL) so new stickers in the pack are picked
  up by the next day. Setting: `sticker_pack` (env `TG_STICKER_PACK`; a
  `t.me/addstickers/…` URL is accepted); `off` disables. `bin/tg-cloud setup`
  copies it with the other settings.
- The sticker's `message_id` is stored on the placeholder record
  (`placeholders.sticker_message_id`) and on the webhook event as
  `sticker_message_id`.
### Changed
- The first `tg-send` answer for that message **deletes the sticker**. The
  placeholder itself stays and behaves as before (default mode `done`: it
  becomes ✅, the answer is a new message). A failed delete never breaks the
  answer; a failed send leaves the sticker in place. The sticker is not stored
  as conversation history. A sticker/API failure never blocks the placeholder
  or the routine POST.

## 4.3.2 — 2026-11-24
### Changed
- **Typing only when the bot is actually replying.** Serverless no longer sends
  `sendChatAction` "typing" after the «🗨️ Думаю...» placeholder (there are no
  timers on the platform, so the status vanished after ~5 s; a keep-alive on the
  box also dies when the VM freezes between steps). The placeholder is unchanged.
- `tg-send` itself sends one typing action immediately before the final
  text / rich / photo / file send, and refreshes it while `stream` is running.
- `tg-send typing --keep N` stays accepted (the saved routine prompt still calls
  it as a first step) but is now harmless: one typing action and return, no
  background refresh loop and no timeout notice.
- Default placeholder mode is `done` (`TG_PLACEHOLDER_MODE`): the answer goes
  out as a new message (so it notifies), and the placeholder is edited to ✅
  instead of being overwritten or deleted. `edit` restores the previous
  edit-in-place behaviour.

## 4.3.1 — 2026-11-24
- Answers and the placeholder are plain messages, with no reply quote
  (set serverless kv `reply_quote=on` or env `TG_REPLY_QUOTE=1` to restore it).

## 4.3.0 — 2026-11-23
### Added
- **Placeholder "🗨️ Думаю..."** instead of the 👀 reaction (which is now off by
  default; set `TG_REACTION=👀` to restore it). On intake the bot replies with the
  placeholder and records `(chat_id, reply_to)` → its `message_id`. The first
  `tg-send --reply-to` claims that record atomically and turns the placeholder
  into the answer. The event carries `placeholder_message_id` and
  `placeholder_note`. `TG_PLACEHOLDER` (`off` disables) and
  `TG_PLACEHOLDER_EDIT_MAX_AGE` (0 = always try an edit) control the behaviour.
- New box/Serverless hand-off: table `placeholders`, box ops `placeholder_claim` /
  `placeholder_release`, local SQLite cache (`placeholders.sqlite3`). A failed
  send releases the claim so the next attempt can still replace the placeholder.
  A second `tg-send` for the same message never overwrites the first answer.
  Photos/files cannot be an edit of the text placeholder: the file is sent, then
  the placeholder is deleted (or edited to ✅ if delete is refused).
- Failure paths reuse the placeholder: a dead delivery, a stale pending delivery or a
  handler exception turns the still-active placeholder into the existing ⚠️ notice
  (atomic claim, so an answer already put there is never overwritten); without one
  the notice is sent as before.
- `--no-placeholder` on `tg-send text|rich|stream|photo|file` leaves the placeholder
  alone. Without `--reply-to`, tg-send uses a placeholder only if it is the single
  active one in that chat/topic (< 1 h old). Serverless claims cost one
  `tgcloud run` (~1 s) per answered message; repeat sends use the local cache.
- The polling fallback relay (`tg-mode polling`) sends and hands off the same
  placeholder through the local cache.
### Notes
- Verified live: Bot API accepts `editMessageText` with `rich_message` on a plain
  text message, so a Rich answer can replace the text placeholder.
- An edit does not trigger a new Telegram notification. If long tasks should ping
  the owner when done, set e.g. `TG_PLACEHOLDER_EDIT_MAX_AGE=60` (older placeholders
  are deleted and the answer is sent as a new message).
- `tg-send text --rich` (and the existing `rich` subcommand) to force a Rich
  Message. Auto-detect also selects Rich when the text contains ATX headings,
  GFM or HTML tables, `<details>`, or `<h1>`–`<h6>` (fenced code is ignored).

### Changed
- **`tg-send text` default is classic `sendMessage` again** (Markdown rendered to
  Telegram HTML, split at 4 096, plain fallback). Rich (`sendRichMessage`) is used
  only when `--rich` / `tg-send rich` is requested, or when auto-detect finds
  headings, tables or `<details>`. `--classic` remains as a compatibility alias
  that forces the classic path (a no-op for ordinary short replies; it disables
  auto-detect). `--format plain` is never Rich.
- Replacing the placeholder follows the same format choice: a classic answer is
  `editMessageText` (text + parse_mode); a Rich answer is `editMessageText` with
  `rich_message` (Bot API 10.1+). If that edit is impossible (effect, age, API
  refusal), the placeholder is deleted and a new message is sent.
- `tg-send stream` follows the same rule: classic drafts by default, Rich drafts
  if `--rich` or the buffer looks structured.
- ROUTINE_PROMPT.md: How to reply now describes the placeholder flow and when to
  use Rich vs classic. Do not send a preliminary "Принял, делаю…" — that would
  consume the placeholder.

## 4.2.0 — 2026-11-23
### Added
- **Telegram command menu** (Bot API `setMyCommands`). Owner chat (`BotCommandScopeChat`):
  `/new`, `/skills`, `/usage`; default and all private chats (guests): `/new` only. Menu button
  is `commands`. Repeatable and idempotent: `bin/tg-cloud commands` (`--show` only reads back via
  `getMyCommands`/`getChatMenuButton`). Runs inside Serverless (`endpoints/box` ops
  `commands_set` / `commands_get`) — no bot token on the box is involved.
- `/skills` and `/usage` (also `/cmd@BotName`). Guests get «Эта команда доступна только
  владельцу.» locally. For the owner the message is relayed to the routine with the new top-level
  event field `command` (`"skills"` | `"usage"`); `message.text` is kept.
- `/skills` also attaches `skill_source` `{thread_id, topic_name, history}`: the conversation with
  the latest non-command user message (the current topic if it is the latest, else the most
  recent other one), up to 60 messages / `max(24000, 3×history_max_chars)` chars.
- ROUTINE_PROMPT.md: paragraph for the `command` field.
### Changed
- `/skills` and `/usage` are not written to conversation memory (they are service commands);
  the routine's reply via `tg-send` is stored as before.
- Serverless `VERSION` and box `__version__` are now both 4.2.0 (core.js was still 4.0.0).

## 4.1.0 — 2026-11-23
### Added
- **Telegram Rich Messages** (Bot API 10.1–10.4). `tg-send` calls `sendRichMessage` with
  `InputRichMessage` (`markdown` by default, or `html` with `--html` / `--format html`).
  Official limits are checked before the request: 32 768 characters, 500 blocks, 20 table
  columns, 50 media, 16 levels of nesting. aiogram 3.33 already has these methods — no raw
  token-in-URL calls.
- New subcommand `tg-send rich` (Markdown on stdin or `--html`). Same `--chat` / `--thread` /
  `--reply-to` / allowlist rules as `text`. `--no-fallback` refuses to use `sendMessage`.
- Streaming uses `sendRichMessageDraft` (with the official `<tg-thinking>` placeholder), and
  falls back to `sendMessageDraft` if the API rejects the draft.
### Changed
- `tg-send text` (format `md` or `html`) now sends a Rich Message first. If the API rejects
  it or the payload is over a rich limit, it falls back to the previous `sendMessage` path
  (including 4 096-character splitting). `--classic` and `--format plain` keep the old path
  only. Replies are still stored in Serverless history as the original markdown/plain text.
### Notes
- Markdown is sent to Telegram as-is (official Rich Markdown includes GFM tables, headings,
  lists, and mixed-in HTML such as `<details>`). No third-party converter was vendored.

## 4.0.0 — 2026-11-22
### Changed
- **Intake moved to Telegram Serverless.** `serverless/tgcloud/handlers/message.js` runs on
  Telegram's platform (webhook managed by Telegram): same allowlist (private chat,
  `chat.id == from.id`, owner + `TG_ALLOWED_USER_IDS`), `/start /ping /status /reset /new`,
  👀 + typing, same webhook payload and `Authorization: Bearer` header. The box no longer has to be
  awake to accept a message; the long-polling relay and supervisord are stopped.
- **Memory** lives in the Serverless database (`messages`, same item format, per chat and topic,
  30 days). `tg-send` queues replies locally and a detached `tg-cloud flush` stores them;
  `tg-history` reads from Serverless (`--local` for the old SQLite).
- **Attachments** are passed as `file_id`/`file_unique_id`/`file_name`/`mime_type`/… with
  `path: null`; the event carries `fetch_attachments_cmd` and `how_to_get_attachments`.
- `tg-health` checks Serverless (settings, key fingerprint, dead letters, `tgcloud webhook`,
  deployed code = local, `getWebhookInfo`). `tg-redeliver` works on Serverless dead letters.
### Added
- `bin/tg-fetch-attachments`, `bin/tg-cloud` (setup/status/deploy/flush/import-history/webhook-sync),
  `bin/tg-mode` (serverless ⇄ polling fallback).
- Outbox table `deliveries` (row written before the first POST), 4 attempts with 1/3/6 s backoff,
  dead letters + sender notice, stale-pending sweep, `update_id` dedupe, album merge,
  `stopped_message_generation` handler.
- New payload fields: `intake: "serverless"`, `fetch_attachments_cmd`, `how_to_get_attachments`.
- Tests: `node --test tests/serverless/*.test.mjs` (12) and `tests/test_v4.py` (11); platform runs
  via `tgcloud run` with a `ctx.test` sandbox.
### Unchanged / kept
- `tg-send` CLI, `history` format, `how_to_reply`. The v3 relay remains as a fallback
  (`bin/tg-mode polling`); `tg-ensure` and the relay refuse to start in serverless mode.
- Backup of v3: `/workspace/tg-bridge.bak-v3`.

## 3.0.0 — 2026-11-22
### Added
- **Conversation memory.** Every allowed inbound message and every `tg-send` reply is stored in
  SQLite (`history.sqlite3`, 0600) per chat **and** per topic (owner and guests separate).
  The webhook gets a new top-level `history` (+ `history_note`): the last `TG_HISTORY_MESSAGES`
  (20) earlier messages of that chat+topic, oldest first, within `TG_HISTORY_MAX_CHARS` (8000).
  Item: `{role: user|assistant, text, date (ISO-8601 UTC), ts, message_id?, attachments?, forwarded?}`.
- `/reset` and `/new` clear one chat/topic's history. 30-day retention (`TG_HISTORY_DAYS`).
- `bin/tg-history --chat ID [--thread ID] [--limit N] [--json]`.
- VM-freeze detection: on resume the relay drops dead TCP connections and polls at once.
- `faulthandler`: stacks of all threads are logged before a watchdog exit and on `kill -USR1`.
### Fixed
- **False watchdog kills** ("heartbeat stale for 195s"): the box VM is frozen while the agent is idle;
  the wall clock jumps on resume but the monotonic clock does not. Both watchdogs now use the monotonic
  clock; the heartbeat file holds `<pid> <monotonic>`.
- Long poll shortened 50 s → 30 s; a 409 after resume is retried after 3 s instead of 30 s;
  the supervisord listener no longer kills the same pid twice.
### Unchanged
- Webhook contract (`type`, `message.*`, `how_to_reply`, …), `tg-send` CLI, allowlist, `/start /ping /status`.

## 2.0.0 — 2026-11-22
- Rewrite on aiogram 3, aiohttp, tenacity and supervisord; no-loss offset, dead letters, explicit
  key source (`ROUTINE_KEY_ENV`), hang watchdogs, hash-pinned `requirements.txt`.

## 1.x
- Stdlib-only prototype (urllib long polling, bash supervisor).

