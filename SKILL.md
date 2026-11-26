# Пошаговый гайд: Telegram → Telegram Serverless → Grok Bot (v4.6)

Ты (Grok Bot) проводишь человека по подключению его Telegram-бота к тебе с помощью полного пакета `tg-bridge` (v4.6): приём сообщений работает в **Telegram Serverless** (`serverless/`), а отправка ответов, вложений, Rich-таблиц, карточки `/usage` и погоды `/weather` — через локальные команды на боксе (`bin/tg-send` и др.).

Не вываливай все шаги сразу: коротко обозначь план, дождись согласия и веди по одному шагу.

---

## Что получится

1. Человек пишет боту в Telegram — платформа запускает `serverless/tgcloud/handlers/message.js`.
2. Мост проверяет allowlist (только личка владельца и опциональных гостей), ставит «🗨️ Думаю...» + случайный стикер, склеивает альбомы и пересылки с комментарием и отправляет событие POST-запросом на webhook рутины с заголовком `Authorization: Bearer <ключ>`.
3. Рутина отвечает командой `bin/tg-send` на боксе (классический текст, Rich Message с таблицами, стрим, фото или файл); стикер удаляется, а ответ сохраняется в памяти разговора в Serverless.

---

## Шаг 1. Создать отдельного бота у @BotFather

> **Заведи нового, отдельного бота.** Не подключай бота с важными подписчиками или базами: на боксе хранятся токен бота и CLI-токен Serverless.

1. Открой официального [@BotFather](https://t.me/BotFather) и отправь `/newbot`.
2. Задай имя и username (оканчивается на `bot`).
3. Сохрани **токен бота** (`TG_BOT_TOKEN`) как пароль — никому его не отправляй и не коммить в репозиторий.
4. Узнай свой числовой Telegram ID (например, через [@userinfobot](https://t.me/userinfobot)) — это `TG_OWNER_ID`.

---

## Шаг 2. Включить Telegram Serverless и задеплоить `serverless/`

1. В @BotFather открой своего бота → **Serverless** → включи функцию.
2. Там же открой **Serverless → CLI Access** и скопируй **CLI access token** (это отдельный токен `TGCLOUD_TOKEN`, не `TG_BOT_TOKEN`).
3. На машине с Node.js 18+ выполни:
   ```bash
   cd serverless
   npm ci
   npx tgcloud login
   npx tgcloud push
   ```
4. Webhook платформа поставит сама — вручную `setWebhook` вызывать не нужно.

---

## Шаг 3. Создать webhook-рутину в Grok Bot

1. Создай рутину с **webhook-триггером** и вставь инструкцию из [ROUTINE_PROMPT.md](ROUTINE_PROMPT.md).
2. Открой карточку рутины (на компьютере: имя бота вверху чата / `Cmd+Shift+I` → вкладка Details → Routines → блок **Webhook**) и скопируй:
   * **POST to** → это `ROUTINE_URL`;
   * **key** → это секрет рутины (`TGGROK_ROUTINE_KEY`).

---

## Шаг 4. Установить Python-часть на боксе и связать с Serverless

1. Установи зависимости в `.venv`:
   ```bash
   bin/tg-install
   ```
2. Создай конфиг `~/.config/tg-bridge/env` на основе `env.example`:
   * укажи `TG_OWNER_ID`, `ROUTINE_URL` (только `https://`), `ROUTINE_KEY_ENV=TGGROK_ROUTINE_KEY`;
   * `TG_BOT_TOKEN`, `TGGROK_ROUTINE_KEY` и `TGCLOUD_TOKEN` лучше держать в переменных окружения / секретах бокса (не в git).
   * При необходимости настрой `TG_STICKER_PACK` (по умолчанию `VikostVSpack`, `off` отключает).
3. Передай настройки в Serverless и зарегистрируй меню команд (`/new`, `/skills`, `/usage`, `/weather`):
   ```bash
   bin/tg-cloud setup
   bin/tg-cloud commands
   bin/tg-health
   ```
   Убедись, что `bin/tg-health` показывает режим `serverless`, синхронизированный webhook и совпадающий отпечаток ключа.

---

## Шаг 5. Опционально: карточка `/usage` и погода `/weather`

* **Блок расходов Grok API в `/usage`:** получи Management-ключ в `console.x.ai` (Settings → Management Keys), положи в `XAI_MANAGEMENT_KEY`, а Team ID — в `XAI_TEAM_ID` или локальный файл `xai-team-id`. Подробности: [docs/USAGE_CARD.md](docs/USAGE_CARD.md).
* **Виджет погоды `/weather`:** положи ключ meteoblue в `METEOBLUE_API_KEY`. Город по умолчанию — Киев (`kyiv`), можно сменить через `TG_WEATHER_CITY` или флаги `--name --lat --lon`. Подробности: [docs/WEATHER.md](docs/WEATHER.md).

---

## Шаг 6. Проверка и частые проблемы

1. Напиши боту в личку «привет». В чате должны появиться «🗨️ Думаю...» и стикер, в Grok Bot — запуск рутины, затем ответ в Telegram (стикер исчезнет).
2. Проверь команды `/status`, `/usage`, `/weather` и `/new`.
3. Сообщения от чужих ID или из групп должны молча игнорироваться.

Если что-то не работает:
* **Бот молчит:** проверь `bin/tg-health` и `bin/tg-cloud status` — заданы ли `owner_id`, `routine_url` (`https://`) и `routine_key`, не на паузе ли рутина.
* **Сообщения не дошли:** посмотри `bin/tg-redeliver` (можно переотправить с `--send` или сбросить `--drop ID`).
* **Нужен временный откат без Serverless:** `bin/tg-mode polling` (вернуть обратно: `bin/tg-mode serverless`).

---

## Источники

* Боты Telegram: [core.telegram.org/bots](https://core.telegram.org/bots#botfather)
* Telegram Serverless: [core.telegram.org/bots/serverless](https://core.telegram.org/bots/serverless)
* Рутины Grok Bot: [docs.x.ai/grok-bot/skills-routines-and-automations](https://docs.x.ai/grok-bot/skills-routines-and-automations)
