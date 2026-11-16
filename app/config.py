import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
SESSIONS_DIR = DATA_DIR / "sessions"

for directory in (DATA_DIR, MEDIA_DIR, SESSIONS_DIR):
    directory.mkdir(parents=True, exist_ok=True)


FREEBIE_BADGE = "#халява"

DEFAULT_STYLE_PROMPT = (
    "Ты — шеф-редактор современного Telegram-канала про технологии, ИИ, разработку и полезные сервисы.\n"
    "Твоя задача — делать посты визуально привлекательными, структурными и легко читаемыми (в стиле топовых ТГ-медиа), "
    "сохраняя 100% фактов и полезных ссылок оригинала.\n"
    "Правила вёрстки и редактуры:\n"
    "1. ЗАГОЛОВОК: Первая строка поста ВСЕГДА должна быть выразительным жирным заголовком <b>...</b> с уместным "
    "тематическим эмодзи (или кастомным <tg-emoji emoji-id=\"...\"> из исходника).\n"
    "2. СТРУКТУРА И ТИПОГРАФИКА: Разбивай текст на короткие динамичные абзацы. Если в посте перечисляются факты, фичи, "
    "условия или шаги — оформляй их аккуратным списком через маркер «•» с жирными акцентами (например: • <b>Главное:</b> ...).\n"
    "3. ЦИТАТЫ И ПАНЧЛАЙНЫ: Ироничные ремарки, выводы, прямую речь или яркие мысли (даже в коротких постах из 2–3 строк) "
    "оформляй в красивый блок цитаты <blockquote>...</blockquote> (а длинные свёрнутые блоки — в <blockquote expandable>...</blockquote>).\n"
    "4. КОД И ТЕРМИНЫ: Названия нейросетей, версий, консольных команд или библиотек можно аккуратно выделять тегом <code>...</code>.\n"
    "5. КАСТОМНЫЕ ПРЕМИУМ-ЭМОДЗИ И ССЫЛКИ: ОБЯЗАТЕЛЬНО сохраняй все теги анимированных эмодзи <tg-emoji emoji-id=\"...\">...</tg-emoji> "
    "и все полезные внешние ссылки по теме поста через <a href=\"https://...\">текст</a>.\n"
    "6. ЧИСТОТА: Не лей пустую «воду» и удаляй ссылки на чужие Telegram-каналы (t.me/...), чужие @юзернеймы и призывы подписаться.\n"
    "7. Используй ТОЛЬКО HTML-теги Telegram (<b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>, <blockquote expandable>, "
    "<tg-spoiler>, <a href=\"...\">, <tg-emoji emoji-id=\"...\">), без Markdown."
)


@dataclass
class Config:
    # Telegram Userbot (Telethon) — с сайта https://my.telegram.org
    tg_api_id: int
    tg_api_hash: str
    tg_session_name: str

    # Telegram Bot (Aiogram) — от @BotFather
    bot_token: str
    admin_ids: list[int]
    target_channel: str | int  # @username канала или ID вида -1001234567890

    # Настройки ИИ (универсальные)
    ai_provider: str           # gemini | openai | openrouter | deepseek | groq | custom
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    ai_proxy: str = ""         # Опциональный HTTP/SOCKS прокси для запросов к ИИ
    tg_string_session: str = ""  # Опциональная строковая сессия Telethon-парсера (твинка) для облака
    tg_publisher_session_name: str = ""  # Сессия аккаунта с Premium (основы) только для публикации в канал
    tg_publisher_string_session: str = ""  # Опциональная строковая сессия Premium-публикатора для облака

    # Лимиты медиа (Bot API позволяет отправлять файлы до 50 МБ)
    max_media_size_mb: int = 45
    db_path: Path = field(default_factory=lambda: DATA_DIR / "autoposter.db")


def _parse_target_channel(raw: str) -> str | int:
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            ids.append(int(item))
    return ids


PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-3.5-flash-lite",
    ),
    "openai": (
        "https://api.openai.com/v1",
        "gpt-4o-mini",
    ),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "google/gemma-4-31b-it:free",
    ),
    "deepseek": (
        "https://api.deepseek.com/v1",
        "deepseek-chat",
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
}


def load_config() -> Config:
    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
    target_channel_raw = os.getenv("TARGET_CHANNEL", "").strip()

    missing = []
    if not api_id_raw or not api_id_raw.isdigit() or api_id_raw == "12345678":
        missing.append("TG_API_ID (укажите ваш реальный api_id с https://my.telegram.org)")
    if not api_hash or "your_api_hash" in api_hash:
        missing.append("TG_API_HASH (укажите ваш реальный api_hash с https://my.telegram.org)")
    if not bot_token or "your_bot_token" in bot_token:
        missing.append("BOT_TOKEN (укажите токен вашего бота от @BotFather)")
    if not admin_ids_raw or admin_ids_raw == "123456789":
        missing.append("ADMIN_IDS (укажите ваш реальный числовой Telegram ID от @userinfobot)")
    if not target_channel_raw or target_channel_raw == "@my_awesome_channel":
        missing.append("TARGET_CHANNEL (укажите @username вашего канала или его ID -100...)")

    ai_provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    ai_api_key = os.getenv("AI_API_KEY", "").strip()
    if not ai_api_key or "your_ai_api_key" in ai_api_key:
        missing.append("AI_API_KEY (укажите API-ключ выбранной нейросети, например с https://aistudio.google.com/apikey)")

    if missing:
        joined = "\n  - ".join(missing)
        raise ValueError(
            f"Не заполнены обязательные переменные в файле .env:\n  - {joined}\n"
            f"Скопируйте .env.example в .env и укажите ваши данные."
        )

    default_url, default_model = PROVIDER_DEFAULTS.get(
        ai_provider, ("https://api.openai.com/v1", "gpt-4o-mini")
    )
    ai_base_url = os.getenv("AI_BASE_URL", "").strip() or default_url
    ai_model = os.getenv("AI_MODEL", "").strip() or default_model

    max_media_raw = os.getenv("MAX_MEDIA_SIZE_MB", "45").strip()
    max_media_mb = int(max_media_raw) if max_media_raw.isdigit() else 45

    return Config(
        tg_api_id=int(api_id_raw),
        tg_api_hash=api_hash,
        tg_session_name=str(SESSIONS_DIR / os.getenv("TG_SESSION_NAME", "userbot_reader")),
        tg_string_session=os.getenv("TG_STRING_SESSION", "").strip(),
        tg_publisher_session_name=str(
            SESSIONS_DIR / os.getenv("TG_PUBLISHER_SESSION_NAME", "userbot_publisher")
        ),
        tg_publisher_string_session=os.getenv("TG_PUBLISHER_STRING_SESSION", "").strip(),
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(admin_ids_raw),
        target_channel=_parse_target_channel(target_channel_raw),
        ai_provider=ai_provider,
        ai_api_key=ai_api_key,
        ai_model=ai_model,
        ai_base_url=ai_base_url.rstrip("/"),
        ai_proxy=os.getenv("AI_PROXY", "").strip(),
        max_media_size_mb=max_media_mb,
    )
