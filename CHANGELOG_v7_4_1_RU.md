# v7.4.1 Bothost-safe

- Docker-код перенесён из `/app` в `/usr/src/xzona`; `/app/data` оставлен только для persistent SQLite.
- Зафиксирован рекомендуемый внутренний порт 8080; приложение по-прежнему использует `PORT` от Bothost.
- Добавлен ранний `[XZONA BOOT]` лог до импорта основных модулей.
- Добавлен аварийный `/health`, если приложение упало уже на runtime-старте.
- Токен читается из `BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TOKEN` или `API_TOKEN`.
- Добавлена пошаговая диагностика: config -> SQLite -> Telethon -> web -> Telegram getMe -> polling.
- Telegram `getMe` и удаление webhook имеют несколько попыток перед фатальной остановкой.
