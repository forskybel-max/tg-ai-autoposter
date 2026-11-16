import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import logging
import re
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.extensions import html as tg_html
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
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

# Регистрируем поддержку <tg-spoiler> и чистого <pre> для точного переноса форматирования (в обе стороны: unparse и parse)
tg_html.ENTITY_TO_FORMATTER[MessageEntitySpoiler] = ("<tg-spoiler>", "</tg-spoiler>")
tg_html.ENTITY_TO_FORMATTER[MessageEntityPre] = lambda e, _: (
    f'<pre><code class="language-{e.language}">' if getattr(e, "language", "") else "<pre>",
    "</code></pre>" if getattr(e, "language", "") else "</pre>",
)

_orig_handle_starttag = tg_html.HTMLToTelegramParser.handle_starttag


def _patched_handle_starttag(self, tag, attrs):
    if tag == "tg-spoiler":
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)
        if tag not in self._building_entities:
            self._building_entities[tag] = MessageEntitySpoiler(
                offset=len(self.text), length=0
            )
        return
    return _orig_handle_starttag(self, tag, attrs)


tg_html.HTMLToTelegramParser.handle_starttag = _patched_handle_starttag

_orig_unparse = tg_html.unparse


def _patched_unparse(text: str, entities):
    if entities:
        # Сортируем сущности так, чтобы внешние/длинные теги (например, <b>Заголовок</b>)
        # открывались раньше вложенных коротких (<tg-emoji>🔥</tg-emoji>) при одинаковом offset
        sorted_ents = sorted(
            entities,
            key=lambda e: (getattr(e, "offset", 0), -getattr(e, "length", 0)),
        )
        return _orig_unparse(text, sorted_ents)
    return _orig_unparse(text, entities)


