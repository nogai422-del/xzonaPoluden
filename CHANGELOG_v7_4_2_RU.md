# v7.4.2 Bothost hotfix

- Исправлен `NameError: ADMIN_CHAT_TYPES is not defined`, из-за которого модуль `multitask_handlers` падал при импорте и Telegram polling не запускался.
- Добавлен локальный `ADMIN_CHAT_TYPES = GROUP_TYPES | {"private"}` для обработчиков полной админ-панели.
- Версия объявления поднята до v7.4.2.
- Старый `/app/data/bot.db` совместим и должен быть сохранён.
