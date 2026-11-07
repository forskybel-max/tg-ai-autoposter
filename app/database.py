import json
import os
from pathlib import Path
from typing import Any
import aiosqlite

from app.config import DEFAULT_STYLE_PROMPT


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    username_or_link TEXT NOT NULL,
                    is_private INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER NOT NULL,
                    source_msg_id INTEGER NOT NULL,
                    source_title TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    rewritten_text TEXT NOT NULL,
                    media_json TEXT NOT NULL DEFAULT '[]',
                    use_media INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_chat_id, source_msg_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            # Установка дефолтных настроек при первом запуске
            defaults = {
                "style_prompt": DEFAULT_STYLE_PROMPT,
                "channel_signature": "",
                "filter_ads": "1",
                "auto_publish": "0",
            }
            for k, v in defaults.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )

            # Обновляем старый дефолтный промпт, чтобы сохранялись полезные внешние ссылки
            await db.execute(
                """
                UPDATE settings SET value = ?
                WHERE key = 'style_prompt' AND value LIKE '%ПОЛНОСТЬЮ удали любые чужие @юзернеймы%'
                """,
                (DEFAULT_STYLE_PROMPT,),
            )

            # Автовосстановление списка каналов из INITIAL_SOURCES_JSON (для бесплатного облака без диска)
            initial_sources_raw = os.getenv("INITIAL_SOURCES_JSON", "").strip()
            if initial_sources_raw:
                try:
                    initial_list = json.loads(initial_sources_raw)
                    for src in initial_list:
                        await db.execute(
                            """
                            INSERT OR IGNORE INTO sources (chat_id, title, username_or_link, is_private, enabled)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                int(src["chat_id"]),
                                str(src["title"]),
                                str(src["username_or_link"]),
                                int(src.get("is_private", 0)),
                                int(src.get("enabled", 1)),
                            ),
                        )
                except Exception:
                    pass

            await db.commit()

    # --- Работа с источниками (каналами-донорами) ---

    async def add_source(
        self,
        chat_id: int,
        title: str,
        username_or_link: str,
        is_private: bool = False,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sources (chat_id, title, username_or_link, is_private, enabled)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username_or_link = excluded.username_or_link,
                    is_private = excluded.is_private,
                    enabled = 1
                """,
                (chat_id, title, username_or_link, int(is_private)),
            )
            await db.commit()

    async def get_sources(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        query = "SELECT chat_id, title, username_or_link, is_private, enabled FROM sources"
        if only_enabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at DESC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_source(self, chat_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id, title, username_or_link, is_private, enabled FROM sources WHERE chat_id = ?",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def toggle_source(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sources SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE chat_id = ?",
                (chat_id,),
            )
            await db.commit()
            async with db.execute(
                "SELECT enabled FROM sources WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row[0]) if row else False

    async def delete_source(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM sources WHERE chat_id = ?", (chat_id,))
            await db.commit()

    # --- Работа с постами и предложкой ---

    async def is_post_processed(self, source_chat_id: int, source_msg_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM posts WHERE source_chat_id = ? AND source_msg_id = ?",
                (source_chat_id, source_msg_id),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def get_recent_post_snippets(self, limit: int = 15) -> list[str]:
        """Возвращает начала последних постов для проверки смысловых дублей."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT rewritten_text FROM posts
                WHERE status IN ('pending', 'published') AND rewritten_text != ''
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [r[0][:200] for r in rows if r[0]]

    async def create_post(
        self,
        source_chat_id: int,
        source_msg_id: int,
        source_title: str,
        original_text: str,
        rewritten_text: str,
        media_items: list[dict[str, str]],
        status: str = "pending",
    ) -> int:
        media_json = json.dumps(media_items, ensure_ascii=False)
        use_media = 1 if media_items else 0
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO posts (
                    source_chat_id, source_msg_id, source_title,
                    original_text, rewritten_text, media_json, use_media, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_chat_id, source_msg_id) DO UPDATE SET
                    rewritten_text = excluded.rewritten_text,
                    media_json = excluded.media_json,
                    status = excluded.status
                RETURNING id
                """,
                (
                    source_chat_id,
                    source_msg_id,
                    source_title,
                    original_text,
                    rewritten_text,
                    media_json,
                    use_media,
                    status,
                ),
            )
            row = await cursor.fetchone()
            await db.commit()
            return int(row[0])

    async def get_post(self, post_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                data = dict(row)
                data["media_items"] = json.loads(data.get("media_json") or "[]")
                return data

    async def update_post_text(self, post_id: int, new_text: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE posts SET rewritten_text = ? WHERE id = ?",
                (new_text, post_id),
            )
            await db.commit()

    async def toggle_post_media(self, post_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE posts SET use_media = CASE WHEN use_media = 1 THEN 0 ELSE 1 END WHERE id = ?",
                (post_id,),
            )
            await db.commit()
            async with db.execute(
                "SELECT use_media FROM posts WHERE id = ?", (post_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row[0]) if row else False

    async def update_post_status(self, post_id: int, status: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE posts SET status = ? WHERE id = ?",
                (status, post_id),
            )
            await db.commit()

    # --- Настройки ---

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()
