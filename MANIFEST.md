# Содержимое пакета telegram-grok-wake (tg-bridge v4.6)

Полный комплект для связки **Telegram → Telegram Serverless → рутина Grok Bot → Telegram**. Секреты, токены и личные ID в пакет не входят.

## Корень

| Файл | Зачем |
| :--- | :--- |
| `README.md` | Обзор, схема работы, быстрый старт, команды и безопасность. |
| `SKILL.md` | Пошаговый интерактивный гайд по установке для Grok Bot. |
| `MANIFEST.md` | Карта файлов пакета. |
| `ROUTINE_PROMPT.md` | Готовая инструкция (Instruction) для webhook-рутины Grok Bot. |
| `CHANGELOG.md` | История изменений по версиям. |
| `env.example`, `xai-team-id.example` | Шаблоны конфигурации без секретов. |
| `requirements.in`, `requirements.txt`, `supervisord.conf` | Python-зависимости и конфиг для резервного режима polling. |
| `banner.png`, `grok-bot-appicon.png`, `telegram-logo.png` | Графика для оформления репозитория. |
| `LICENSE`, `NOTICE` | Лицензия и уведомления. |

## `serverless/` — приём сообщений на Telegram Serverless

| Файл | Зачем |
| :--- | :--- |
| `tgcloud/handlers/message.js` | Вход: входящие сообщения, команды, альбомы, burst-пересылки. |
| `tgcloud/handlers/callback_query.js` | Вход: кнопка **🔄 Обновить** на карточке `/usage`. |
| `tgcloud/handlers/stopped_message_generation.js` | Вход: нажатие «Стоп» при потоковом ответе. |
| `tgcloud/endpoints/box.js` | Служебный канал с бокса (`tgcloud run`): настройки, меню, плейсхолдеры, история, карточка `/usage`. |
| `tgcloud/lib/core.js`, `bridge.js`, `store.js`, `pause.js`, `usage_card.js` | Чистая логика, оркестрация, БД, паузы без таймеров и рендер карточки `/usage`. |
| `tgcloud/schema.js` | Схема БД Serverless (`messages`, `deliveries`, `kv`, `seen`, `topics`, `album_parts`, `burst_parts`, `stops`, `placeholders`). |
| `tgcloud.jsonc`, `package.json`, `AGENTS.md`, `docs/tgcloud-sdk.md` | Конфигурация `@tgcloud/cli` и справка по SDK. |

## `bin/` и `tgbridge/` — локальная часть на боксе (Python)

| Путь | Зачем |
| :--- | :--- |
| `bin/tg-send`, `tgbridge/send.py`, `rich.py`, `textfmt.py` | Отправка ответов (классика, Rich, стрим, фото, файлы) и снятие плейсхолдера/стикера. |
| `bin/tg-fetch-attachments`, `tgbridge/fetch.py` | Скачивание вложений по `file_id`. |
| `bin/tg-cloud`, `tgbridge/cloud.py`, `cloudcli.py` | Настройка, деплой, синхронизация меню команд и сброс очереди памяти в Serverless. |
| `bin/tg-health`, `tg-redeliver`, `tg-history`, `tg-mode`, `tg-install` | Диагностика, повторная доставка, просмотр истории, переключение режима и установка `.venv`. |
| `bin/tg-weather-widget`, `tgbridge/widgets/weather.py`, `weather.html` | Виджет погоды (meteoblue, Bot API 10.4 + PNG-превью). |
| `bin/xai-usage` | Получение расходов Grok API из xAI Management API для `/usage`. |
| `tgbridge/tz.py`, `config.py`, `log.py`, `placeholder.py`, `relay.py`, `state.py` | Общие утилиты (таймзона, конфиг, редактура секретов, кэш плейсхолдеров, резервный polling). |

## `docs/` и `tests/`

| Путь | Зачем |
| :--- | :--- |
| `docs/USAGE_CARD.md` | Настройка Management-ключа xAI и формат карточки `/usage`. |
| `docs/WEATHER.md` | Настройка meteoblue, города и фолбэка для `/weather`. |
| `tests/` | Автотесты Python (`test_*.py`), Serverless (`tests/serverless/*.test.mjs`) и офлайн-фикстуры (`tests/fixtures/`). |
