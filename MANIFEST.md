# Содержимое пакета telegram-grok-wake

Набор файлов, чтобы человек со своим Grok Bot повторил связку **Telegram → Telegram Serverless → рутина Grok Bot**.

Основной путь — папка `serverless/`. Секреты (токены, ключи, свои URL и id) в пакет не входят.

- Гайд: [SKILL.md](SKILL.md)
- Кратко о схеме: [README.md](README.md)
- Текст инструкции рутины: [ROUTINE_PROMPT.md](ROUTINE_PROMPT.md)

---

## Корень

| Файл | Зачем |
|---|---|
| `MANIFEST.md` | Этот список. |
| `README.md` | Схема, параметры настройки, безопасность и ограничения. |
| `SKILL.md` | Пошаговый гайд только по Telegram Serverless. |
| `ROUTINE_PROMPT.md` | Пример Instruction для webhook-рутины. |
| `.gitignore` | Игнорирует служебные папки с секретами и зависимостями. |
| `banner.png`, `grok-bot-appicon.png`, `telegram-logo.png` | Оформление, по желанию. |

---

## Папка `serverless/` — основной путь

Код v4 на Telegram Serverless. Секреты и свои user id в исходниках не зашиты: их записывают в базу проекта (`owner_id`, `allowed_ids`, `routine_url`, `routine_key`).

| Файл | Зачем |
|---|---|
| `tgcloud/handlers/message.js` | Вход: входящее сообщение. |
| `tgcloud/handlers/stopped_message_generation.js` | Вход: нажатие «Стоп» на черновике. |
| `tgcloud/lib/bridge.js` | Мост: проверка, дедуп, заглушка «думаю…», сбор события (в т.ч. `reply.token`), вызов рутины. |
| `tgcloud/lib/core.js` | Allowlist, сборка JSON, нарезка длинного текста. |
| `tgcloud/lib/store.js` | Конфиг, история, `seen` (дедуп `update_id`), защита токена ответа (атомарный захват, возврат при ошибке отправки). |
| `tgcloud/lib/reply_token.js` | Подпись и проверка токена ответа. |
| `tgcloud/lib/pause.js` | Пауза между повторными попытками доставки. |
| `tgcloud/endpoints/box.js` | Канал с компьютера (`npx tgcloud run`): настройки, диагностика и `op: send` (первый ответ правит заглушку «думаю…»). |
| `tgcloud/schema.js` | Таблицы базы, включая `seen`. |
| `tgcloud.jsonc`, `package.json`, `package-lock.json` | Конфиг и CLI `@tgcloud/cli`. |
| `AGENTS.md`, `docs/tgcloud-sdk.md` | Справка для агента и SDK. |
| `.gitignore` | Игнорирует `.tgcloud/` и `node_modules/`. |

---

## С чего начать

1. Прочитай `SKILL.md`.
2. Создай бота у @BotFather, включи Serverless, выложи `serverless/`.
3. Создай webhook-рутину в Grok Bot, запиши URL и ключ через `config_set`.
4. Никакие токены в файлы пакета насовсем не вписывай.
