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
                "custom_emoji_palette": "",
                "emoji_list_bullet": "",
                "emoji_headline": "",
                "publisher_string_session": "",
            }
            for k, v in defaults.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v),
                )

            # Обновляем старый дефолтный промпт на бережное сохранение структуры 1-в-1, фильтр тематики и удаление подписей доноров
            async with db.execute("SELECT value FROM settings WHERE key = 'style_prompt'") as cur:
                row = await cur.fetchone()
                if row and (
                    "опытный главный редактор" in row[0]
                    or "БЕРЕЖНАЯ редактура, а не тотальное переписывание" in row[0]
                    or "шеф-редактор современного Telegram-канала" in row[0]
                    or "СТРОГО СОХРАНЯЯ оригинальную структуру, абзацы, списки и форматирование исходного поста" in row[0]
                ):
                    await db.execute(
                        "UPDATE settings SET value = ? WHERE key = 'style_prompt'",
                        (DEFAULT_STYLE_PROMPT,),
                    )

            # Базовый список из 19 каналов-доноров + дополнение из INITIAL_SOURCES_JSON
            default_sources = [
                {"chat_id": -1001319248631, "title": "Бэкдор", "username_or_link": "@whackdoor", "is_private": 0, "enabled": 1},
                {"chat_id": -1003469689117, "title": "要 C 一下吗？", "username_or_link": "@pk_fns", "is_private": 0, "enabled": 1},
                {"chat_id": -1004418840629, "title": "老王技术资讯频道", "username_or_link": "@eooce_channel", "is_private": 0, "enabled": 1},
                {"chat_id": -1002546853359, "title": "Никита Велс | AI", "username_or_link": "https://telegram.me/+frtm7Hh88ss1NDBh", "is_private": 1, "enabled": 1},
                {"chat_id": -1002002176741, "title": "Бывший Скамер печатает..", "username_or_link": "https://t.me/+Akjabw9v_cI4Y2Ni", "is_private": 1, "enabled": 1},
                {"chat_id": -1001555418015, "title": "Вайб-кодинг", "username_or_link": "@vibecoding_tg", "is_private": 0, "enabled": 1},
                {"chat_id": -1002824239254, "title": "AI для своих", "username_or_link": "@ai4svoi", "is_private": 0, "enabled": 1},
                {"chat_id": -1002331256012, "title": "Матрица", "username_or_link": "@matrix_media_1", "is_private": 0, "enabled": 1},
                {"chat_id": -1002270345261, "title": "Александр Бубнов [ex. ИИШНЫЙ]", "username_or_link": "@aishnyy", "is_private": 0, "enabled": 1},
                {"chat_id": -1002011947245, "title": "Алексей Орфеев про бизнес и контент с помощью нейросетей и автоматизации", "username_or_link": "@AlekseyOrfeev", "is_private": 0, "enabled": 1},
                {"chat_id": -1002966261035, "title": "Vọc VN", "username_or_link": "@vocvnchanel", "is_private": 0, "enabled": 1},
                {"chat_id": -1002041340502, "title": "SGX", "username_or_link": "@crypto_sgx", "is_private": 0, "enabled": 1},
                {"chat_id": -1003799671340, "title": "выф", "username_or_link": "@wod304", "is_private": 0, "enabled": 1},
                {"chat_id": -1002857314778, "title": "НЕЙРО ПРОФИТ", "username_or_link": "@neuroprft", "is_private": 0, "enabled": 1},
                {"chat_id": -1002671714459, "title": "PK个人频道", "username_or_link": "@pk_oa", "is_private": 0, "enabled": 1},
                {"chat_id": -1001966291562, "title": "КиберТопор", "username_or_link": "https://t.me/cybers", "is_private": 1, "enabled": 1},
                {"chat_id": -1001758234692, "title": "XOR", "username_or_link": "@xor_journal", "is_private": 0, "enabled": 1},
                {"chat_id": -1001333809979, "title": "CodeCamp", "username_or_link": "https://t.me/codecamp", "is_private": 1, "enabled": 1},
                {"chat_id": -1001104344271, "title": "Не баг, а фича", "username_or_link": "@bugnotfeature", "is_private": 0, "enabled": 1},
            ]
            combined_sources = list(default_sources)
            initial_sources_raw = os.getenv("INITIAL_SOURCES_JSON", "").strip()
            if initial_sources_raw:
                try:
                    initial_list = json.loads(initial_sources_raw)
                    if isinstance(initial_list, list):
                        combined_sources.extend(initial_list)
                except Exception:
                    pass

            for src in combined_sources:
                try:
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

    async def are_any_posts_processed(
        self, source_chat_id: int, msg_ids: list[int]
    ) -> bool:
        if not msg_ids:
            return False
        placeholders = ",".join("?" for _ in msg_ids)
        query = (
            f"SELECT 1 FROM posts WHERE source_chat_id = ? "
            f"AND source_msg_id IN ({placeholders}) LIMIT 1"
        )
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, (source_chat_id, *msg_ids)) as cursor:
                return await cursor.fetchone() is not None

    async def get_recent_post_snippets(self, limit: int = 35) -> list[str]:
        """Возвращает чистые текстовые сниппеты последних постов (включая отклонённые) для проверки смысловых дублей."""
        import re as _re

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT COALESCE(NULLIF(rewritten_text, ''), original_text) FROM posts
                WHERE status IN ('pending', 'published', 'rejected')
                  AND (rewritten_text != '' OR original_text != '')
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                snippets: list[str] = []
                for r in rows:
                    if not r or not r[0]:
                        continue
                    plain = _re.sub(r"</?[a-zA-Z0-9_-]+(?:\s+[^>]*)?>", "", str(r[0]))
                    plain = _re.sub(r"\s+", " ", plain).strip()
                    if plain:
                        snippets.append(plain[:220])
                return snippets

    async def get_recent_posts_for_dedup(self, limit: int = 80) -> list[tuple[str, str]]:
        """Возвращает (original_text, rewritten_text) последних постов для быстрой программной дедупликации."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT original_text, rewritten_text FROM posts
                WHERE (original_text != '' OR rewritten_text != '')
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [(str(r[0] or ""), str(r[1] or "")) for r in rows]

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
