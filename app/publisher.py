import html
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)

from app.ai_service import AIService
from app.config import Config
from app.database import Database

logger = logging.getLogger(__name__)


def build_moderation_keyboard(
    post_id: int,
    has_media: bool,
    use_media: bool,
    is_freebie: bool = False,
) -> InlineKeyboardMarkup:
    """Формирует инлайн-клавиатуру для поста в предложке."""
    freebie_label = "✅ #халява" if is_freebie else "🏷 + #халява"
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Опубликовать в канал",
                callback_data=f"pub:approve:{post_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Переписать (ИИ)",
                callback_data=f"pub:rewrite:{post_id}",
            ),
            InlineKeyboardButton(
                text="✏️ Свой текст",
                callback_data=f"pub:edit:{post_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=freebie_label,
                callback_data=f"pub:freebie:{post_id}",
            ),
            InlineKeyboardButton(
                text="👁 Исходник",
                callback_data=f"pub:orig:{post_id}",
            ),
        ],
    ]

    bottom_row: list[InlineKeyboardButton] = []
    if has_media:
        media_label = "🖼 Убрать медиа" if use_media else "🖼 Вернуть медиа"
        bottom_row.append(
            InlineKeyboardButton(
                text=media_label,
                callback_data=f"pub:toggle_media:{post_id}",
            )
        )
    bottom_row.append(
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"pub:reject:{post_id}",
        )
    )
    rows.append(bottom_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Publisher:
    def __init__(self, bot: Bot, config: Config, db: Database, premium_publisher: Any = None):
        self.bot = bot
        self.config = config
        self.db = db
        self.premium_publisher = premium_publisher

    @staticmethod
    def _video_kwargs(item: dict[str, Any]) -> dict[str, Any]:
        """Параметры для нативного потокового воспроизведения видео в Telegram без скачивания."""
        kwargs: dict[str, Any] = {"supports_streaming": True}
        if item.get("width"):
            kwargs["width"] = int(item["width"])
        if item.get("height"):
            kwargs["height"] = int(item["height"])
        if item.get("duration") is not None:
            kwargs["duration"] = int(item["duration"])
        thumb = item.get("thumb_path")
        if thumb and Path(thumb).exists():
            kwargs["thumbnail"] = FSInputFile(thumb)
        return kwargs

    @staticmethod
    def build_preview_text(
        post: dict[str, Any],
        media_count: int,
        has_media: bool,
        use_media: bool,
    ) -> str:
        """Формирует текст превью поста в предложке вместе со служебной шапкой."""
        media_badge = (
            f"📎 Медиа: {media_count} шт. ({'вкл' if use_media else 'выкл — только текст'})"
            if has_media
            else "📎 Без медиа"
        )
        custom_emoji_cnt = AIService.count_custom_emojis(post.get("rewritten_text", ""))
        emoji_line = (
            f"\n💎 <i>Анимированных эмодзи в посте: {custom_emoji_cnt} шт. (оживут в канале)</i>"
            if custom_emoji_cnt > 0
            else ""
        )
        header = (
            f"📥 <b>Предложка #{post['id']}</b> | Источник: <b>{html.escape(post['source_title'])}</b>\n"
            f"<i>{media_badge}</i>{emoji_line}\n"
            f"{'─' * 22}\n\n"
        )
        return f"{header}{post['rewritten_text']}"

    async def _send_text_safe(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        # Для стандартного Bot API убираем обёртку <tg-emoji>, оставляя сам смайлик и всю HTML-вёрстку
        bot_ready_html = AIService.strip_custom_emoji_tags(text)
        safe_html = AIService.truncate_telegram_html(bot_ready_html, 4090)
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=safe_html,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            if "parse" in str(exc).lower() or "entit" in str(exc).lower():
                logger.warning("Фоллбэк отправки без HTML (%s): %s", chat_id, exc)
                plain = AIService.strip_html_tags(safe_html)[:4090]
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=plain,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            else:
                raise

    async def _send_single_media_safe(
        self,
        chat_id: int | str,
        item: dict[str, Any],
        caption: str | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        file = FSInputFile(item["path"])
        bot_ready_cap = AIService.strip_custom_emoji_tags(caption) if caption else None
        safe_caption = AIService.truncate_telegram_html(bot_ready_cap, 1024) if bot_ready_cap else None
        try:
            if item["type"] == "video":
                await self.bot.send_video(
                    chat_id=chat_id,
                    video=file,
                    caption=safe_caption,
                    parse_mode="HTML" if safe_caption else None,
                    reply_markup=reply_markup,
                    **self._video_kwargs(item),
                )
            else:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=file,
                    caption=safe_caption,
                    parse_mode="HTML" if safe_caption else None,
                    reply_markup=reply_markup,
                )
        except TelegramBadRequest as exc:
            if safe_caption and ("parse" in str(exc).lower() or "entit" in str(exc).lower()):
                logger.warning("Фоллбэк медиа-подписи без HTML (%s): %s", chat_id, exc)
                plain_cap = AIService.strip_html_tags(safe_caption)[:1024]
                if item["type"] == "video":
                    await self.bot.send_video(
                        chat_id=chat_id,
                        video=FSInputFile(item["path"]),
                        caption=plain_cap,
                        reply_markup=reply_markup,
                        **self._video_kwargs(item),
                    )
                else:
                    await self.bot.send_photo(
                        chat_id=chat_id,
                        photo=FSInputFile(item["path"]),
                        caption=plain_cap,
                        reply_markup=reply_markup,
                    )
            else:
                raise

    async def send_to_moderation(
        self, post_id: int, target_admin_id: int | None = None
    ) -> None:
        """Отправляет подготовленный пост в предложку админу(ам)."""
        post = await self.db.get_post(post_id)
        if not post:
            return

        media_items: list[dict[str, str]] = [
            m for m in post["media_items"] if Path(m["path"]).exists()
        ]
        has_media = len(media_items) > 0
        use_media = bool(post["use_media"]) and has_media
        is_freebie = AIService.has_freebie_tag(post["rewritten_text"])
        keyboard = build_moderation_keyboard(
            post_id, has_media, use_media, is_freebie=is_freebie
        )
        full_preview_text = AIService.balance_telegram_html(
            self.build_preview_text(post, len(media_items), has_media, use_media)
        )

        recipients = (
            [target_admin_id] if target_admin_id else self.config.admin_ids
        )

        for admin_id in recipients:
            try:
                if use_media and len(media_items) == 1 and len(full_preview_text) <= 1024:
                    await self._send_single_media_safe(
                        chat_id=admin_id,
                        item=media_items[0],
                        caption=full_preview_text,
                        reply_markup=keyboard,
                    )
                elif use_media and len(media_items) >= 1:
                    # Для альбомов (2+ файла) или длинного текста (>1024 симв.)
                    # сначала отправляем медиа, а следом текст с кнопками управления
                    if len(media_items) == 1:
                        await self._send_single_media_safe(
                            chat_id=admin_id,
                            item=media_items[0],
                            caption=None,
                        )
                        await self._send_text_safe(
                            chat_id=admin_id,
                            text=full_preview_text,
                            reply_markup=keyboard,
                        )
                    else:
                        media_group = self._build_media_group(media_items, caption=None)
                        sent_msgs = await self.bot.send_media_group(
                            chat_id=admin_id, media=media_group
                        )
                        await self._send_text_safe(
                            chat_id=admin_id,
                            text=full_preview_text,
                            reply_to_message_id=sent_msgs[0].message_id,
                            reply_markup=keyboard,
                        )
                else:
                    await self._send_text_safe(
                        chat_id=admin_id,
                        text=full_preview_text,
                        reply_markup=keyboard,
                    )
            except Exception as exc:
                logger.exception(
                    "Ошибка отправки превью поста #%s админу %s: %s",
                    post_id,
                    admin_id,
                    exc,
                )

    async def publish_to_channel(self, post_id: int) -> bool:
        """
        Публикует пост в целевой канал TARGET_CHANNEL.
        Если подключен Premium-публикатор (основа с Telegram Premium) — публикует через него
        с сохранением всех платных анимированных эмодзи (<tg-emoji emoji-id="...">).
        Иначе (или при ошибке прав) автоматически публикует через стандартного бота.
        """
        post = await self.db.get_post(post_id)
        if not post:
            return False

        text = AIService.balance_telegram_html(post["rewritten_text"].strip())
        media_items: list[dict[str, str]] = [
            m for m in post["media_items"] if Path(m["path"]).exists()
        ]
        use_media = bool(post["use_media"]) and len(media_items) > 0
        target = self.config.target_channel

        if not use_media and not text:
            logger.warning("Пост #%s не содержит ни медиа, ни текста — публикация отменена.", post_id)
            return False

        # 1. Попытка публикации через аккаунт с Telegram Premium (для живых анимированных <tg-emoji>)
        if self.premium_publisher is not None:
            try:
                if await self.premium_publisher.is_authorized():
                    pub_ok = await self.premium_publisher.publish_post(
                        target=target,
                        text=text,
                        media_items=media_items,
                        use_media=use_media,
                    )
                    if pub_ok:
                        logger.info("Пост #%s опубликован в канал через Premium-публикатор!", post_id)
                        await self.db.update_post_status(post_id, "published")
                        self.cleanup_post_media(post)
                        return True
            except Exception as exc:
                logger.warning(
                    "Не удалось опубликовать пост #%s через Premium-публикатор (%s), "
                    "выполняем резервную публикацию через бота...",
                    post_id,
                    exc,
                )

        # 2. Публикация через стандартного бота (Bot API)
        try:
            if not use_media:
                await self._send_text_safe(chat_id=target, text=text)
            elif len(media_items) == 1:
                item = media_items[0]
                if len(text) <= 1024:
                    await self._send_single_media_safe(
                        chat_id=target,
                        item=item,
                        caption=text or None,
                    )
                else:
                    # Если текст длиннее 1024 символов — отправляем медиа и следом текст
                    await self._send_single_media_safe(
                        chat_id=target,
                        item=item,
                        caption=None,
                    )
                    await self._send_text_safe(chat_id=target, text=text)
            else:
                # Публикация медиа-альбома (2–10 файлов)
                bot_ready_text = AIService.strip_custom_emoji_tags(text)
                caption_for_album = bot_ready_text if (bot_ready_text and len(bot_ready_text) <= 1024) else None
                media_group = self._build_media_group(
                    media_items, caption=caption_for_album
                )
                try:
                    await self.bot.send_media_group(chat_id=target, media=media_group)
                except TelegramBadRequest as exc:
                    if caption_for_album and ("parse" in str(exc).lower() or "entit" in str(exc).lower()):
                        plain_cap = AIService.strip_html_tags(caption_for_album)[:1024]
                        media_group_plain = self._build_media_group(
                            media_items, caption=plain_cap, use_html=False
                        )
                        await self.bot.send_media_group(chat_id=target, media=media_group_plain)
                    else:
                        raise
                if len(bot_ready_text) > 1024:
                    await self._send_text_safe(chat_id=target, text=text)

            await self.db.update_post_status(post_id, "published")
            self.cleanup_post_media(post)
            return True
        except Exception as exc:
            logger.exception("Ошибка публикации поста #%s в канал: %s", post_id, exc)
            return False

    @classmethod
    def _build_media_group(
        cls,
        media_items: list[dict[str, Any]],
        caption: str | None = None,
        use_html: bool = True,
    ) -> list[InputMediaPhoto | InputMediaVideo]:
        group: list[InputMediaPhoto | InputMediaVideo] = []
        for idx, item in enumerate(media_items[:10]):
            file = FSInputFile(item["path"])
            item_caption = caption if (idx == 0 and caption) else None
            parse_mode = ("HTML" if use_html else None) if item_caption else None
            if item["type"] == "video":
                group.append(
                    InputMediaVideo(
                        media=file,
                        caption=item_caption,
                        parse_mode=parse_mode,
                        **cls._video_kwargs(item),
                    )
                )
            else:
                group.append(
                    InputMediaPhoto(
                        media=file, caption=item_caption, parse_mode=parse_mode
                    )
                )
        return group

    @staticmethod
    def cleanup_post_media(post: dict[str, Any]) -> None:
        """Удаляет временные скачанные медиафайлы после публикации или отклонения."""
        for item in post.get("media_items", []):
            for key in ("path", "thumb_path"):
                path = item.get(key)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    @staticmethod
    def cleanup_stale_media_dir(media_dir: Path, max_age_hours: int = 72) -> int:
        """Удаляет старые временные медиафайлы старше max_age_hours часов (защита диска на сервере)."""
        if not media_dir.exists():
            return 0
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        for f in media_dir.iterdir():
            if f.is_file():
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        return removed
