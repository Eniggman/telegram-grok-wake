<p align="center">
  <img src="banner.png" alt="Grok Bot и Telegram" width="100%">
</p>

# telegram-grok-wake

Личный мост **Telegram → Telegram Serverless → хук Grok Bot**

Вы пишете своему Telegram-боту — сообщение через Telegram Serverless будит рутину Grok Bot, а ответ (текст, таблицы Rich, файлы, карточки лимитов или погода) возвращается обратно в тот же чат и топик. Компьютер Grok Bot при этом может спокойно засыпать между задачами: приём сообщений крутится на серверах Telegram.

## Зачем это и в чём загвоздка

* **Почему не polling:** если держать на компьютере скрипт, который постоянно опрашивает `getUpdates`, связь обрывается каждый раз, когда VM засыпает или перезапускается.
* **Почему нужен посредник:** Telegram шлёт webhook со своим секретом в `X-Telegram-Bot-Api-Secret-Token`, а webhook-рутина Grok Bot ждёт заголовок `Authorization: Bearer <ключ>`. Код в `serverless/` работает на инфраструктуре [Telegram Serverless](https://core.telegram.org/bots/serverless): принимает обновления, проверяет список допуска и будит рутину с нужным заголовком.

## Как это работает

```
Telegram
   │  обновление (webhook ведёт сама платформа Serverless)
   ▼
Telegram Serverless (папка serverless/)
   │  1. только личка (chat.id == from.id) и id из allowlist
   │  2. дедупликация update_id, лимиты, склейка альбомов и пересылок с комментарием
   │  3. заглушка «🗨️ Думаю...» + случайный стикер, запись в память (7 дней)
   │  4. POST события на webhook рутины (Authorization: Bearer <ключ>, до 4 попыток)
   ▼
Рутина Grok Bot (на боксе)
   │  думает, при необходимости скачивает вложения через bin/tg-fetch-attachments
   │  и отвечает через bin/tg-send (классика / Rich / стрим / фото / файлы)
   ▼
Тот же чат и топик в Telegram (стикер удаляется, ответ записывается в память)
```

## Что умеет

* **Приём на Telegram Serverless:** работает даже когда бокс спит; повторы при сбоях (до 4 попыток), очередь недоставленных (`bin/tg-redeliver`), резервный режим `bin/tg-mode polling` на случай отката.
* **Плейсхолдер «🗨️ Думаю...» и стикер:** сразу после сообщения бот ставит заглушку и шлёт случайный стикер из заданного набора (`TG_STICKER_PACK`, по умолчанию `VikostVSpack`, `off` выключает). При первом ответе стикер удаляется, а статус «печатает…» появляется только перед реальной отправкой ответа.
* **Rich Messages (Bot API 10.1–10.4):** короткие ответы идут обычным `sendMessage`, а отчёты с заголовками, GFM-таблицами и `<details>` автоматически отправляются как Rich Message (до 32 768 символов) с автофолбэком на классику.
* **Меню команд (`bin/tg-cloud commands`):**
  * `/new` (или `/reset`) — очистить историю текущего чата/топика (для всех допущенных).
  * `/skills` (только владелец) — создать/обновить скилл из последнего разговора (`skill_source`).
  * `/usage` (только владелец) — карточка лимитов подписки Grok с кнопкой **🔄 Обновить** и опциональным блоком расходов Grok API (10-дневный график в `$` и таблица токенов за 3 дня из `bin/xai-usage`). Подробнее: [docs/USAGE_CARD.md](docs/USAGE_CARD.md).
  * `/weather` (только владелец) — прогноз на неделю из meteoblue (по умолчанию Киев; задаётся через `TG_WEATHER_CITY` или флаги `--city`/`--name --lat --lon`) на базе Bot API 10.4 `sendWidget` с автоматическим фолбэком в PNG-превью, пока Telegram ограничивает виджеты. Подробнее: [docs/WEATHER.md](docs/WEATHER.md).
* **Память разговора и склейка пересылок:** отдельная история на каждый чат и топик (хранится 7 дней, в событие попадают последние 20 сообщений до 12 000 символов, длинные > 4 000 режутся с сохранением начала и хвоста). Пересланный пост с комментарием в окне ~1,5 с склеивается в одно пробуждение рутины, а в историю пишется раздельно.

## Быстрый старт

Полный интерактивный гайд для пошаговой настройки вместе с Grok Bot — в [SKILL.md](SKILL.md). Кратко:

1. **Создайте отдельного бота** у [@BotFather](https://t.me/BotFather) (`/newbot`), сохраните `TG_BOT_TOKEN` и включите **Serverless** в настройках бота (там же возьмите **CLI Access token** → `TGCLOUD_TOKEN`).
2. **Задеплойте Serverless-часть** (нужен Node.js 18+):
   ```bash
   cd serverless
   npm ci
   npx tgcloud login
   npx tgcloud push
   ```
3. **Создайте webhook-рутину в Grok Bot**, вставьте инструкцию из [ROUTINE_PROMPT.md](ROUTINE_PROMPT.md) и скопируйте её **POST to** (`ROUTINE_URL`) и **key** (`TGGROK_ROUTINE_KEY`).
4. **Настройте Python-часть на боксе** (Python 3.11+):
   ```bash
   bin/tg-install
   cp env.example ~/.config/tg-bridge/env
   # заполните TG_BOT_TOKEN, TG_OWNER_ID, ROUTINE_URL (секреты можно держать в env процесса)
   ```
5. **Передайте настройки в Serverless и зарегистрируйте меню команд:**
   ```bash
   bin/tg-cloud setup
   bin/tg-cloud commands
   bin/tg-health
   ```
6. **Опционально:**
   * для блока расходов API в `/usage` задайте `XAI_MANAGEMENT_KEY` и `XAI_TEAM_ID` ([docs/USAGE_CARD.md](docs/USAGE_CARD.md));
   * для `/weather` задайте `METEOBLUE_API_KEY` и при желании `TG_WEATHER_CITY` ([docs/WEATHER.md](docs/WEATHER.md)).

## Основные команды на боксе

| Команда | Что делает |
| :--- | :--- |
| `bin/tg-health` | Полная диагностика: режим, сверка ключей, вебхук, код в облаке и недоставленные |
| `bin/tg-send text` / `rich` / `stream` / `photo` / `file` | Отправка ответов из рутины в Telegram |
| `bin/tg-fetch-attachments --chat ID FILE_ID…` | Скачать вложения по `file_id` (до 20 МБ) |
| `bin/tg-history --chat ID [--thread ID] [--limit N]` | Прочитать историю из Serverless (по умолчанию 30 сообщений) |
| `bin/tg-cloud setup` / `status` / `deploy` / `commands` | Управление настройками, деплоем и меню команд в Serverless |
| `bin/tg-redeliver [--send] [--drop ID]` | Просмотр и повторная отправка недоставленных сообщений |
| `bin/tg-mode [serverless\|polling]` | Показать или переключить режим приёма |
| `bin/tg-weather-widget render\|send\|update` | Сборка, отправка и обновление виджета погоды |
| `bin/xai-usage` | Получение расходов Grok API из xAI Management API (JSON) |

## Безопасность

* Проходят только личные чаты (`chat.id == from.id`) из allowlist (`TG_OWNER_ID` + опционально `TG_ALLOWED_USER_IDS`). Если `TG_OWNER_ID` не задан — не проходит никто.
* Команды `/skills`, `/usage` и `/weather` доступны только владельцу; гостям бот отказывает локально в Serverless. Гостей от владельца в обычных сообщениях ограничивает инструкция рутины (`ROUTINE_PROMPT.md`), поэтому без необходимости гостей лучше не добавлять.
* Секреты (`TG_BOT_TOKEN`, `TGCLOUD_TOKEN`, `TGGROK_ROUTINE_KEY`, `XAI_MANAGEMENT_KEY`, `METEOBLUE_API_KEY`) никогда не коммитятся в репозиторий и вырезаются из логов/ошибок.
* В Serverless ключ рутины хранится в таблице `kv`, недоступной снаружи (Mini App у бота нет, а `endpoints/box.js` отклоняет любые вызовы с `initData`). Заводите для моста отдельного бота.

## Тесты и структура

```bash
.venv/bin/python -m unittest discover -s tests -t .   # тесты бокса (Python)
node --test tests/serverless/*.test.mjs               # тесты Serverless (Node 22+ для burst.test.mjs)
```

Полная карта файлов пакета — в [MANIFEST.md](MANIFEST.md), история изменений — в [CHANGELOG.md](CHANGELOG.md).
