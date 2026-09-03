# XZONA Group Bot v7.6 — запуск на Bothost.ru

## 1. Проект

- платформа: Telegram / Python;
- главный файл: `main.py`;
- при использовании приложенного Dockerfile включите собственный Dockerfile;
- домен включён;
- внутренний порт: `8080` (или порт, который Bothost передаёт в `PORT`).

После обновления файлов выполняйте **Redeploy**.

## 2. Переменные окружения

```env
OWNER_ID=ВАШ_TELEGRAM_ID
ADMIN_IDS=ВАШ_TELEGRAM_ID
DB_PATH=/app/data/bot.db
TELETHON_WEB_HOST=0.0.0.0
ANNOUNCE_ON_START=1
TEMP_MESSAGE_TTL=90
TELETHON_MEMBER_SYNC_INTERVAL=3600
```

Токен должен быть задан штатным полем Bothost или как `BOT_TOKEN`.

После выдачи HTTPS-домена:

```env
TELETHON_WEB_PUBLIC_URL=https://ВАШ-ДОМЕН
TELETHON_WEB_TICKET_TTL=900
```

`TELETHON_MEMBER_SYNC_INTERVAL=3600` означает синхронизацию состава раз в час. `0` отключает фоновую синхронизацию, ручная кнопка продолжает работать.

## 3. Первый запуск v7.6

1. Сохраните старый `/app/data/bot.db` — удалять его не нужно.
2. Сделайте Redeploy.
3. В личке бота откройте `/admin`.
4. В `🔐 Telethon` авторизуйте аккаунт.
5. **Заново вручную подтвердите нужные разделы**: зайдите в каждую forum-тему и выполните соответствующую команду `/set_..._topic`.
6. Вернитесь в `/admin -> 🔐 Telethon` и нажмите `👥 Синхронизировать участников`.
7. Через `📋 Состав группы` проверьте, кого Telethon видит в группе и у кого ещё нет игрового ника.

Важно: v7.6 специально не использует старые автоматические привязки разделов, пока вы не назначите их вручную.

## 4. Команды ручной привязки

```text
/set_general_topic
/set_nicks_topic
/set_storage_topic
/set_market_topic
/set_delivery_topic
/set_gp_stock_topic
/set_events_topic
/set_diplomacy_topic
/set_targets_topic
/set_news_topic
/set_info_topic
/set_bar_topic
```

Если ошиблись разделом, выполните ту же команду в правильной теме. Старая панель бота будет удалена из прежнего раздела.

## 5. Проверка запуска

Откройте:

```text
https://ВАШ-ДОМЕН/health
```

В логах должна быть строка:

```text
[XZONA BOOT] Starting XZONA Group Bot v7.6 Bothost-safe
```

После подключения Telegram должна появиться строка `Telegram connected: @...`.
