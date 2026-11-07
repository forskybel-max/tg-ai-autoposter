import asyncio
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
import aiohttp

from app.config import Config
from app.database import Database

logger = logging.getLogger(__name__)


@dataclass
class AIResult:
    is_rejected: bool
    reject_reason: str
    rewritten_text: str


class AIService:
    """Универсальный сервис работы с ИИ (Gemini, OpenAI, OpenRouter, DeepSeek, Groq)."""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._discovered_gemini_fallbacks: list[str] | None = None

    async def process_post(
        self,
        original_text: str,
        has_media: bool = False,
        extra_instruction: str = "",
    ) -> AIResult:
        """
        Проверяет пост на рекламу/дубли, очищает от чужих ссылок/@юзернеймов
        и переписывает в стиле канала.
        """
        style_prompt = await self.db.get_setting("style_prompt")
        signature = await self.db.get_setting("channel_signature")
        filter_ads = (await self.db.get_setting("filter_ads", "1")) == "1"
        recent_snippets = await self.db.get_recent_post_snippets(limit=12)

        if not original_text or not original_text.strip():
            final_caption = signature.strip() if signature.strip() else ""
            return AIResult(
                is_rejected=False,
                reject_reason="",
                rewritten_text=final_caption,
            )

        recent_context = ""
        if recent_snippets:
            formatted = "\n".join(f"- {s}" for s in recent_snippets)
            recent_context = (
                "\nСписок недавно обработанных постов (для проверки смысловых дублей):\n"
                f"{formatted}\n"
            )

        ad_rule = (
            "Если исходный пост является прямой рекламой, продажей курсов, казино, ставками, "
            "розыгрышем за подписку, призывом перейти в другой бот/канал без полезной сути, "
            "или полным смысловым дублем уже вышедшего поста из списка выше — верни "
            '"is_rejected": true и укажи краткую причину в "reject_reason".'
            if filter_ads
            else 'Всегда возвращай "is_rejected": false (фильтр рекламы отключен).'
        )

        length_rule = (
            "Поскольку к посту прикреплено фото/видео, длина итогового текста "
            "НЕ должна превышать 850 символов."
            if has_media
            else "Длина итогового текста не должна превышать 3000 символов."
        )

        extra_block = (
            f"\nДополнительное пожелание редактора к этому варианту: {extra_instruction}\n"
            if extra_instruction
            else ""
        )

        tech_rules = (
            "Обязательные технические правила ссылок и разметки:\n"
            "- ОБЯЗАТЕЛЬНО СОХРАНЯЙ полезные внешние ссылки по смыслу поста (на обсуждаемые сервисы, сайты, "
            'GitHub, инструменты, статьи, видео и т.д.), встраивая их через HTML-тег <a href="https://...">текст</a>.\n'
            "- УДАЛЯЙ ТОЛЬКО ссылки на чужие Telegram-каналы (t.me/...), чужие @юзернеймы и призывы подписаться.\n"
            "- Используй ТОЛЬКО HTML-теги Telegram: <b>, <i>, <u>, <s>, <code>, <blockquote>, <a href=\"...\"> (без Markdown)."
        )

        system_prompt = (
            f"{style_prompt}\n\n"
            f"{tech_rules}\n\n"
            f"{length_rule}\n"
            f"{ad_rule}\n"
            f"{recent_context}"
            f"{extra_block}\n"
            "Ответь СТРОГО валидным JSON-объектом без обёрток ```json и лишнего текста:\n"
            "{\n"
            '  "is_rejected": false,\n'
            '  "reject_reason": "",\n'
            '  "rewritten_text": "Готовый переписанный текст с HTML-тегами <b>, <i>, <a href=...>"\n'
            "}"
        )

        raw_response = await self._call_llm(
            system_prompt=system_prompt,
            user_text=f"Исходный текст поста:\n\n{original_text}",
        )

        parsed = self._parse_json_response(raw_response, fallback_text=original_text)
        if parsed.is_rejected:
            return parsed

        cleaned_text = self._sanitize_html_and_links(parsed.rewritten_text)
        if signature and signature.strip():
            cleaned_text = f"{cleaned_text}\n\n{signature.strip()}".strip()

        return AIResult(
            is_rejected=False,
            reject_reason="",
            rewritten_text=cleaned_text,
        )

    def _resolve_proxy(self) -> str | None:
        if self.config.ai_proxy:
            return self.config.ai_proxy
        sys_proxies = urllib.request.getproxies()
        proxy = sys_proxies.get("https") or sys_proxies.get("http")
        if proxy and "://" not in proxy:
            proxy = f"http://{proxy}"
        return proxy

    async def _discover_gemini_models(self, session: aiohttp.ClientSession, proxy: str | None) -> list[str]:
        if self._discovered_gemini_fallbacks is not None:
            return self._discovered_gemini_fallbacks
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.config.ai_api_key}"
        try:
            async with session.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                data = await resp.json()
                models_raw = data.get("models") or []
                candidates: list[str] = []
                for m in models_raw:
                    methods = m.get("supportedGenerationMethods") or []
                    name = m.get("name", "").removeprefix("models/")
                    if "generateContent" in methods and "tts" not in name and "image" not in name and "embedding" not in name:
                        if "flash" in name:
                            candidates.append(name)
                logger.info("Доступные модели Gemini Flash на ключе: %s", candidates)
                # Приоритет быстрым flash-lite / flash моделям
                candidates.sort(key=lambda x: (0 if "lite" in x else 1, x))
                self._discovered_gemini_fallbacks = candidates
                return candidates
        except Exception as exc:
            logger.warning("Не удалось получить список моделей Gemini: %s", exc)
            return []

    async def _call_llm(self, system_prompt: str, user_text: str) -> str:
        if self.config.ai_provider == "gemini" and "googleapis.com" in self.config.ai_base_url:
            return await self._call_gemini_native(system_prompt, user_text)
        return await self._call_openai_compatible(system_prompt, user_text)

    async def _call_gemini_single(
        self,
        session: aiohttp.ClientSession,
        model: str,
        system_prompt: str,
        user_text: str,
        proxy: str | None,
        use_low_thinking: bool = True,
        timeout_sec: float = 35.0,
    ) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.config.ai_api_key}"
        )
        gen_cfg: dict = {"temperature": 0.6}
        if use_low_thinking and ("3.8" in model or "3.7" in model):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": gen_cfg,
        }

        t0 = time.monotonic()
        async with session.post(
            url,
            json=payload,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=timeout_sec, connect=10.0),
        ) as resp:
            body_text = await resp.text()
            elapsed = time.monotonic() - t0
            if resp.status != 200:
                raise RuntimeError(f"Gemini ({model}) HTTP {resp.status} [{elapsed:.1f}s]: {body_text[:350]}")

            data = json.loads(body_text)
            candidates = data.get("candidates") or []
            if not candidates:
                raise RuntimeError(f"Пустой ответ от Gemini ({model}): {body_text[:250]}")
            parts = candidates[0].get("content", {}).get("parts") or []
            texts = [p["text"] for p in parts if "text" in p and not p.get("thought", False)]
            if not texts:
                texts = [p["text"] for p in parts if "text" in p]
            logger.info("Gemini (%s) ответил за %.2f сек", model, elapsed)
            return "\n".join(texts).strip()

    async def _call_gemini_native(self, system_prompt: str, user_text: str) -> str:
        """Вызов нативного REST API Google Gemini с автоподбором быстрой модели при таймауте."""
        proxy = self._resolve_proxy()
        primary_model = self.config.ai_model.removeprefix("models/")

        async with aiohttp.ClientSession() as session:
            try:
                return await self._call_gemini_single(
                    session, primary_model, system_prompt, user_text, proxy, use_low_thinking=True, timeout_sec=25.0
                )
            except (asyncio.TimeoutError, Exception) as first_err:
                logger.warning("Первичная модель %s не ответила быстро (%s: %s), ищем резервную Flash-модель...", primary_model, type(first_err).__name__, first_err)
                # Если ошибка 400 из-за thinkingConfig — пробуем ту же модель без thinkingConfig
                if "thinking" in str(first_err).lower():
                    try:
                        return await self._call_gemini_single(
                            session, primary_model, system_prompt, user_text, proxy, use_low_thinking=False, timeout_sec=30.0
                        )
                    except Exception:
                        pass

                fallbacks = await self._discover_gemini_models(session, proxy)
                last_err: Exception = first_err
                for fb_model in fallbacks:
                    if fb_model == primary_model:
                        continue
                    try:
                        res = await self._call_gemini_single(
                            session, fb_model, system_prompt, user_text, proxy, use_low_thinking=True, timeout_sec=25.0
                        )
                        logger.info("Переключаем рабочую модель по умолчанию на быструю: %s", fb_model)
                        self.config.ai_model = fb_model
                        return res
                    except Exception as fb_err:
                        logger.warning("Резервная модель %s не сработала: %s: %s", fb_model, type(fb_err).__name__, fb_err)
                        last_err = fb_err

                raise RuntimeError(f"{type(last_err).__name__}: {last_err}") from last_err

    async def _call_openai_compatible(self, system_prompt: str, user_text: str) -> str:
        url = f"{self.config.ai_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.ai_api_key}",
            "Content-Type": "application/json",
        }
        proxy = self._resolve_proxy()

        models_to_try = [self.config.ai_model]
        if self.config.ai_provider == "openrouter" and (
            self.config.ai_model.endswith(":free")
            or self.config.ai_model == "openrouter/free"
        ):
            for fb in (
                "google/gemma-4-31b-it:free",
                "google/gemma-4-26b-a4b-it:free",
                "nvidia/nemotron-3.5-lightning:free",
                "openrouter/free",
            ):
                if fb not in models_to_try:
                    models_to_try.append(fb)

        last_err = ""
        async with aiohttp.ClientSession() as session:
            for model_name in models_to_try:
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text},
                    ],
                    "temperature": 0.6,
                }
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=40.0, connect=12.0),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        data = json.loads(body)
                        choices = data.get("choices") or []
                        if choices and choices[0].get("message", {}).get("content"):
                            return choices[0]["message"]["content"].strip()
                    last_err = f"HTTP {resp.status} ({model_name}): {body[:350]}"
                    logger.warning("Попытка модели %s не удалась: %s", model_name, last_err)
                    if resp.status in (401, 403):
                        break

        raise RuntimeError(last_err)

    @staticmethod
    def _parse_json_response(raw: str, fallback_text: str) -> AIResult:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            obj = json.loads(cleaned)
            return AIResult(
                is_rejected=bool(obj.get("is_rejected", False)),
                reject_reason=str(obj.get("reject_reason", "")),
                rewritten_text=str(obj.get("rewritten_text", "")).strip(),
            )
        except Exception:
            logger.warning("Не удалось распарсить JSON от ИИ, используем текст напрямую")
            return AIResult(
                is_rejected=False,
                reject_reason="",
                rewritten_text=cleaned or fallback_text,
            )

    @staticmethod
    def _sanitize_html_and_links(text: str) -> str:
        # 1. Если ИИ случайно написал ссылку в Markdown [текст](https://...), конвертируем в HTML <a>
        text = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            r'<a href="\2">\1</a>',
            text,
        )

        # 2. Аккуратно убираем HTML-ссылки, ведущие на чужие t.me / telegram.me каналы
        text = re.sub(
            r'<a\s+href=["\'](?:https?://)?(?:t\.me|telegram\.me)/[^"\']*["\']\s*>.*?</a>',
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # 3. Убираем голые ссылки t.me/... и чужие @юзернеймы (но НЕ трогаем email вида user@mail.com)
        text = re.sub(r"https?://(?:t\.me|telegram\.me)/\S+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:t\.me|telegram\.me)/\S+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?<![a-zA-Z0-9._%+-])@[a-zA-Z0-9_]{3,32}\b", "", text)

        # 4. Конвертируем частые теги от нейросетей в поддерживаемые Telegram
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*p(?:\s+[^>]*)?\s*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

        allowed_pattern = re.compile(
            r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)\b[^>]*>"
            r"|<a\s+href=[\"']https?://[^\"']+[\"'][^>]*>|</a>",
            flags=re.IGNORECASE,
        )
        preserved: list[str] = []

        def _stash(match: re.Match) -> str:
            tag_str = match.group(0)
            # Нормализуем <a href="..." target="_blank"> -> <a href="..."> для строгого парсера Telegram
            a_match = re.match(r'<a\s+href=(["\']https?://[^"\']+["\'])[^>]*>', tag_str, flags=re.IGNORECASE)
            if a_match:
                tag_str = f"<a href={a_match.group(1)}>"
            idx = len(preserved)
            preserved.append(tag_str)
            return f"\x00TAG{idx}\x00"

        stashed = allowed_pattern.sub(_stash, text)
        stashed = re.sub(r"</?[a-zA-Z][a-zA-Z0-9_-]*(?:\s+[^>]*)?>", "", stashed)
        stashed = re.sub(r"&(?!(?:lt|gt|amp|quot|#\d+);)", "&amp;", stashed)
        stashed = stashed.replace("<", "&lt;").replace(">", "&gt;")

        for idx, tag in enumerate(preserved):
            stashed = stashed.replace(f"\x00TAG{idx}\x00", tag)

        stashed = re.sub(r"\n{3,}", "\n\n", stashed)
        return stashed.strip()
