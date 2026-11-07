import logging
import re
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.extensions import html as tg_html
from telethon.sessions import StringSession
from telethon.errors import (
    InviteRequestSentError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import (
    ChatInviteAlready,
    DocumentAttributeVideo,
    Message,
    MessageEntityPre,
    MessageEntitySpoiler,
)

# Регистрируем поддержку <tg-spoiler> и чистого <pre> для точного переноса форматирования
tg_html.ENTITY_TO_FORMATTER[MessageEntitySpoiler] = ("<tg-spoiler>", "</tg-spoiler>")
tg_html.ENTITY_TO_FORMATTER[MessageEntityPre] = lambda e, _: (
    f'<pre><code class="language-{e.language}">' if getattr(e, "language", "") else "<pre>",
    "</code></pre>" if getattr(e, "language", "") else "</pre>",
)

from app.ai_service import AIService
from app.config import Config, MEDIA_DIR
from app.database import Database
from app.publisher import Publisher

logger = logging.getLogger(__name__)


class UserbotParser:
    """Telethon-клиент для подписки и парсинга открытых и закрытых каналов."""

    def __init__(
        self,
        config: Config,
        db: Database,
        ai_service: AIService,
        publisher: Publisher,
    ):
        self.config = config
        self.db = db
        self.ai = ai_service
        self.publisher = publisher
        session_obj = (
            StringSession(self.config.tg_string_session)
            if self.config.tg_string_session
            else self.config.tg_session_name
        )
        self.client = TelegramClient(
            session_obj,
            self.config.tg_api_id,
            self.config.tg_api_hash,
            system_version="4.16.30-vxCUSTOM",
        )
        self.enabled_chats: set[int] = set()
        self._handlers_registered: bool = False
        self._pending_phone: str | None = None
        self._pending_phone_hash: str | None = None

    def export_string_session(self) -> str:
        """Экспортирует текущую сессию в строку TG_STRING_SESSION для облачного хостинга."""
        return StringSession.save(self.client.session)

    async def start(self) -> bool:
        """
        Подключается к Telegram без блокировки консоли.
        Возвращает True, если сессия Userbot уже авторизована, иначе False
        (тогда авторизацию можно пройти прямо через бота в Telegram!).
        """
        if not self.client.is_connected():
            await self.client.connect()

        if await self.client.is_user_authorized():
            await self._on_authorized()
            return True

        logger.warning(
            "Userbot пока не авторизован! Откройте вашего бота в Telegram, "
            "нажмите /start и нажмите кнопку '🔑 Войти в Userbot'."
        )
        return False

    async def is_authorized(self) -> bool:
        if not self.client.is_connected():
            await self.client.connect()
        return await self.client.is_user_authorized()

    async def get_account_label(self) -> str:
        if not await self.is_authorized():
            return "❌ Не авторизован"
        me = await self.client.get_me()
        name = getattr(me, "first_name", "") or "User"
        username = getattr(me, "username", None)
        suffix = f" (@{username})" if username else f" (ID: {me.id})"
        return f"✅ {name}{suffix}"

    async def send_login_code(self, phone: str) -> tuple[bool, str]:
        """Отправляет код подтверждения на указанный номер телефона."""
        clean_phone = re.sub(r"[^\d+]", "", phone.strip())
        if not clean_phone.startswith("+"):
            clean_phone = "+" + clean_phone
        try:
            if not self.client.is_connected():
                await self.client.connect()
            sent = await self.client.send_code_request(clean_phone)
            self._pending_phone = clean_phone
            self._pending_phone_hash = sent.phone_code_hash
            return True, clean_phone
        except Exception as exc:
            logger.exception("Ошибка отправки кода на %s: %s", clean_phone, exc)
            return False, f"Не удалось отправить код: <code>{exc}</code>"

    async def submit_login_code(self, raw_code: str) -> tuple[str, str]:
        """
        Завершает вход по коду.
        Возвращает статус: 'ok' | 'need_2fa' | 'error' и сообщение.
        """
        code = re.sub(r"\D", "", raw_code)
        if not code:
            return "error", "В сообщении не найдено цифр кода. Попробуйте ещё раз."
        if not self._pending_phone or not self._pending_phone_hash:
            return "error", "Сессия ввода номера истекла. Начните вход заново."

        try:
            await self.client.sign_in(
                phone=self._pending_phone,
                code=code,
                phone_code_hash=self._pending_phone_hash,
            )
            self._pending_phone = None
            self._pending_phone_hash = None
            await self._on_authorized()
            label = await self.get_account_label()
            return "ok", f"🎉 Успешный вход в Userbot: <b>{label}</b>!"
        except SessionPasswordNeededError:
            return (
                "need_2fa",
                "🔐 На этом аккаунте включен <b>облачный пароль (2FA)</b>.\n"
                "Отправьте пароль ответным сообщением (бот сразу удалит ваше сообщение с паролем из чата):",
            )
        except PhoneCodeInvalidError:
            return "error", "❌ Неверный код. Проверьте цифры и отправьте снова (через пробел, например <code>1 2 3 4 5</code>)."
        except PhoneCodeExpiredError:
            return (
                "error",
                "⚠️ Код истек или был заблокирован Telegram (не пересылайте код напрямую без пробелов!). "
                "Нажмите «🔑 Войти в Userbot» и запросите новый код, затем введите его через пробелы: <code>1 2 3 4 5</code>.",
            )
        except Exception as exc:
            logger.exception("Ошибка входа по коду: %s", exc)
            return "error", f"❌ Ошибка авторизации: <code>{exc}</code>"

    async def submit_2fa_password(self, password: str) -> tuple[bool, str]:
        """Завершает вход при включенном облачном пароле (2FA)."""
        try:
            await self.client.sign_in(password=password)
            self._pending_phone = None
            self._pending_phone_hash = None
            await self._on_authorized()
            label = await self.get_account_label()
            return True, f"🎉 Успешный вход в Userbot: <b>{label}</b>!"
        except Exception as exc:
            logger.exception("Ошибка ввода 2FA пароля: %s", exc)
            return False, f"❌ Неверный облачный пароль или ошибка: <code>{exc}</code>"

    async def _on_authorized(self) -> None:
        me = await self.client.get_me()
        logger.info(
            "Telethon Userbot активен как: %s (ID: %s)",
            getattr(me, "first_name", "User"),
            me.id,
        )
        await self.reload_sources()
        if not self._handlers_registered:
            self._register_handlers()
            self._handlers_registered = True

    async def stop(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

    async def reload_sources(self) -> None:
        sources = await self.db.get_sources(only_enabled=True)
        self.enabled_chats = {int(s["chat_id"]) for s in sources}
        logger.info("Активных каналов-доноров на мониторинге: %d", len(self.enabled_chats))

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage())
        async def on_new_message(event: events.NewMessage.Event) -> None:
            if event.chat_id not in self.enabled_chats:
                return
            if event.message.grouped_id is not None:
                return
            await self._handle_messages(event.chat_id, [event.message])

        @self.client.on(events.Album())
        async def on_album(event: events.Album.Event) -> None:
            if event.chat_id not in self.enabled_chats:
                return
            await self._handle_messages(event.chat_id, list(event.messages))

    async def join_and_register_channel(self, raw_input: str) -> tuple[bool, str]:
        """
        Принимает @username, публичную ссылку t.me/... или закрытую инвайт-ссылку t.me/+...,
        подписывает юзербота и сохраняет канал в базу.
        """
        if not await self.is_authorized():
            return (
                False,
                "⚠️ Сначала авторизуйте аккаунт-парсер (кнопка «🔑 Войти в Userbot» в главном меню)!",
            )

        raw = raw_input.strip()

        # 1. Проверка на приватную инвайт-ссылку (t.me/+xxx или t.me/joinchat/xxx)
        invite_match = re.search(
            r"(?:t\.me/(?:\+|joinchat/)|^\+)([a-zA-Z0-9_-]+)", raw
        )
        if invite_match:
            invite_hash = invite_match.group(1)
            try:
                invite_info = await self.client(CheckChatInviteRequest(invite_hash))
                if isinstance(invite_info, ChatInviteAlready):
                    entity = invite_info.chat
                else:
                    updates = await self.client(ImportChatInviteRequest(invite_hash))
                    entity = updates.chats[0]

                chat_id = utils.get_peer_id(entity)
                title = getattr(entity, "title", f"Private {chat_id}")
                await self.db.add_source(
                    chat_id=chat_id,
                    title=title,
                    username_or_link=raw,
                    is_private=True,
                )
                await self.reload_sources()
                return True, f"🔒 Закрытый канал <b>{title}</b> (<code>{chat_id}</code>) успешно подключен!"
            except InviteRequestSentError:
                return (
                    False,
                    "⏳ Заявка на вступление в закрытый канал отправлена! "
                    "Когда администратор канала её одобрит, отправьте эту ссылку боту повторно.",
                )
            except UserAlreadyParticipantError:
                entity = await self.client.get_entity(raw)
                chat_id = utils.get_peer_id(entity)
                title = getattr(entity, "title", f"Private {chat_id}")
                await self.db.add_source(chat_id, title, raw, is_private=True)
                await self.reload_sources()
                return True, f"🔒 Закрытый канал <b>{title}</b> добавлен в базу!"
            except Exception as exc:
                logger.exception("Ошибка подключения закрытого канала: %s", exc)
                return False, f"❌ Не удалось подключить закрытый канал: <code>{exc}</code>"

        # 2. Открытый канал (@username, https://t.me/channel, https://t.me/s/channel или ID)
        cleaned = raw
        pub_match = re.search(r"t\.me/(?:s/)?([a-zA-Z0-9_]{4,32})", raw)
        if pub_match:
            cleaned = pub_match.group(1)
        elif cleaned.startswith("@"):
            cleaned = cleaned[1:]

        try:
            target_ref: int | str = (
                int(cleaned) if cleaned.lstrip("-").isdigit() else cleaned
            )
            entity = await self.client.get_entity(target_ref)
            try:
                await self.client(JoinChannelRequest(entity))
            except Exception:
                pass

            chat_id = utils.get_peer_id(entity)
            title = getattr(entity, "title", str(cleaned))
            username = getattr(entity, "username", None)
            display_link = f"@{username}" if username else raw

            await self.db.add_source(
                chat_id=chat_id,
                title=title,
                username_or_link=display_link,
                is_private=not bool(username),
            )
            await self.reload_sources()
            return True, f"📢 Канал <b>{title}</b> ({display_link}) успешно добавлен на мониторинг!"
        except Exception as exc:
            logger.exception("Ошибка добавления канала %s: %s", raw, exc)
            return (
                False,
                f"❌ Не удалось найти или подключить канал <code>{raw}</code>.\n"
                f"Детали: <code>{exc}</code>",
            )

    async def fetch_latest_post_from_source(
        self, chat_id: int, admin_id: int
    ) -> tuple[bool, str]:
        """Принудительно забирает самый свежий пост из канала-донора (для мгновенного теста)."""
        if not await self.is_authorized():
            return False, "Сначала авторизуйте Userbot в главном меню!"

        try:
            messages = await self.client.get_messages(chat_id, limit=10)
            if not messages:
                return False, "В канале пока нет доступных сообщений."

            valid = [
                m for m in messages if isinstance(m, Message) and (m.message or m.media)
            ]
            if not valid:
                return False, "Не найдено подходящих постов с текстом или медиа."

            first = valid[0]
            if first.grouped_id:
                album_msgs = [
                    m for m in reversed(valid) if m.grouped_id == first.grouped_id
                ]
                await self._handle_messages(
                    chat_id, album_msgs, force=True, target_admin_id=admin_id
                )
            else:
                await self._handle_messages(
                    chat_id, [first], force=True, target_admin_id=admin_id
                )
            return True, "Пост успешно обработан ИИ и отправлен вам в предложку!"
        except Exception as exc:
            logger.exception("Ошибка тестового парсинга %s: %s", chat_id, exc)
            return False, f"Ошибка чтения канала: {exc}"

    async def _handle_messages(
        self,
        chat_id: int,
        messages: list[Message],
        force: bool = False,
        target_admin_id: int | None = None,
    ) -> None:
        if not messages:
            return

        primary_msg = messages[0]
        msg_id = primary_msg.id

        if not force and await self.db.is_post_processed(chat_id, msg_id):
            return

        original_text = ""
        for m in messages:
            if m.message and m.message.strip():
                # Извлекаем текст вместе с HTML-оформлением (цитаты, жирный, код, скрытые гиперссылки)
                original_text = tg_html.unparse(m.message, m.entities or []).strip()
                break

        source = await self.db.get_source(chat_id)
        source_title = source["title"] if source else str(chat_id)

        media_items = await self._download_media(chat_id, messages)

        if not original_text and not media_items:
            return

        logger.info(
            "Новый пост из '%s' (msg_id=%s, медиа=%d шт.). Отправляем в ИИ...",
            source_title,
            msg_id,
            len(media_items),
        )

        try:
            ai_result = await self.ai.process_post(
                original_text=original_text,
                has_media=len(media_items) > 0,
            )
        except Exception as exc:
            logger.exception("Сбой при обращении к ИИ: %s", exc)
            import html as _html
            err_desc = f"{type(exc).__name__}: {exc}".strip(": ")
            recipients = [target_admin_id] if target_admin_id else self.config.admin_ids
            for adm_id in recipients:
                try:
                    await self.publisher.bot.send_message(
                        chat_id=adm_id,
                        text=(
                            f"⚠️ <b>Пост из «{_html.escape(source_title)}» перехвачен, "
                            f"но нейросеть вернула ошибку:</b>\n"
                            f"<code>{_html.escape(err_desc[:600])}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        if ai_result.is_rejected and not force:
            logger.info(
                "Пост %s из '%s' отфильтрован ИИ. Причина: %s",
                msg_id,
                source_title,
                ai_result.reject_reason,
            )
            post_id = await self.db.create_post(
                source_chat_id=chat_id,
                source_msg_id=msg_id,
                source_title=source_title,
                original_text=original_text,
                rewritten_text="",
                media_items=media_items,
                status="filtered_ad",
            )
            post_data = await self.db.get_post(post_id)
            if post_data:
                self.publisher.cleanup_post_media(post_data)
            return

        rewritten = ai_result.rewritten_text or original_text
        post_id = await self.db.create_post(
            source_chat_id=chat_id,
            source_msg_id=msg_id,
            source_title=source_title,
            original_text=original_text,
            rewritten_text=rewritten,
            media_items=media_items,
            status="pending",
        )

        auto_publish = (await self.db.get_setting("auto_publish", "0")) == "1"
        if auto_publish and not force:
            ok = await self.publisher.publish_to_channel(post_id)
            logger.info("Автопубликация поста #%s: %s", post_id, ok)
        else:
            await self.publisher.send_to_moderation(
                post_id, target_admin_id=target_admin_id
            )

    async def _download_media(
        self, chat_id: int, messages: list[Message]
    ) -> list[dict]:
        max_bytes = self.config.max_media_size_mb * 1024 * 1024
        downloaded: list[dict] = []

        for m in messages:
            if not m.media:
                continue

            media_type: str | None = None
            if m.photo:
                media_type = "photo"
            elif (m.video or m.gif) and not m.sticker and not m.video_note:
                if m.file and m.file.size and m.file.size > max_bytes:
                    logger.warning(
                        "Видео в посте %s слишком большое (%d МБ > %d МБ), пропускаем файл.",
                        m.id,
                        m.file.size // (1024 * 1024),
                        self.config.max_media_size_mb,
                    )
                    continue
                media_type = "video"

            if not media_type:
                continue

            ext = ".mp4" if media_type == "video" else ".jpg"
            target_file = MEDIA_DIR / f"src_{abs(chat_id)}_{m.id}{ext}"
            try:
                saved_path = await self.client.download_media(
                    m, file=str(target_file)
                )
                if not saved_path or not Path(saved_path).exists():
                    continue

                item_meta: dict = {
                    "path": str(saved_path),
                    "type": media_type,
                }

                if media_type == "video" and m.document:
                    for attr in m.document.attributes or []:
                        if isinstance(attr, DocumentAttributeVideo):
                            if getattr(attr, "w", None):
                                item_meta["width"] = int(attr.w)
                            if getattr(attr, "h", None):
                                item_meta["height"] = int(attr.h)
                            if getattr(attr, "duration", None) is not None:
                                item_meta["duration"] = int(round(float(attr.duration)))
                            break

                    if getattr(m.document, "thumbs", None):
                        thumb_target = MEDIA_DIR / f"src_{abs(chat_id)}_{m.id}_thumb.jpg"
                        try:
                            saved_thumb = await self.client.download_media(
                                m, thumb=-1, file=str(thumb_target)
                            )
                            if saved_thumb and Path(saved_thumb).exists():
                                item_meta["thumb_path"] = str(saved_thumb)
                        except Exception:
                            pass

                downloaded.append(item_meta)
            except Exception as exc:
                logger.warning("Не удалось скачать медиа поста %s: %s", m.id, exc)

        return downloaded
