# Контекст проекта: Telegram AI Autoposter (Handoff для нового чата)

## 1. Общее описание и статус
Система автоматизированного ведения Telegram-канала с ИИ-редактором и премодерацией («Предложкой»).
* **Статус:** Запущен и работает в продакшене (`LIVE`) на **Render (Free Tier, регион Oregon, USA)**.
* **Локальная папка проекта:** `C:\Users\User\.gemini\antigravity\scratch\tg_ai_autoposter`
* **Локальное окружение Python:** `.venv` (`Python 3.12`)
* **Локальные секреты:** хранятся в `C:\Users\User\.gemini\antigravity\scratch\tg_ai_autoposter\.env` (не в Git).
* **GitHub репозиторий (приватный):** `https://github.com/forskybel-max/tg-ai-autoposter` (ветка `main`, настроен `autoDeploy` на Render).
* **Render Dashboard:** `https://dashboard.render.com/web/srv-dbnfj9jm8hqs73c32mqg`
  * Service ID: `srv-dbnfj9jm8hqs73c32mqg`
  * Workspace / Owner ID: `tea-dbnffunf3r2c738gr8ug`
  * Public Healthcheck URL: `https://tg-ai-autoposter.onrender.com/health`
* **Бот управления и предложки:** `@neuro_ai_admin_bot` («НейроАдмин»)
* **Целевой канал публикации:** `-1004395920076` (задан в `TARGET_CHANNEL`).

---

## 2. Стек и архитектура
Единый асинхронный процесс (`asyncio` в `main.py`), объединяющий:
1. **`Telethon` (Userbot):** читает открытые (`@username`) и закрытые (`https://t.me/+...`) каналы-доноры в реальном времени, склеивает медиа-альбомы (`events.Album`), извлекает полное HTML-оформление оригинала (`html.unparse`: скрытые гиперссылки, цитаты `<blockquote>` / `<blockquote expandable>`, код `<pre>`, спойлеры `<tg-spoiler>`), скачивает фото и видео (вместе с `width`, `height`, `duration` и `thumb`).
2. **`AIService` (`aiohttp`):** отправляет посты в **Google Gemini API** (рабочая быстрая модель `gemini-3.1-flash-lite`, есть автопоиск свободных Flash-моделей при `503` и поддержка `OpenRouter`/`OpenAI`/`DeepSeek`). Фильтрует рекламу и смысловые дубли, делает **бережный рерайт** без лишней «воды» с сохранением цитат и полезного форматирования, сохраняет внешние ссылки (`<a href="https://...">`), вырезает ссылки на чужие ТГ-каналы (`t.me/...`) и чужие `@юзернеймы`, а также автоматически распознаёт бесплатные раздачи и ставит рубрику `🎁 #халява`.
3. **`Aiogram 3` (Бот-админка и Предложка):** присылает готовый пост админу в ЛС с кнопками:
   * `✅ Опубликовать в канал` / `🔄 Переписать (ИИ)` / `✏️ Свой текст` / `🎁 + #халява` (`🎁 Халява: ВКЛ`) / `👁 Исходник` / `🖼 Убрать/Вернуть медиа` / `❌ Отклонить`.
   * Админ-меню (`/start`): добавление/удаление каналов-доноров, тестовый парсинг последнего поста (`📥 Тест`), смена промпта стиля, подписи канала, переключение режима «Предложка / Автопилот», смена ИИ-ключа, экспорт `TG_STRING_SESSION`.
4. **`SQLite` (`aiosqlite`):** хранит источники (`sources`), посты (`posts`) и настройки (`settings`).

---

## 3. Структура файлов
* `main.py` — точка входа: инициализация БД, запуск `aiohttp` Healthcheck-сервера (`PORT`), фоновой очистки старых медиа (`>72ч`) + пинга `RENDER_EXTERNAL_URL`, старт `UserbotParser` и `Aiogram` polling.
* `app/config.py` — загрузка `.env`, дефолтный промпт `DEFAULT_STYLE_PROMPT`, пресеты провайдеров ИИ.
* `app/database.py` — SQLite-слой + автовосстановление списка каналов из `INITIAL_SOURCES_JSON` при рестарте контейнера.
* `app/ai_service.py` — нативный REST-клиент Gemini (`aiohttp`) с автофоллбэком моделей, OpenAI-совместимый клиент и HTML-санитайзер (`_sanitize_html_and_links`).
* `app/userbot.py` — клиент `Telethon` (поддержка файловой сессии и `StringSession`), вступление в публичные/приватные каналы, авторизация из ТГ-бота, скачивание медиа.
* `app/publisher.py` — отправка превью в предложку и публикация в канал (одиночные посты, медиа-группы/альбомы, разбивка длинных подписей `>1024` симв., стриминг видео `supports_streaming=True`).
* `app/bot_handlers.py` — все хендлеры и FSM-состояния админ-бота `Aiogram 3`.
* `Dockerfile`, `.dockerignore`, `render.yaml`, `docker-compose.yml`, `start.bat` — деплой и локальный запуск.

---

## 4. Важные нюансы (не ломать при доработках!)
1. **Не запускать локально одновременно с Render:** пока сервис активен на Render, запуск локального `main.py` / `start.bat` вызовет `TelegramConflictError` у бота и конфликт одной и той же MTProto-сессии.
2. **Эфемерный диск на Render Free Tier:**
   * Сессия Userbot передана через переменную окружения `TG_STRING_SESSION` на Render.
   * Базовый список из 7 каналов-доноров сохранен в переменной окружения `INITIAL_SOURCES_JSON` на Render (при пересоздании контейнера `database.py` автоматически восстанавливает их в SQLite). Если добавляются новые постоянные каналы надолго — стоит обновлять `INITIAL_SOURCES_JSON` или подключить внешнюю БД / постоянный диск.
3. **Региональные ограничения с ПК пользователя (РФ):** локально без VPN Google Gemini API отдает `400 User location is not supported`, а на сервере Render (США, Oregon) работает напрямую без прокси.
4. **Деплой изменений:** достаточно закоммитить изменения в `C:\Users\User\.gemini\antigravity\scratch\tg_ai_autoposter` и сделать `git push origin main` — Render автоматически пересоберет Docker-контейнер за ~1 минуту.
