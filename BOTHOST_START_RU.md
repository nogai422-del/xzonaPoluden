# XZONA Group Bot v7.4.2 — точный запуск на Bothost.ru

Эта сборка специально сделана так, чтобы при проблеме не было «тишины»: первая строка runtime-лога начинается с `[XZONA BOOT]`, а при фатальной ошибке процесс оставляет диагностический `/health` с безопасным текстом ошибки.

## 1. Настройки проекта Bothost

В панели проекта выставьте:

- платформа: **Telegram / Python**;
- главный файл: **`main.py`**;
- **Использовать собственный Dockerfile: ВКЛ**;
- домен: **ВКЛ**;
- внутренний порт: **`8080`**.

Важно: документация Bothost требует, чтобы веб-сервис слушал `0.0.0.0`, порт брал из `PORT`, а внутренний порт в панели совпадал с реально слушаемым портом. Эта сборка делает именно так; значение по умолчанию — 8080.

После изменения Dockerfile/порта нужен именно **Deploy/Redeploy**, не простой Restart.

## 2. Переменные окружения

Минимум:

```env
OWNER_ID=ВАШ_ЧИСЛОВОЙ_TELEGRAM_ID
ADMIN_IDS=ВАШ_ЧИСЛОВОЙ_TELEGRAM_ID
DB_PATH=/app/data/bot.db
TELETHON_WEB_HOST=0.0.0.0
ANNOUNCE_ON_START=1
TEMP_MESSAGE_TTL=90
```

Токен обычно передаётся Bothost как `BOT_TOKEN`. Сборка также понимает `TELEGRAM_BOT_TOKEN`, `TOKEN` и `API_TOKEN`.

Если в панели есть отдельное поле Bot Token — заполните его. Если `/health` показывает, что токен не найден, добавьте вручную:

```env
BOT_TOKEN=1234567890:AA...
```

Для Telethon после появления домена:

```env
TELETHON_WEB_PUBLIC_URL=https://ВАШ-ДОМЕН
TELETHON_WEB_TICKET_TTL=900
```

Не задавайте `TELETHON_WEB_PORT`, если Bothost уже задаёт `PORT`.

## 3. Что должно появиться в логах

Сразу после запуска:

```text
[XZONA BOOT] Starting XZONA Group Bot v7.4.2 Bothost-safe
[XZONA BOOT] env: BOT_TOKEN=yes, OWNER_ID=yes, PORT=8080, DB_PATH=/app/data/bot.db
... SQLite ready
... Web/Telethon auth listening on 0.0.0.0:8080
... Telegram connected: @имя_бота (id=...)
```

Если последней строки `Telegram connected` нет — смотрите строку перед ней: теперь там будет точная причина.

## 4. Проверка через браузер

Откройте:

```text
https://ВАШ-ДОМЕН/health
```

Нормальный ответ:

```json
{"ok": true, "ready": true, "telethon_connected": false}
```

Если основной процесс упал уже после сборки, диагностический launcher постарается оставить `/health` доступным и вернёт примерно:

```json
{"ok": false, "ready": false, "startup_error_type": "...", "startup_error": "..."}
```

Секреты в этот endpoint не выводятся.

## 5. База данных

Bothost предназначает `/app/data` для постоянных данных. Оставьте:

```env
DB_PATH=/app/data/bot.db
```

Старый `bot.db` от v7.3/v7.4 удалять не нужно. Если он уже есть, поместите/оставьте его в `data`.

## 6. После первого успешного запуска

1. В Telegram отправьте боту `/myid` и проверьте, что ID совпадает с `OWNER_ID`.
2. Откройте `/admin`.
3. Авторизуйте Telethon через `🔐 Telethon`.
4. Выполните `/autoconfigure_topics`.
5. Если нужно повторно показать инструкции во всех темах — `/announce_all`.

## 7. Если всё равно не запускается

Нужны именно **runtime-логи после Build completed**, а не только лог сборки. Скопируйте первые строки от `[XZONA BOOT]` до traceback/ошибки — по ним можно точно определить проблему.