tg_html.unparse = _patched_unparse

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
        self._sync_task: asyncio.Task | None = None
        self._sync_after: datetime = datetime.now(timezone.utc) - timedelta(minutes=25)
        self._processing_keys: set[tuple[int, int]] = set()
        self._handled_order: deque[tuple[int, int]] = deque()
        self._handled_msg_ids: set[tuple[int, int]] = set()
        self._pending_groups: set[tuple[int, int]] = set()

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

    def _remember_handled(self, chat_id: int, msg_ids: list[int]) -> None:
        for mid in msg_ids:
            key = (chat_id, mid)
            if key not in self._handled_msg_ids:
                self._handled_msg_ids.add(key)
                self._handled_order.append(key)
        while len(self._handled_order) > 3000:
            old = self._handled_order.popleft()
            self._handled_msg_ids.discard(old)

    async def _warm_entity_cache(self) -> None:
        """
        Прогревает кэш диалогов и access_hash для StringSession,
        чтобы Telethon не пропускал апдейты и корректно работал get_channel_difference.
        """
        try:
            await self.client.get_dialogs(limit=200)
        except Exception as exc:
            logger.warning("Не удалось предзагрузить список диалогов: %s", exc)

        sources = await self.db.get_sources(only_enabled=True)
        for s in sources:
            cid = int(s["chat_id"])
            try:
                await self.client.get_entity(cid)
            except Exception:
                ref = (s.get("username_or_link") or "").strip().lstrip("@")
                if ref and "t.me/+" not in ref and "joinchat" not in ref:
                    try:
                        await self.client.get_entity(ref)
                    except Exception:
                        pass

    async def _on_authorized(self) -> None:
        me = await self.client.get_me()
        logger.info(
            "Telethon Userbot активен как: %s (ID: %s)",
            getattr(me, "first_name", "User"),
            me.id,
        )
        self._sync_after = datetime.now(timezone.utc) - timedelta(minutes=25)
        await self.reload_sources()
        await self._warm_entity_cache()
        if not self._handlers_registered:
            self._register_handlers()
            self._handlers_registered = True
        if not self._sync_task or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._catchup_sync_loop())

    async def stop(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        if self.client.is_connected():
            await self.client.disconnect()

    async def reload_sources(self) -> None:
        sources = await self.db.get_sources(only_enabled=True)
        self.enabled_chats = {int(s["chat_id"]) for s in sources}
        logger.info("Активных каналов-доноров на мониторинге: %d", len(self.enabled_chats))

    async def _delayed_group_fallback(self, chat_id: int, grouped_id: int) -> None:
        """
        Страховка от известного бага Telethon events.Album, когда одиночные посты с grouped_id
        или разделённые по DC альбомы отбрасываются обработчиком Album.filter.
        """
        gkey = (chat_id, grouped_id)
        try:
            await asyncio.sleep(4.0)
            if not self.client.is_connected():
                return
            msgs = await self.client.get_messages(chat_id, limit=12)
            group_msgs = [
                m
                for m in reversed(msgs)
                if isinstance(m, Message) and m.grouped_id == grouped_id and (m.message or m.media)
            ]
            if not group_msgs:
                return
            ids = [m.id for m in group_msgs]
            if any((chat_id, mid) in self._handled_msg_ids or (chat_id, mid) in self._processing_keys for mid in ids):
                return
            if await self.db.are_any_posts_processed(chat_id, ids):
                self._remember_handled(chat_id, ids)
                return
            logger.info("Резервная сборка группы/альбома (msg_id=%s, grouped_id=%s) в чате %s", ids[0], grouped_id, chat_id)
            await self._handle_messages(chat_id, group_msgs)
        except Exception as exc:
            logger.warning("Ошибка резервной сборки grouped_id=%s в %s: %s", grouped_id, chat_id, exc)
        finally:
            self._pending_groups.discard(gkey)

    async def _catchup_sync_loop(self) -> None:
        """
        Резервный периодический опрос каналов-доноров (раз в 90 сек).
        Гарантирует, что ни один пост не потеряется из-за троттлинга пассивных
        channel-апдейтов Telegram, сетевых сбоев (Bad Gateway) или короткого рестарта.
        """
        await asyncio.sleep(25)
        while True:
            try:
                sources = await self.db.get_sources(only_enabled=True)
                now = datetime.now(timezone.utc)
                cutoff = max(self._sync_after, now - timedelta(hours=2))

                for s in sources:
                    chat_id = int(s["chat_id"])
                    if chat_id not in self.enabled_chats:
                        continue
                    try:
                        try:
                            msgs = await self.client.get_messages(chat_id, limit=8)
                        except ValueError:
                            ref = (s.get("username_or_link") or "").strip().lstrip("@")
                            if ref and "t.me/+" not in ref and "joinchat" not in ref:
                                entity = await self.client.get_entity(ref)
                                msgs = await self.client.get_messages(entity, limit=8)
                            else:
                                continue

                        if not msgs:
                            continue

                        valid: list[Message] = []
                        for m in msgs:
                            if not isinstance(m, Message) or not (m.message or m.media):
                                continue
                            m_dt = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
                            if m_dt >= cutoff:
                                valid.append(m)

                        if not valid:
                            continue

                        # Собираем хронологически от старых к новым и склеиваем альбомы по grouped_id
                        chronological = sorted(valid, key=lambda m: m.id)
                        groups: dict[int, list[Message]] = {}
                        ordered_posts: list[list[Message]] = []
                        for m in chronological:
                            if m.grouped_id is not None:
                                if m.grouped_id not in groups:
                                    bucket: list[Message] = []
                                    groups[m.grouped_id] = bucket
                                    ordered_posts.append(bucket)
                                groups[m.grouped_id].append(m)
                            else:
                                ordered_posts.append([m])

                        for post_msgs in ordered_posts:
                            # Даём 8 секунд свежим сообщениям, чтобы сработал мгновенный realtime-хендлер и собрался весь альбом
                            newest_dt = max(
                                (pm.date if pm.date.tzinfo else pm.date.replace(tzinfo=timezone.utc))
                                for pm in post_msgs
                            )
                            if (now - newest_dt).total_seconds() < 8:
                                continue

                            ids = [pm.id for pm in post_msgs]
                            if any(
                                (chat_id, mid) in self._handled_msg_ids
                                or (chat_id, mid) in self._processing_keys
                                for mid in ids
                            ):
                                continue
                            if await self.db.are_any_posts_processed(chat_id, ids):
                                self._remember_handled(chat_id, ids)
                                continue

                            logger.info(
                                "Catch-up sync обнаружил пропущенный пост в '%s' (msg_id=%s)",
                                s["title"],
                                ids[0],
                            )
                            await self._handle_messages(chat_id, post_msgs)
                    except FloodWaitError as fw:
                        logger.warning("FloodWait при catch-up sync (%s сек)", fw.seconds)
                        await asyncio.sleep(min(fw.seconds + 2, 60))
                    except Exception as exc:
                        logger.warning("Ошибка catch-up sync для %s (%s): %s", s["title"], chat_id, exc)

                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Сбой в цикле catch-up sync: %s", exc)

            await asyncio.sleep(90)

    def _register_handlers(self) -> None:
        @self.client.on(events.NewMessage())
        async def on_new_message(event: events.NewMessage.Event) -> None:
            chat_id = event.chat_id
            if chat_id is None and event.message and event.message.peer_id:
                chat_id = utils.get_peer_id(event.message.peer_id)
            if chat_id not in self.enabled_chats:
                return
            if not isinstance(event.message, Message):
                return
            if event.message.grouped_id is not None:
                gkey = (chat_id, event.message.grouped_id)
                if gkey not in self._pending_groups:
                    self._pending_groups.add(gkey)
                    asyncio.create_task(self._delayed_group_fallback(chat_id, event.message.grouped_id))
                return
            await self._handle_messages(chat_id, [event.message])

        @self.client.on(events.Album())
        async def on_album(event: events.Album.Event) -> None:
            chat_id = event.chat_id
            if chat_id is None and event.messages and event.messages[0].peer_id:
                chat_id = utils.get_peer_id(event.messages[0].peer_id)
            if chat_id not in self.enabled_chats:
                return
            await self._handle_messages(chat_id, list(event.messages))

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
            try:
                messages = await self.client.get_messages(chat_id, limit=10)
            except ValueError:
                src = await self.db.get_source(chat_id)
                ref = (src.get("username_or_link") or "").strip().lstrip("@") if src else ""
                if not ref:
                    raise
                entity = await self.client.get_entity(ref)
                messages = await self.client.get_messages(entity, limit=10)
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
        ordered = sorted(messages, key=lambda m: m.id)
        if not ordered:
            return

        msg_ids = [m.id for m in ordered]
        primary_msg = ordered[0]
        msg_id = primary_msg.id

        if not force:
            if any(
                (chat_id, mid) in self._processing_keys
                or (chat_id, mid) in self._handled_msg_ids
                for mid in msg_ids
            ):
                return
            if await self.db.are_any_posts_processed(chat_id, msg_ids):
                self._remember_handled(chat_id, msg_ids)
                return

        lock_keys = [(chat_id, mid) for mid in msg_ids]
        self._processing_keys.update(lock_keys)
        try:
            original_text = ""
            for m in ordered:
                if m.message and m.message.strip():
                    # Извлекаем текст вместе с HTML-оформлением (цитаты, жирный, код, скрытые гиперссылки)
                    original_text = tg_html.unparse(m.message, m.entities or []).strip()
                    break

            # Если ссылка на сервис спрятана только в превью (или под невидимым символом), сохраняем её
            for m in ordered:
                wp = getattr(m, "web_preview", None)
                wp_url = getattr(wp, "url", None) if wp else None
                if (
                    wp_url
                    and isinstance(wp_url, str)
                    and wp_url.startswith("http")
                    and "t.me/" not in wp_url.lower()
                    and "telegram.me/" not in wp_url.lower()
                    and wp_url not in original_text
                ):
                    original_text = f'{original_text}\n\n<a href="{wp_url}">{wp_url}</a>'.strip()
                    break

            source = await self.db.get_source(chat_id)
            source_title = source["title"] if source else str(chat_id)

            media_items = await self._download_media(chat_id, ordered)

            if not original_text and not media_items:
                self._remember_handled(chat_id, msg_ids)
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
                self._remember_handled(chat_id, msg_ids)
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
            self._remember_handled(chat_id, msg_ids)

            auto_publish = (await self.db.get_setting("auto_publish", "0")) == "1"
            if auto_publish and not force:
                ok = await self.publisher.publish_to_channel(post_id)
                logger.info("Автопубликация поста #%s: %s", post_id, ok)
            else:
                await self.publisher.send_to_moderation(
                    post_id, target_admin_id=target_admin_id
                )
        finally:
            self._processing_keys.difference_update(lock_keys)

    async def _download_media(
        self, chat_id: int, messages: list[Message]
    ) -> list[dict]:
        max_bytes = self.config.max_media_size_mb * 1024 * 1024
        downloaded: list[dict] = []

        for m in messages:
            if not m.media:
                continue

            media_type: str | None = None
            mime = getattr(getattr(m, "file", None), "mime_type", "") or ""
            if m.photo or (mime.startswith("image/") and not m.sticker):
                if m.file and m.file.size and m.file.size > max_bytes:
                    logger.warning("Изображение в посте %s слишком большое, пропускаем.", m.id)
                    continue
                media_type = "photo"
            elif (m.video or m.gif or mime.startswith("video/")) and not m.sticker and not m.video_note:
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


class PremiumPublisherClient:
    """
    Отдельный легковесный Telethon-клиент для основного аккаунта с Telegram Premium.
    НЕ подписывается на каналы, НЕ слушает сообщения и НЕ занимается парсингом (0% риска для основы).
    Используется исключительно для отправки одобренных постов в целевой канал,
    чтобы Telegram активировал платные анимированные эмодзи (<tg-emoji emoji-id="...">).
    """

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        session_obj = (
            StringSession(self.config.tg_publisher_string_session)
            if self.config.tg_publisher_string_session
            else (self.config.tg_publisher_session_name or StringSession())
        )
        self.client = TelegramClient(
            session_obj,
            self.config.tg_api_id,
            self.config.tg_api_hash,
            system_version="4.16.30-vxCUSTOM",
            receive_updates=False,
        )
        self._pending_phone: str | None = None
        self._pending_phone_hash: str | None = None
        self._dialogs_warmed: bool = False

    def export_string_session(self) -> str:
        return StringSession.save(self.client.session)

    async def start(self) -> bool:
        """Проверяет наличие сохранённой сессии публикатора (в env или БД) и подключает её."""
        if not self.config.tg_publisher_string_session:
            db_sess = (await self.db.get_setting("publisher_string_session", "")).strip()
            if db_sess:
                self.config.tg_publisher_string_session = db_sess
                self.client = TelegramClient(
                    StringSession(db_sess),
                    self.config.tg_api_id,
                    self.config.tg_api_hash,
                    system_version="4.16.30-vxCUSTOM",
                    receive_updates=False,
                )

        try:
            if not self.client.is_connected():
                await self.client.connect()
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                is_prem = bool(getattr(me, "premium", False))
                logger.info(
                    "Premium-публикатор (Основа) подключен: %s (ID: %s, Premium=%s)",
                    getattr(me, "first_name", "User"),
                    me.id,
                    is_prem,
                )
                return True
        except Exception as exc:
            logger.warning("Не удалось инициализировать Premium-публикатор: %s", exc)
        return False

    async def stop(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

    async def is_authorized(self) -> bool:
        try:
            if not self.client.is_connected():
                await self.client.connect()
            return await self.client.is_user_authorized()
        except Exception:
            return False

    async def get_account_label(self) -> str:
        if not await self.is_authorized():
            return "⚪ Не подключен (публикует бот)"
        me = await self.client.get_me()
        name = getattr(me, "first_name", "") or "User"
        username = getattr(me, "username", None)
        is_prem = bool(getattr(me, "premium", False))
        prem_badge = "💎 Premium" if is_prem else "⚠️ без Premium"
        suffix = f"@{username}" if username else f"ID: {me.id}"
        return f"✅ {name} ({suffix}, {prem_badge})"

    async def _persist_session(self) -> str:
        sess_str = self.export_string_session()
        self.config.tg_publisher_string_session = sess_str
        await self.db.set_setting("publisher_string_session", sess_str)
        try:
            from dotenv import set_key
            from app.config import BASE_DIR

            env_file = BASE_DIR / ".env"
            if env_file.exists():
                set_key(str(env_file), "TG_PUBLISHER_STRING_SESSION", sess_str)
        except Exception:
            pass
        return sess_str

    async def send_login_code(self, phone: str) -> tuple[bool, str]:
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
            logger.exception("Ошибка отправки кода для Premium-публикатора на %s: %s", clean_phone, exc)
            return False, f"Не удалось отправить код: <code>{exc}</code>"

    async def submit_login_code(self, raw_code: str) -> tuple[str, str]:
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
            self._dialogs_warmed = False
            sess_str = await self._persist_session()
            label = await self.get_account_label()
            return (
                "ok",
                f"🎉 <b>Premium-публикатор успешно подключен:</b> {label}!\n\n"
                f"☁️ Строка <code>TG_PUBLISHER_STRING_SESSION</code> (для Render):\n"
                f"<code>{sess_str}</code>",
            )
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
                "Запросите новый код и введите его через пробелы: <code>1 2 3 4 5</code>.",
            )
        except Exception as exc:
            logger.exception("Ошибка входа в Premium-публикатор: %s", exc)
            return "error", f"❌ Ошибка авторизации: <code>{exc}</code>"

    async def submit_2fa_password(self, password: str) -> tuple[bool, str]:
        try:
            await self.client.sign_in(password=password)
            self._pending_phone = None
            self._pending_phone_hash = None
            self._dialogs_warmed = False
            sess_str = await self._persist_session()
            label = await self.get_account_label()
            return (
                True,
                f"🎉 <b>Premium-публикатор успешно подключен:</b> {label}!\n\n"
                f"☁️ Строка <code>TG_PUBLISHER_STRING_SESSION</code> (для Render):\n"
                f"<code>{sess_str}</code>",
            )
        except Exception as exc:
            logger.exception("Ошибка ввода 2FA для Premium-публикатора: %s", exc)
            return False, f"❌ Неверный облачный пароль или ошибка: <code>{exc}</code>"

    async def _resolve_target(self, target: int | str):
        try:
            return await self.client.get_entity(target)
        except ValueError:
            if not self._dialogs_warmed:
                await self.client.get_dialogs(limit=150)
                self._dialogs_warmed = True
            return await self.client.get_entity(target)

    async def _send_html_text(self, entity, text: str) -> None:
        safe_html = AIService.truncate_telegram_html(text, 4090)
        await self.client.send_message(
            entity,
            safe_html,
            parse_mode="html",
            link_preview=False,
        )

    async def _send_single_media(self, entity, item: dict, caption: str | None) -> None:
        safe_caption = AIService.truncate_telegram_html(caption, 1024) if caption else None
        if item.get("type") == "video":
            attrs = []
            w = int(item.get("width") or 0)
            h = int(item.get("height") or 0)
            dur = int(item.get("duration") or 0)
            if w or h or dur:
                attrs.append(
                    DocumentAttributeVideo(
                        duration=dur,
                        w=w or 720,
                        h=h or 1280,
                        supports_streaming=True,
                    )
                )
            thumb = item.get("thumb_path")
            thumb_arg = thumb if (thumb and Path(thumb).exists()) else None
            await self.client.send_file(
                entity,
                item["path"],
                caption=safe_caption,
                parse_mode="html" if safe_caption else None,
                supports_streaming=True,
                attributes=attrs or None,
                thumb=thumb_arg,
            )
        else:
            await self.client.send_file(
                entity,
                item["path"],
                caption=safe_caption,
                parse_mode="html" if safe_caption else None,
            )

    async def publish_post(
        self,
        target: int | str,
        text: str,
        media_items: list[dict],
        use_media: bool,
    ) -> bool:
        """Публикует пост в целевой канал через аккаунт с Telegram Premium."""
        if not await self.is_authorized():
            return False

        entity = await self._resolve_target(target)
        if not use_media:
            if not text:
                return False
            await self._send_html_text(entity, text)
        elif len(media_items) == 1:
            item = media_items[0]
            if len(text) <= 1024:
                await self._send_single_media(entity, item, caption=text or None)
            else:
                await self._send_single_media(entity, item, caption=None)
                await self._send_html_text(entity, text)
        else:
            caption_for_album = (
                AIService.truncate_telegram_html(text, 1024)
                if (text and len(text) <= 1024)
                else None
            )
            files = [m["path"] for m in media_items[:10]]
            await self.client.send_file(
                entity,
                files,
                caption=caption_for_album,
                parse_mode="html" if caption_for_album else None,
                supports_streaming=True,
            )
            if len(text) > 1024:
                await self._send_html_text(entity, text)
        return True
