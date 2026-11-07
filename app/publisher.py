import html
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)

from app.config import Config
from app.database import Database

logger = logging.getLogger(__name__)


def build_moderation_keyboard(
    post_id: int, has_media: bool, use_media: bool
) -> InlineKeyboardMarkup:
    """Формирует инлайн-клавиатуру для поста в предложке."""
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
    ]

    extra_row = [
        InlineKeyboardButton(
            text="👁 Исходник",
            callback_data=f"pub:orig:{post_id}",
        )
    ]
    if has_media:
        media_label = "🖼 Убрать медиа" if use_media else "🖼 Вернуть медиа"
        extra_row.append(
            InlineKeyboardButton(
                text=media_label,
                callback_data=f"pub:toggle_media:{post_id}",
            )
        )
    extra_row.append(
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"pub:reject:{post_id}",
        )
    )
    rows.append(extra_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Publisher:
    def __init__(self, bot: Bot, config: Config, db: Database):
        self.bot = bot
        self.config = config
        self.db = db

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
        keyboard = build_moderation_keyboard(post_id, has_media, use_media)

        media_badge = (
            f"📎 Медиа: {len(media_items)} шт. ({'вкл' if use_media else 'выкл — только текст'})"
            if has_media
            else "📎 Без медиа"
        )
        header = (
            f"📥 <b>Предложка #{post_id}</b> | Источник: <b>{html.escape(post['source_title'])}</b>\n"
            f"<i>{media_badge}</i>\n"
            f"{'─' * 22}\n\n"
        )
        full_preview_text = f"{header}{post['rewritten_text']}"

        recipients = (
            [target_admin_id] if target_admin_id else self.config.admin_ids
        )

        for admin_id in recipients:
            try:
                if use_media and len(media_items) == 1 and len(full_preview_text) <= 1024:
                    item = media_items[0]
                    file = FSInputFile(item["path"])
                    if item["type"] == "video":
                        await self.bot.send_video(
                            chat_id=admin_id,
                            video=file,
                            caption=full_preview_text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                            **self._video_kwargs(item),
                        )
                    else:
                        await self.bot.send_photo(
                            chat_id=admin_id,
                            photo=file,
                            caption=full_preview_text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                elif use_media and len(media_items) >= 1:
                    # Для альбомов (2+ файла) или длинного текста (>1024 симв.)
                    # сначала отправляем медиа, а следом текст с кнопками управления
                    media_group = self._build_media_group(media_items, caption=None)
                    sent_msgs = await self.bot.send_media_group(
                        chat_id=admin_id, media=media_group
                    )
                    await self.bot.send_message(
                        chat_id=admin_id,
                        text=full_preview_text[:4090],
                        parse_mode="HTML",
                        reply_to_message_id=sent_msgs[0].message_id,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                else:
                    await self.bot.send_message(
                        chat_id=admin_id,
                        text=full_preview_text[:4090],
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
            except Exception as exc:
                logger.exception(
                    "Ошибка отправки превью поста #%s админу %s: %s",
                    post_id,
                    admin_id,
                    exc,
                )

    async def publish_to_channel(self, post_id: int) -> bool:
        """Публикует пост в целевой канал TARGET_CHANNEL."""
        post = await self.db.get_post(post_id)
        if not post:
            return False

        text = post["rewritten_text"].strip()
        media_items: list[dict[str, str]] = [
            m for m in post["media_items"] if Path(m["path"]).exists()
        ]
        use_media = bool(post["use_media"]) and len(media_items) > 0
        target = self.config.target_channel

        try:
            if not use_media:
                if not text:
                    logger.warning("Пост #%s не содержит ни медиа, ни текста — публикация отменена.", post_id)
                    return False
                await self.bot.send_message(
                    chat_id=target,
                    text=text[:4096],
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            elif len(media_items) == 1:
                item = media_items[0]
                file = FSInputFile(item["path"])
                if len(text) <= 1024:
                    if item["type"] == "video":
                        await self.bot.send_video(
                            chat_id=target,
                            video=file,
                            caption=text or None,
                            parse_mode="HTML",
                            **self._video_kwargs(item),
                        )
                    else:
                        await self.bot.send_photo(
                            chat_id=target,
                            photo=file,
                            caption=text or None,
                            parse_mode="HTML",
                        )
                else:
                    # Если текст длиннее 1024 символов — отправляем медиа и следом текст
                    if item["type"] == "video":
                        await self.bot.send_video(
                            chat_id=target,
                            video=file,
                            **self._video_kwargs(item),
                        )
                    else:
                        await self.bot.send_photo(chat_id=target, photo=file)
                    await self.bot.send_message(
                        chat_id=target,
                        text=text[:4096],
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
            else:
                # Публикация медиа-альбома (2–10 файлов)
                caption_for_album = text if len(text) <= 1024 else None
                media_group = self._build_media_group(
                    media_items, caption=caption_for_album
                )
                await self.bot.send_media_group(chat_id=target, media=media_group)
                if len(text) > 1024:
                    await self.bot.send_message(
                        chat_id=target,
                        text=text[:4096],
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

            await self.db.update_post_status(post_id, "published")
            self.cleanup_post_media(post)
            return True
        except Exception as exc:
            logger.exception("Ошибка публикации поста #%s в канал: %s", post_id, exc)
            return False

    @classmethod
    def _build_media_group(
        cls, media_items: list[dict[str, Any]], caption: str | None = None
    ) -> list[InputMediaPhoto | InputMediaVideo]:
        group: list[InputMediaPhoto | InputMediaVideo] = []
        for idx, item in enumerate(media_items[:10]):
            file = FSInputFile(item["path"])
            item_caption = caption if (idx == 0 and caption) else None
            parse_mode = "HTML" if item_caption else None
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
