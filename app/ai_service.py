import json
import logging
import re
import urllib.request
from dataclasses import dataclass
import httpx

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

        system_prompt = (
            f"{style_prompt}\n\n"
            f"{length_rule}\n"
            f"{ad_rule}\n"
            f"{recent_context}"
            f"{extra_block}\n"
            "Ответь СТРОГО валидным JSON-объектом без обёрток ```json и лишнего текста:\n"
            "{\n"
            '  "is_rejected": false,\n'
            '  "reject_reason": "",\n'
            '  "rewritten_text": "Готовый переписанный текст с HTML-тегами <b>, <i>"\n'
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
        """Возвращает прокси из .env или системный прокси Windows (если включен)."""
        if self.config.ai_proxy:
            return self.config.ai_proxy
        sys_proxies = urllib.request.getproxies()
        proxy = sys_proxies.get("https") or sys_proxies.get("http")
        if proxy and "://" not in proxy:
            proxy = f"http://{proxy}"
        return proxy

    def _build_http_client(self) -> httpx.AsyncClient:
        proxy = self._resolve_proxy()
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=20.0, pool=15.0)
        if proxy:
            return httpx.AsyncClient(timeout=timeout, proxy=proxy, http2=False)
        return httpx.AsyncClient(timeout=timeout, http2=False)

    async def _call_llm(self, system_prompt: str, user_text: str) -> str:
        if self.config.ai_provider == "gemini" and "googleapis.com" in self.config.ai_base_url:
            return await self._call_gemini_native(system_prompt, user_text)
        return await self._call_openai_compatible(system_prompt, user_text)

    async def _call_gemini_native(self, system_prompt: str, user_text: str) -> str:
        """Потоковый вызов нативного REST API Google Gemini (не зависает на VPN при генерации)."""
        model = self.config.ai_model.removeprefix("models/")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:streamGenerateContent?alt=sse&key={self.config.ai_api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": 0.6,
                "responseMimeType": "application/json",
            },
        }

        collected_chunks: list[str] = []
        async with self._build_http_client() as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    err_bytes = await response.aread()
                    body_preview = err_bytes.decode("utf-8", errors="replace")[:400]
                    logger.error("Ошибка ответа Gemini (%s): %s", response.status_code, body_preview)
                    raise RuntimeError(f"Gemini HTTP {response.status_code}: {body_preview}")

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw_json = line[5:].strip()
                    if not raw_json or raw_json == "[DONE]":
                        continue
                    try:
                        chunk_data = json.loads(raw_json)
                        for cand in chunk_data.get("candidates") or []:
                            for part in cand.get("content", {}).get("parts") or []:
                                if "text" in part and not part.get("thought", False):
                                    collected_chunks.append(part["text"])
                    except Exception:
                        pass

        result = "".join(collected_chunks).strip()
        if not result:
            raise RuntimeError("Пустой ответ от Gemini API")
        return result

    async def _call_openai_compatible(self, system_prompt: str, user_text: str) -> str:
        url = f"{self.config.ai_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.ai_api_key}",
            "Content-Type": "application/json",
        }

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
        async with self._build_http_client() as client:
            for model_name in models_to_try:
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text},
                    ],
                    "temperature": 0.6,
                }
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices") or []
                    if choices and choices[0].get("message", {}).get("content"):
                        return choices[0]["message"]["content"].strip()
                last_err = f"HTTP {response.status_code} ({model_name}): {response.text[:350]}"
                logger.warning("Попытка модели %s не удалась: %s", model_name, last_err)
                if response.status_code in (401, 403):
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
        """
        Страховочная зачистка после ИИ:
        1. Удаляет забытые @username и ссылки на чужие t.me/...
        2. Конвертирует <br>, <p>, **жирный** в поддерживаемый Telegram HTML.
        3. Экранирует неподдерживаемые теги и одиночные символы < и &, чтобы Telegram не выдал ошибку парсинга.
        """
        text = re.sub(r"https?://(?:t\.me|telegram\.me)/\S+", "", text)
        text = re.sub(r"\b(?:t\.me|telegram\.me)/\S+", "", text)
        text = re.sub(r"(?<!\w)@[a-zA-Z0-9_]{3,32}\b", "", text)

        # Замена частых HTML/Markdown конструкций от нейросетей
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*p(?:\s+[^>]*)?\s*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

        # Защищаем разрешенные теги Telegram временными маркерами, экранируем всё остальное и возвращаем теги
        allowed_pattern = re.compile(
            r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)\b[^>]*>"
            r"|<a\s+href=[\"'][^\"']+[\"']\s*>|</a>",
            flags=re.IGNORECASE,
        )
        preserved: list[str] = []

        def _stash(match: re.Match) -> str:
            idx = len(preserved)
            preserved.append(match.group(0))
            return f"\x00TAG{idx}\x00"

        stashed = allowed_pattern.sub(_stash, text)
        # Удаляем прочие случайные HTML-теги (<h1>, <div>, <ul>, <li> и т.п.)
        stashed = re.sub(r"</?[a-zA-Z][a-zA-Z0-9_-]*(?:\s+[^>]*)?>", "", stashed)
        # Экранируем голые &, <, >
        stashed = re.sub(r"&(?!(?:lt|gt|amp|quot|#\d+);)", "&amp;", stashed)
        stashed = stashed.replace("<", "&lt;").replace(">", "&gt;")

        for idx, tag in enumerate(preserved):
            stashed = stashed.replace(f"\x00TAG{idx}\x00", tag)

        stashed = re.sub(r"\n{3,}", "\n\n", stashed)
        return stashed.strip()
