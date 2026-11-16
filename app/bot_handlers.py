import html
import json
import logging
import re
from pathlib import Path

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.ai_service import AIService
from app.config import Config, DEFAULT_STYLE_PROMPT
from app.database import Database
from app.publisher import Publisher, build_moderation_keyboard
from app.userbot import PremiumPublisherClient, UserbotParser

logger = logging.getLogger(__name__)


class AdminStates(StatesGroup):
    waiting_for_login_phone = State()
    waiting_for_login_code = State()
    waiting_for_login_2fa = State()
    waiting_for_pub_phone = State()
    waiting_for_pub_code = State()
    waiting_for_pub_2fa = State()
    waiting_for_ai_key = State()
    waiting_for_source = State()
    waiting_for_style_prompt = State()
    waiting_for_signature = State()
    waiting_for_emoji_palette = State()
    waiting_for_manual_edit = State()
    waiting_for_rewrite_hint = State()


class AdminOnlyMiddleware(BaseMiddleware):
    """Пропускает команды и нажатия кнопок только от администраторов из ADMIN_IDS."""

    def __init__(self, admin_ids: list[int]):
        self.admin_ids = set(admin_ids)

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if not user or user.id not in self.admin_ids:
            if isinstance(event, Message):
                await event.answer(
                    f"⛔ Доступ запрещен. Ваш Telegram ID: <code>{user.id if user else 'unknown'}</code>",
                    parse_mode="HTML",
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            return None
        return await handler(event, data)


def setup_router(
    config: Config,
    db: Database,
    ai: AIService,
    publisher: Publisher,
    userbot: UserbotParser,
    premium_publisher: PremiumPublisherClient | None = None,
) -> Router:
    router = Router()
    router.message.middleware(AdminOnlyMiddleware(config.admin_ids))
    router.callback_query.middleware(AdminOnlyMiddleware(config.admin_ids))

    async def render_main_menu_text() -> tuple[str, InlineKeyboardMarkup]:
        sources = await db.get_sources()
        active_count = sum(1 for s in sources if s["enabled"])
        filter_ads = (await db.get_setting("filter_ads", "1")) == "1"
        auto_pub = (await db.get_setting("auto_publish", "0")) == "1"
        sig = await db.get_setting("channel_signature", "")
        palette = await db.get_setting("custom_emoji_palette", "")
        bullet_emj = await db.get_setting("emoji_list_bullet", "")
        head_emj = await db.get_setting("emoji_headline", "")
        palette_cnt = (
            AIService.count_custom_emojis(palette)
            + AIService.count_custom_emojis(bullet_emj)
            + AIService.count_custom_emojis(head_emj)
        )
        is_ub_auth = await userbot.is_authorized()
        ub_label = await userbot.get_account_label()
        pub_label = (
            await premium_publisher.get_account_label()
            if premium_publisher is not None
            else "⚪ Не подключен (публикует бот)"
        )
        is_pub_auth = (
            await premium_publisher.is_authorized()
            if premium_publisher is not None
            else False
        )

        mode_str = "⚡ Автопилот (сразу в канал)" if auto_pub else "🛡 Предложка (с одобрением)"
        ads_str = "✅ Включен" if filter_ads else "❌ Выключен"
        sig_str = "Установлена" if sig.strip() else "Нет"
        pal_str = f"{palette_cnt} шт." if palette_cnt > 0 else "Только из доноров"

        text = (
            "🤖 <b>Панель управления AI-Автопостером</b>\n\n"
            f"🕵️ <b>Парсер (Твинк):</b> {ub_label}\n"
            f"💎 <b>Публикатор (Premium-основа):</b> {pub_label}\n"
            f"✨ <b>Фирменные Premium-эмодзи:</b> {pal_str}\n"
            f"🎯 <b>Целевой канал:</b> <code>{config.target_channel}</code>\n"
            f"🧠 <b>Нейросеть:</b> <code>{config.ai_provider} / {config.ai_model}</code>\n"
            f"📡 <b>Каналов-доноров:</b> {active_count} активных (всего {len(sources)})\n"
            f"⚙️ <b>Режим публикации:</b> {mode_str}\n"
            f"🧹 <b>Фильтр рекламы и дублей:</b> {ads_str}\n"
            f"✍️ <b>Подпись канала:</b> {sig_str}"
        )
        if not is_ub_auth:
            text += (
                "\n\n⚠️ <b>Внимание:</b> нажмите кнопку <b>«🔑 Войти в Userbot»</b> ниже, "
                "чтобы подключить аккаунт для чтения каналов!"
            )

        rows: list[list[InlineKeyboardButton]] = []
        if not is_ub_auth:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🔑 Войти в Userbot (подключить парсер)",
                        callback_data="menu:ub_login",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Добавить канал-донор", callback_data="menu:add_src"
                    ),
                    InlineKeyboardButton(
                        text=f"📋 Источники ({len(sources)})",
                        callback_data="menu:list_src",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=(
                            "💎 Premium-основа (Подключена)"
                            if is_pub_auth
                            else "💎 Подключить Premium-основу"
                        ),
                        callback_data="menu:pub_login",
                    ),
                    InlineKeyboardButton(
                        text=f"✨ Премиум-эмодзи ({palette_cnt})",
                        callback_data="menu:emoji_palette",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🎨 Стиль ИИ (Промпт)", callback_data="menu:prompt"
                    ),
                    InlineKeyboardButton(
                        text="✍️ Подпись канала", callback_data="menu:signature"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"🧹 Анти-реклама: {'ВКЛ' if filter_ads else 'ВЫКЛ'}",
                        callback_data="menu:toggle_ads",
                    ),
                    InlineKeyboardButton(
                        text=f"⚙️ Режим: {'Авто' if auto_pub else 'Предложка'}",
                        callback_data="menu:toggle_mode",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🧠 Сменить ИИ / Ключ (OpenRouter без VPN)",
                        callback_data="menu:ai_key",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="☁️ Экспорт сессий (для деплоя в облако)",
                        callback_data="menu:export_session",
                    )
                ],
            ]
        )
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data == "menu:export_session")
    async def cb_export_session(call: CallbackQuery) -> None:
        ub_ok = await userbot.is_authorized()
        pub_ok = (
            await premium_publisher.is_authorized()
            if premium_publisher is not None
            else False
        )
        if not ub_ok and not pub_ok:
            await call.answer("Сначала войдите хотя бы в один аккаунт!", show_alert=True)
            return

        blocks: list[str] = []
        if ub_ok:
            session_str = userbot.export_string_session()
            blocks.append(
                "🕵️ <b>Сессия парсера (Твинк) — <code>TG_STRING_SESSION</code>:</b>\n"
                f"<code>{session_str}</code>"
            )
        if pub_ok and premium_publisher is not None:
            pub_str = premium_publisher.export_string_session()
            blocks.append(
                "💎 <b>Сессия публикатора (Основа с Premium) — <code>TG_PUBLISHER_STRING_SESSION</code>:</b>\n"
                f"<code>{pub_str}</code>"
            )

        await call.message.answer(
            "☁️ <b>Ваши строковые сессии для облачного хостинга (Render):</b>\n"
            "<i>(Никому их не передавайте! Укажите их в Environment Variables на Render, "
            "чтобы авторизация сохранялась при пересборке контейнера):</i>\n\n"
            + "\n\n".join(blocks),
            parse_mode="HTML",
        )
        await call.answer()

    # --- Быстрая смена ИИ-провайдера прямо из Telegram ---

    @router.callback_query(F.data == "menu:ai_key")
    async def cb_ai_key(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.waiting_for_ai_key)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")]
            ]
        )
        await call.message.edit_text(
            "🧠 <b>Быстрое подключение ИИ (работает напрямую из РФ без VPN!)</b>\n\n"
            "1. Перейдите на <a href='https://openrouter.ai/keys'>openrouter.ai/keys</a> (вход через Google/GitHub за 10 секунд).\n"
            "2. Нажмите <b>Create Key</b> и скопируйте бесплатный ключ (начинается на <code>sk-or-v1-...</code>).\n"
            "3. Просто отправьте этот ключ сюда в чат — я сам сохраню его в <code>.env</code> и сразу включу бесплатную модель без перезапуска!",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await call.answer()

    @router.message(AdminStates.waiting_for_ai_key)
    async def msg_save_ai_key(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        new_key = message.text.strip()
        try:
            await message.delete()
        except Exception:
            pass

        from dotenv import set_key
        from app.config import BASE_DIR, PROVIDER_DEFAULTS

        env_file = BASE_DIR / ".env"
        if new_key.startswith("sk-or-"):
            provider = "openrouter"
        elif new_key.startswith("AIza"):
            provider = "gemini"
        else:
            provider = "deepseek"

        base_url, default_model = PROVIDER_DEFAULTS[provider]
        config.ai_provider = provider
        config.ai_api_key = new_key
        config.ai_base_url = base_url
        config.ai_model = default_model

        if env_file.exists():
            set_key(str(env_file), "AI_PROVIDER", provider)
            set_key(str(env_file), "AI_API_KEY", new_key)
            set_key(str(env_file), "AI_MODEL", default_model)
            set_key(str(env_file), "AI_BASE_URL", "")

        await state.clear()
        wait = await message.answer(
            f"⏳ Проверяю связь с <b>{provider} ({default_model})</b>...",
            parse_mode="HTML",
        )
        try:
            res = await ai.process_post("Тестовая новость: ИИ успешно подключен и готов к работе!")
            await wait.delete()
            text, kb = await render_main_menu_text()
            await message.answer(
                f"✅ <b>ИИ успешно подключен без VPN!</b>\n"
                f"Тестовый рерайт:\n<blockquote>{res.rewritten_text}</blockquote>\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as exc:
            await wait.edit_text(
                f"⚠️ Ключ сохранен ({provider}), но при тесте возникла ошибка:\n"
                f"<code>{html.escape(str(exc)[:400])}</code>",
                parse_mode="HTML",
            )

    # --- Авторизация Userbot прямо через Telegram-бота ---

    @router.callback_query(F.data == "menu:ub_login")
    async def cb_ub_login(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.waiting_for_login_phone)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")]
            ]
        )
        await call.message.edit_text(
            "🔑 <b>Авторизация аккаунта-парсера (Userbot)</b>\n\n"
            "Отправьте номер телефона аккаунта Telegram, с которого скрипт будет читать каналы, "
            "в международном формате (например: <code>+79991234567</code>):",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.message(AdminStates.waiting_for_login_phone)
    async def msg_ub_phone(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        wait = await message.answer("⏳ Запрашиваю код входа в Telegram...")
        ok, info = await userbot.send_login_code(message.text)
        if not ok:
            await wait.edit_text(f"❌ {info}", parse_mode="HTML")
            return

        await state.set_state(AdminStates.waiting_for_login_code)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отмена", callback_data="menu:home")]
            ]
        )
        await wait.edit_text(
            f"📩 Код отправлен в приложение Telegram на номер <code>{info}</code>!\n\n"
            "⚠️ <b>ВАЖНО:</b> введите цифры кода <b>ЧЕРЕЗ ПРОБЕЛ</b> (например: <code>1 2 3 4 5</code>)!\n"
            "<i>Если переслать слитный код в чат, система безопасности Telegram автоматически аннулирует его.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.message(AdminStates.waiting_for_login_code)
    async def msg_ub_code(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        status, info = await userbot.submit_login_code(message.text)
        if status == "ok":
            await state.clear()
            text, kb = await render_main_menu_text()
            await message.answer(f"{info}\n\n{text}", parse_mode="HTML", reply_markup=kb)
        elif status == "need_2fa":
            await state.set_state(AdminStates.waiting_for_login_2fa)
            await message.answer(info, parse_mode="HTML")
        else:
            await message.answer(info, parse_mode="HTML")

    @router.message(AdminStates.waiting_for_login_2fa)
    async def msg_ub_2fa(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        pwd = message.text.strip()
        try:
            await message.delete()
        except Exception:
            pass
        ok, info = await userbot.submit_2fa_password(pwd)
        if ok:
            await state.clear()
            text, kb = await render_main_menu_text()
            await message.answer(f"{info}\n\n{text}", parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(info, parse_mode="HTML")

    # --- Авторизация Premium-публикатора (Основы) прямо через Telegram-бота ---

    @router.callback_query(F.data == "menu:pub_login")
    async def cb_pub_login(call: CallbackQuery, state: FSMContext) -> None:
        if premium_publisher is None:
            await call.answer("Модуль Premium-публикатора не инициализирован", show_alert=True)
            return
        await state.set_state(AdminStates.waiting_for_pub_phone)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")]
            ]
        )
        await call.message.edit_text(
            "💎 <b>Подключение Premium-публикатора (Ваша Основа)</b>\n\n"
            "🛡 <b>100% безопасно для основы:</b> этот аккаунт <b>НЕ</b> парсит каналы, не подписывается на доноров "
            "и не делает фоновых запросов. Он используется <b>только</b> в момент нажатия кнопки «✅ Опубликовать в канал», "
            "чтобы посты в вашем канале выходили с живыми платными анимированными эмодзи!\n\n"
            "📌 <b>Требование:</b> этот аккаунт должен иметь Telegram Premium и быть добавлен в администраторы вашего "
            f"целевого канала (<code>{config.target_channel}</code>) с правом «Публикация сообщений».\n\n"
            "Отправьте номер телефона аккаунта с Premium в международном формате (например: <code>+79991234567</code>):",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.message(AdminStates.waiting_for_pub_phone)
    async def msg_pub_phone(message: Message, state: FSMContext) -> None:
        if not message.text or premium_publisher is None:
            return
        wait = await message.answer("⏳ Запрашиваю код входа для Premium-публикатора...")
        ok, info = await premium_publisher.send_login_code(message.text)
        if not ok:
            await wait.edit_text(f"❌ {info}", parse_mode="HTML")
            return

        await state.set_state(AdminStates.waiting_for_pub_code)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Отмена", callback_data="menu:home")]
            ]
        )
        await wait.edit_text(
            f"📩 Код отправлен в приложение Telegram на номер <code>{info}</code>!\n\n"
            "⚠️ <b>ВАЖНО:</b> введите цифры кода <b>ЧЕРЕЗ ПРОБЕЛ</b> (например: <code>1 2 3 4 5</code>)!\n"
            "<i>Если переслать слитный код в чат, система безопасности Telegram автоматически аннулирует его.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.message(AdminStates.waiting_for_pub_code)
    async def msg_pub_code(message: Message, state: FSMContext) -> None:
        if not message.text or premium_publisher is None:
            return
        status, info = await premium_publisher.submit_login_code(message.text)
        if status == "ok":
            await state.clear()
            text, kb = await render_main_menu_text()
            await message.answer(f"{info}\n\n{text}", parse_mode="HTML", reply_markup=kb)
        elif status == "need_2fa":
            await state.set_state(AdminStates.waiting_for_pub_2fa)
            await message.answer(info, parse_mode="HTML")
        else:
            await message.answer(info, parse_mode="HTML")

    @router.message(AdminStates.waiting_for_pub_2fa)
    async def msg_pub_2fa(message: Message, state: FSMContext) -> None:
        if not message.text or premium_publisher is None:
            return
        pwd = message.text.strip()
        try:
            await message.delete()
        except Exception:
            pass
        ok, info = await premium_publisher.submit_2fa_password(pwd)
        if ok:
            await state.clear()
            text, kb = await render_main_menu_text()
            await message.answer(f"{info}\n\n{text}", parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(info, parse_mode="HTML")

    # --- Главное меню ---

    @router.message(Command("start", "admin", "menu"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, kb = await render_main_menu_text()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    @router.callback_query(F.data == "menu:home")
    async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @router.callback_query(F.data == "menu:toggle_ads")
    async def cb_toggle_ads(call: CallbackQuery) -> None:
        curr = await db.get_setting("filter_ads", "1")
        new_val = "0" if curr == "1" else "1"
        await db.set_setting("filter_ads", new_val)
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer("Настройка фильтра рекламы обновлена!")

    @router.callback_query(F.data == "menu:toggle_mode")
    async def cb_toggle_mode(call: CallbackQuery) -> None:
        curr = await db.get_setting("auto_publish", "0")
        new_val = "0" if curr == "1" else "1"
        await db.set_setting("auto_publish", new_val)
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer(
            "Включен полный автопилот!" if new_val == "1" else "Включен режим предложки!"
        )

    # --- Управление каналами-донорами ---

    @router.callback_query(F.data == "menu:add_src")
    async def cb_add_source(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.waiting_for_source)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")]
            ]
        )
        await call.message.edit_text(
            "➕ <b>Добавление канала-донора</b>\n\n"
            "Отправьте мне один или несколько каналов (каждый с новой строки) в любом формате:\n"
            "• <code>@durov</code>\n"
            "• <code>https://t.me/channel_name</code>\n"
            "• <code>https://t.me/+ПриватнаяИнвайтСсылка</code> (для закрытых каналов)",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.message(AdminStates.waiting_for_source)
    async def msg_add_source(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        lines = [line.strip() for line in message.text.splitlines() if line.strip()]
        status_msg = await message.answer("⏳ Подключаю канал(ы) через Userbot...")

        results: list[str] = []
        for item in lines:
            _, info = await userbot.join_and_register_channel(item)
            results.append(info)

        await state.clear()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 К списку источников", callback_data="menu:list_src"
                    ),
                    InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home"),
                ]
            ]
        )
        await status_msg.edit_text(
            "\n\n".join(results),
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "menu:list_src")
    async def cb_list_sources(call: CallbackQuery) -> None:
        sources = await db.get_sources()
        if not sources:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="➕ Добавить первый канал", callback_data="menu:add_src"
                        )
                    ],
                    [InlineKeyboardButton(text="🔙 В меню", callback_data="menu:home")],
                ]
            )
            await call.message.edit_text(
                "📋 Список каналов-доноров пока пуст.", reply_markup=kb
            )
            await call.answer()
            return

        rows = []
        for s in sources:
            status_icon = "🟢" if s["enabled"] else "⏸"
            lock_icon = "🔒" if s["is_private"] else "📢"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{status_icon} {lock_icon} {s['title']}",
                        callback_data=f"src:view:{s['chat_id']}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="➕ Добавить ещё", callback_data="menu:add_src"),
                InlineKeyboardButton(text="📤 Экспорт (JSON)", callback_data="src:export"),
            ]
        )
        rows.append([InlineKeyboardButton(text="🔙 В меню", callback_data="menu:home")])
        await call.message.edit_text(
            "📋 <b>Ваши каналы-доноры:</b>\n"
            "Нажмите на канал, чтобы протестировать парсинг последнего поста, поставить на паузу или удалить.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await call.answer()

    @router.callback_query(F.data == "src:export")
    async def cb_export_sources(call: CallbackQuery) -> None:
        sources = await db.get_sources()
        payload = [
            {
                "chat_id": int(s["chat_id"]),
                "title": s["title"],
                "username_or_link": s["username_or_link"],
                "is_private": int(s["is_private"]),
                "enabled": int(s["enabled"]),
            }
            for s in sources
        ]
        dumped = json.dumps(payload, ensure_ascii=False)
        await call.message.answer(
            "📤 <b>Текущий список каналов для <code>INITIAL_SOURCES_JSON</code> на Render:</b>\n"
            "<i>(Если добавили новый канал и хотите, чтобы он сохранялся при перезапусках бесплатного сервера Render — "
            "обновите переменную <code>INITIAL_SOURCES_JSON</code> в панели Render или отправьте её мне):</i>\n\n"
            f"<code>{html.escape(dumped)}</code>",
            parse_mode="HTML",
        )
        await call.answer()

    @router.callback_query(F.data.startswith("src:view:"))
    async def cb_view_source(call: CallbackQuery) -> None:
        chat_id = int(call.data.split(":")[2])
        s = await db.get_source(chat_id)
        if not s:
            await call.answer("Канал не найден", show_alert=True)
            return

        status_str = "🟢 Активен (мониторится)" if s["enabled"] else "⏸ На паузе"
        kind_str = "🔒 Закрытый канал" if s["is_private"] else "📢 Открытый канал"
        toggle_btn_text = "⏸ Поставить на паузу" if s["enabled"] else "▶️ Включить мониторинг"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📥 Тест: спарсить свежий пост",
                        callback_data=f"src:pull:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=toggle_btn_text,
                        callback_data=f"src:toggle:{chat_id}",
                    ),
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=f"src:del:{chat_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 К списку каналов", callback_data="menu:list_src"
                    )
                ],
            ]
        )
        await call.message.edit_text(
            f"📡 <b>{html.escape(s['title'])}</b>\n\n"
            f"• <b>Тип:</b> {kind_str}\n"
            f"• <b>ID:</b> <code>{s['chat_id']}</code>\n"
            f"• <b>Ссылка:</b> <code>{html.escape(s['username_or_link'])}</code>\n"
            f"• <b>Статус:</b> {status_str}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.callback_query(F.data.startswith("src:toggle:"))
    async def cb_toggle_source(call: CallbackQuery) -> None:
        chat_id = int(call.data.split(":")[2])
        await db.toggle_source(chat_id)
        await userbot.reload_sources()
        await cb_view_source(call)

    @router.callback_query(F.data.startswith("src:del:"))
    async def cb_delete_source(call: CallbackQuery) -> None:
        chat_id = int(call.data.split(":")[2])
        await db.delete_source(chat_id)
        await userbot.reload_sources()
        await call.answer("Канал удален из мониторинга")
        await cb_list_sources(call)

    @router.callback_query(F.data.startswith("src:pull:"))
    async def cb_pull_latest(call: CallbackQuery) -> None:
        chat_id = int(call.data.split(":")[2])
        await call.answer("⏳ Забираю последний пост и отправляю в ИИ...")
        ok, msg = await userbot.fetch_latest_post_from_source(
            chat_id=chat_id, admin_id=call.from_user.id
        )
        if not ok:
            await call.message.answer(f"⚠️ {msg}")

    # --- Настройки стиля ИИ и подписи ---

    @router.callback_query(F.data == "menu:prompt")
    async def cb_prompt_menu(call: CallbackQuery, state: FSMContext) -> None:
        curr = await db.get_setting("style_prompt", DEFAULT_STYLE_PROMPT)
        await state.set_state(AdminStates.waiting_for_style_prompt)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Сбросить на стандартный",
                        callback_data="menu:reset_prompt",
                    )
                ],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")],
            ]
        )
        await call.message.edit_text(
            "🎨 <b>Настройка промпта (стиля ИИ)</b>\n\n"
            f"<b>Текущая инструкция:</b>\n<blockquote>{html.escape(curr[:1500])}</blockquote>\n\n"
            "Отправьте в чат новый текст инструкции для ИИ (например: тон, длина, стиль подачи):",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.callback_query(F.data == "menu:reset_prompt")
    async def cb_reset_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await db.set_setting("style_prompt", DEFAULT_STYLE_PROMPT)
        await state.clear()
        await call.answer("Стандартный промпт восстановлен!", show_alert=True)
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.message(AdminStates.waiting_for_style_prompt)
    async def msg_save_prompt(message: Message, state: FSMContext) -> None:
        if not message.text:
            return
        await db.set_setting("style_prompt", message.text.strip())
        await state.clear()
        text, kb = await render_main_menu_text()
        await message.answer(
            "✅ Новый стиль ИИ сохранен!", reply_markup=kb, parse_mode="HTML"
        )

    @router.callback_query(F.data == "menu:signature")
    async def cb_signature_menu(call: CallbackQuery, state: FSMContext) -> None:
        curr = await db.get_setting("channel_signature", "")
        await state.set_state(AdminStates.waiting_for_signature)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить подпись", callback_data="menu:clear_sig"
                    )
                ],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:home")],
            ]
        )
        preview = AIService.strip_custom_emoji_tags(curr) if curr else "<i>не задана</i>"
        await call.message.edit_text(
            "✍️ <b>Настройка подписи в конце каждого поста</b>\n\n"
            f"Текущая подпись:\n{preview}\n\n"
            "Отправьте новую подпись (поддерживаются жирный шрифт, ссылки и платные анимированные эмодзи):",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await call.answer()

    @router.callback_query(F.data == "menu:clear_sig")
    async def cb_clear_sig(call: CallbackQuery, state: FSMContext) -> None:
        await db.set_setting("channel_signature", "")
        await state.clear()
        await call.answer("Подпись очищена!")
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.message(AdminStates.waiting_for_signature)
    async def msg_save_sig(message: Message, state: FSMContext) -> None:
        raw_html = message.html_text or message.text or ""
        await db.set_setting("channel_signature", raw_html.strip())
        await state.clear()
        text, kb = await render_main_menu_text()
        await message.answer(
            "✅ Подпись канала сохранена!", reply_markup=kb, parse_mode="HTML"
        )

    # --- Фирменный набор платных анимированных эмодзи (по ролям: списки, заголовки, общие) ---

    async def _render_emoji_palette_screen(target_role: str = "all") -> tuple[str, InlineKeyboardMarkup]:
        bullet_raw = await db.get_setting("emoji_list_bullet", "")
        head_raw = await db.get_setting("emoji_headline", "")
        pal_raw = await db.get_setting("custom_emoji_palette", "")

        b_tags = AIService.extract_custom_emoji_tags(bullet_raw)
        h_tags = AIService.extract_custom_emoji_tags(head_raw)
        p_tags = AIService.extract_custom_emoji_tags(pal_raw)

        b_str = f"<code>{html.escape(' '.join(b_tags))}</code>" if b_tags else "<i>из оригинала поста</i>"
        h_str = f"<code>{html.escape(' '.join(h_tags))}</code>" if h_tags else "<i>из оригинала поста</i>"
        p_str = f"<code>{html.escape(' '.join(p_tags))}</code>" if p_tags else "<i>не заданы</i>"

        role_hints = {
            "bullet": "👉 <b>Сейчас вы задаёте эмодзи ДЛЯ СПИСКОВ (буллет):</b> отправьте 1 платный анимированный эмодзи (например, красивую точку/маркер), который будет ставиться перед каждым пунктом списка!",
            "headline": "👉 <b>Сейчас вы задаёте эмодзи ДЛЯ ЗАГОЛОВКОВ:</b> отправьте 1–3 платных анимированных эмодзи (например, ⚡️ 🧠 🔥) для первой строки поста!",
            "palette": "👉 <b>Сейчас вы задаёте ОБЩИЕ эмодзи для акцентов:</b> отправьте любые любимые платные эмодзи через пробел!",
            "all": (
                "💡 <b>Как прикрепить свои эмодзи:</b>\n"
                "Нажмите одну из кнопок роли ниже («🔹 Для списков», «🔥 Для заголовков», «✨ Общие») и отправьте смайлик(и),\n"
                "<b>ИЛИ</b> отправьте всё сразу одним сообщением с подписями:\n"
                "<code>список: 🔴</code>\n"
                "<code>заголовок: ⚡️ 🧠</code>\n"
                "<code>общие: 🔥 💎 🚀</code>\n"
                "<i>(вместо обычных смайликов вставьте ваши платные анимированные Telegram Premium эмодзи!)</i>"
            ),
        }

        text = (
            "✨ <b>Настройка платных анимированных эмодзи по ролям</b>\n\n"
            f"🔹 <b>Для списков (маркер пунктов):</b> {b_str}\n"
            f"🔥 <b>Для заголовков (начало поста):</b> {h_str}\n"
            f"✨ <b>Общие / для акцентов ({len(p_tags)} шт.):</b> {p_str}\n\n"
            f"{role_hints.get(target_role, role_hints['all'])}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔹 Задать для списков", callback_data="emoji:role:bullet"
                    ),
                    InlineKeyboardButton(
                        text="🔥 Задать для заголовков", callback_data="emoji:role:headline"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="✨ Задать общие", callback_data="emoji:role:palette"
                    ),
                    InlineKeyboardButton(
                        text="🗑 Очистить всё", callback_data="menu:clear_emoji_palette"
                    ),
                ],
                [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="menu:home")],
            ]
        )
        return text, kb

    @router.callback_query(F.data == "menu:emoji_palette")
    async def cb_emoji_palette_menu(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.waiting_for_emoji_palette)
        await state.update_data(emoji_role="all")
        text, kb = await _render_emoji_palette_screen("all")
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @router.callback_query(F.data.startswith("emoji:role:"))
    async def cb_select_emoji_role(call: CallbackQuery, state: FSMContext) -> None:
        role = call.data.split(":")[2]
        await state.set_state(AdminStates.waiting_for_emoji_palette)
        await state.update_data(emoji_role=role)
        text, kb = await _render_emoji_palette_screen(role)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    @router.callback_query(F.data == "menu:clear_emoji_palette")
    async def cb_clear_emoji_palette(call: CallbackQuery, state: FSMContext) -> None:
        await db.set_setting("custom_emoji_palette", "")
        await db.set_setting("emoji_list_bullet", "")
        await db.set_setting("emoji_headline", "")
        await state.clear()
        await call.answer("Все фирменные эмодзи очищены!")
        text, kb = await render_main_menu_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    @router.message(AdminStates.waiting_for_emoji_palette)
    async def msg_save_emoji_palette(message: Message, state: FSMContext) -> None:
        raw_html = message.html_text or message.text or ""
        all_tags = AIService.extract_custom_emoji_tags(raw_html)
        if not all_tags:
            await message.answer(
                "⚠️ В вашем сообщении не найдено платных кастомных эмодзи (<code>&lt;tg-emoji&gt;</code>).\n"
                "Отправьте эмодзи именно из платного набора Telegram Premium (или нажмите «🔙 Назад в меню»).",
                parse_mode="HTML",
            )
            return

        data = await state.get_data()
        role = data.get("emoji_role", "all")

        # Проверяем, прислал ли пользователь структурированное сообщение с метками "список:", "заголовок:", "общие:"
        saved_roles: list[str] = []
        has_explicit_prefixes = bool(
            re.search(r"(?:список|буллет|маркер|заголовок|шапка|общи|акцент)", raw_html, flags=re.IGNORECASE)
        )
        if has_explicit_prefixes:
            for line in raw_html.splitlines():
                line_tags = AIService.extract_custom_emoji_tags(line)
                if not line_tags:
                    continue
                low = line.lower()
                if any(k in low for k in ("список", "списков", "буллет", "маркер", "пункт")):
                    await db.set_setting("emoji_list_bullet", line_tags[0])
                    saved_roles.append(f"для списков ({len(line_tags[:1])} шт.)")
                elif any(k in low for k in ("заголовок", "заголовк", "шапк", "начал")):
                    await db.set_setting("emoji_headline", " ".join(line_tags))
                    saved_roles.append(f"для заголовков ({len(line_tags)} шт.)")
                else:
                    await db.set_setting("custom_emoji_palette", " ".join(line_tags))
                    saved_roles.append(f"общие ({len(line_tags)} шт.)")

        if not saved_roles:
            if role == "bullet":
                await db.set_setting("emoji_list_bullet", all_tags[0])
                saved_roles.append("для списков (буллет)")
            elif role == "headline":
                await db.set_setting("emoji_headline", " ".join(all_tags))
                saved_roles.append(f"для заголовков ({len(all_tags)} шт.)")
            else:
                await db.set_setting("custom_emoji_palette", " ".join(all_tags))
                saved_roles.append(f"общие ({len(all_tags)} шт.)")

        text, kb = await _render_emoji_palette_screen("all")
        await message.answer(
            f"✅ <b>Сохранено ({', '.join(saved_roles)})!</b>\n\n{text}",
            reply_markup=kb,
            parse_mode="HTML",
        )

    # --- Действия в Предложке (Модерация постов) ---

    @router.callback_query(F.data.startswith("pub:approve:"))
    async def cb_approve_post(call: CallbackQuery) -> None:
        post_id = int(call.data.split(":")[2])
        post = await db.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return
        if post["status"] == "published":
            await call.answer("Этот пост уже был опубликован!", show_alert=True)
            return

        await call.answer("🚀 Публикую в канал...")
        ok = await publisher.publish_to_channel(post_id)
        if ok:
            done_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Опубликовано в канал!", callback_data="noop"
                        )
                    ]
                ]
            )
            await call.message.edit_reply_markup(reply_markup=done_kb)
        else:
            await call.message.answer(
                f"❌ Не удалось опубликовать пост #{post_id}. "
                f"Убедитесь, что бот добавлен в администраторы канала <code>{config.target_channel}</code> "
                f"с правом публикации сообщений.",
                parse_mode="HTML",
            )

    @router.callback_query(F.data.startswith("pub:reject:"))
    async def cb_reject_post(call: CallbackQuery) -> None:
        post_id = int(call.data.split(":")[2])
        post = await db.get_post(post_id)
        if post:
            await db.update_post_status(post_id, "rejected")
            publisher.cleanup_post_media(post)
        rejected_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Пост отклонен", callback_data="noop")]
            ]
        )
        await call.message.edit_reply_markup(reply_markup=rejected_kb)
        await call.answer("Пост отклонен и удален из очереди")

    @router.callback_query(F.data.startswith("pub:orig:"))
    async def cb_show_original(call: CallbackQuery) -> None:
        post_id = int(call.data.split(":")[2])
        post = await db.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return
        orig = post["original_text"] or "(Пост был без текста)"
        title_esc = html.escape(post["source_title"])
        try:
            cleaned_orig = re.sub(r"</?tg-emoji[^>]*>", "", orig)
            safe_orig = AIService.truncate_telegram_html(
                AIService.balance_telegram_html(cleaned_orig, strip_tg_links=False),
                3800,
            )
            await call.message.reply(
                f"👁 <b>Исходный пост из «{title_esc}»:</b>\n\n{safe_orig}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await call.message.reply(
                f"👁 <b>Исходный пост из «{title_esc}»:</b>\n\n{html.escape(orig[:3800])}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await call.answer()

    @router.callback_query(F.data.startswith("pub:freebie:"))
    async def cb_toggle_freebie(call: CallbackQuery) -> None:
        post_id = int(call.data.split(":")[2])
        post = await db.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return

        currently_freebie = AIService.has_freebie_tag(post["rewritten_text"])
        new_freebie = not currently_freebie
        new_text = AIService.set_freebie_tag(post["rewritten_text"], enabled=new_freebie)
        await db.update_post_text(post_id, new_text)
        post["rewritten_text"] = new_text

        media_items = [m for m in post["media_items"] if Path(m["path"]).exists()]
        has_media = len(media_items) > 0
        use_media = bool(post["use_media"]) and has_media
        new_kb = build_moderation_keyboard(
            post_id, has_media, use_media, is_freebie=new_freebie
        )
        preview_text = AIService.balance_telegram_html(
            AIService.strip_custom_emoji_tags(
                publisher.build_preview_text(post, len(media_items), has_media, use_media)
            )
        )

        try:
            if call.message.caption is not None:
                if len(preview_text) <= 1024:
                    await call.message.edit_caption(
                        caption=preview_text,
                        parse_mode="HTML",
                        reply_markup=new_kb,
                    )
                else:
                    # Подпись с добавленным тегом превысила лимит 1024 симв. — обновляем отдельным превью
                    await call.message.edit_reply_markup(reply_markup=new_kb)
                    await publisher.send_to_moderation(
                        post_id, target_admin_id=call.from_user.id
                    )
            elif call.message.text is not None:
                await call.message.edit_text(
                    text=AIService.truncate_telegram_html(preview_text, 4090),
                    parse_mode="HTML",
                    reply_markup=new_kb,
                    disable_web_page_preview=True,
                )
            else:
                await call.message.edit_reply_markup(reply_markup=new_kb)
        except Exception as exc:
            logger.warning("Не удалось обновить превью поста #%s: %s", post_id, exc)
            try:
                await call.message.edit_reply_markup(reply_markup=new_kb)
            except Exception:
                pass

        await call.answer(
            "🏷 Рубрика #халява добавлена!"
            if new_freebie
            else "Метка #халява убрана"
        )

    @router.callback_query(F.data.startswith("pub:toggle_media:"))
    async def cb_toggle_media(call: CallbackQuery) -> None:
        post_id = int(call.data.split(":")[2])
        use_media = await db.toggle_post_media(post_id)
        post = await db.get_post(post_id)
        if not post:
            return
        media_items = [m for m in post["media_items"] if Path(m["path"]).exists()]
        has_media = len(media_items) > 0
        is_freebie = AIService.has_freebie_tag(post["rewritten_text"])
        new_kb = build_moderation_keyboard(
            post_id, has_media, use_media and has_media, is_freebie=is_freebie
        )
        await call.message.edit_reply_markup(reply_markup=new_kb)
        await call.answer(
            "Медиа включено для публикации"
            if use_media
            else "Медиа отключено (опубликуется только текст)"
        )

    @router.callback_query(F.data.startswith("pub:rewrite:"))
    async def cb_rewrite_post(call: CallbackQuery, state: FSMContext) -> None:
        post_id = int(call.data.split(":")[2])
        await state.set_state(AdminStates.waiting_for_rewrite_hint)
        await state.update_data(post_id=post_id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎲 Просто переписать по-другому",
                        callback_data=f"pub:rewrite_now:{post_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✖️ Отмена", callback_data="pub:cancel_state"
                    )
                ],
            ]
        )
        await call.message.reply(
            f"🔄 <b>Перегенерация поста #{post_id}</b>\n\n"
            "Напишите в чат, что именно изменить (например: <i>«сделай в 2 раза короче»</i> или <i>«добавь интригу в заголовок»</i>), "
            "или нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.callback_query(F.data.startswith("pub:rewrite_now:"))
    async def cb_rewrite_now(call: CallbackQuery, state: FSMContext) -> None:
        post_id = int(call.data.split(":")[2])
        await state.clear()
        post = await db.get_post(post_id)
        if not post:
            await call.answer("Пост не найден", show_alert=True)
            return

        await call.message.edit_text("⏳ Переписываю пост через ИИ...")
        try:
            ai_res = await ai.process_post(
                original_text=post["original_text"],
                has_media=bool(post["use_media"] and post["media_items"]),
                extra_instruction="Сформулируй другими словами без лишней воды, сохранив оригинальное оформление, цитаты и ссылки.",
            )
            if ai_res.rewritten_text:
                await db.update_post_text(post_id, ai_res.rewritten_text)
            await call.message.delete()
            await publisher.send_to_moderation(post_id, target_admin_id=call.from_user.id)
        except Exception as exc:
            logger.exception("Ошибка перегенерации поста #%s: %s", post_id, exc)
            await call.message.edit_text(
                f"❌ Ошибка при обращении к ИИ: <code>{html.escape(str(exc)[:400])}</code>",
                parse_mode="HTML",
            )

    @router.message(AdminStates.waiting_for_rewrite_hint)
    async def msg_rewrite_with_hint(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        post_id = data.get("post_id")
        await state.clear()
        if not post_id or not message.text:
            return
        post = await db.get_post(int(post_id))
        if not post:
            return

        wait_msg = await message.answer("⏳ Переписываю с учетом ваших пожеланий...")
        try:
            ai_res = await ai.process_post(
                original_text=post["original_text"],
                has_media=bool(post["use_media"] and post["media_items"]),
                extra_instruction=message.text.strip(),
            )
            if ai_res.rewritten_text:
                await db.update_post_text(int(post_id), ai_res.rewritten_text)
            await wait_msg.delete()
            await publisher.send_to_moderation(
                int(post_id), target_admin_id=message.from_user.id
            )
        except Exception as exc:
            logger.exception("Ошибка перегенерации поста #%s по подсказке: %s", post_id, exc)
            await wait_msg.edit_text(
                f"❌ Ошибка при обращении к ИИ: <code>{html.escape(str(exc)[:400])}</code>",
                parse_mode="HTML",
            )

    @router.callback_query(F.data.startswith("pub:edit:"))
    async def cb_manual_edit(call: CallbackQuery, state: FSMContext) -> None:
        post_id = int(call.data.split(":")[2])
        await state.set_state(AdminStates.waiting_for_manual_edit)
        await state.update_data(post_id=post_id)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✖️ Отмена", callback_data="pub:cancel_state"
                    )
                ]
            ]
        )
        await call.message.reply(
            f"✏️ <b>Ручное редактирование поста #{post_id}</b>\n\n"
            "Отправьте мне готовый текст поста (можно использовать жирный шрифт, курсив, ссылки прямо в клиенте Telegram):",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await call.answer()

    @router.message(AdminStates.waiting_for_manual_edit)
    async def msg_manual_edit_save(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        post_id = data.get("post_id")
        await state.clear()
        if not post_id:
            return
        new_html = (message.html_text or message.text or "").strip()
        if not new_html:
            return
        new_html = AIService.balance_telegram_html(new_html, strip_tg_links=False)
        await db.update_post_text(int(post_id), new_html)
        await publisher.send_to_moderation(
            int(post_id), target_admin_id=message.from_user.id
        )

    @router.callback_query(F.data == "pub:cancel_state")
    async def cb_cancel_state(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.message.delete()
        await call.answer("Действие отменено")

    @router.callback_query(F.data == "noop")
    async def cb_noop(call: CallbackQuery) -> None:
        await call.answer()

    return router
