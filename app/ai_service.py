import asyncio
import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
import aiohttp

from app.config import Config, FREEBIE_BADGE
from app.database import Database

logger = logging.getLogger(__name__)


@dataclass
class AIResult:
    is_rejected: bool
    reject_reason: str
    rewritten_text: str
    is_freebie: bool = False


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
            "Если исходный пост является платной рекламой чужого канала/курса, казино, ставками, "
            "розыгрышем за подписку на спонсоров, призывом перейти в чужой ТГ-бот/канал без полезной сути, "
            "или почти дословным смысловым дублем того же самого события/поста без новых деталей из списка выше — верни "
            '"is_rejected": true и укажи краткую причину в "reject_reason".\n'
            "ВАЖНО — НЕ ОТКЛОНЯЙ полезный контент (ставь \"is_rejected\": false):\n"
            "- обзоры, новости и анонсы новых нейросетей, ИИ-моделей, сервисов, приложений, GitHub-репозиториев и лайфхаков "
            "(даже если в посте есть ссылка на сайт/сервис, упоминание бесплатного тарифа или бота-сервиса);\n"
            "- полезные бесплатные раздачи (игры в Steam/Epic Games, бесплатные ИИ-инструменты, промокоды на 100% скидку, "
            "бесплатные программы/ассеты без подписки на ТГ-спонсоров) — для них ставь \"is_rejected\": false и \"is_freebie\": true!"
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
            "Обязательные правила бережной редактуры, оформления и рубрик:\n"
            "1. НЕ переписывай сильно там, где исходный текст уже написан коротко и по делу! Делай только лёгкий, естественный рерайт "
            "без отсебятины, без «воды» и без пафосных клише.\n"
            "2. СОХРАНЯЙ оригинальное форматирование и визуальную структуру: если в исходнике есть цитаты "
            "(<blockquote>...</blockquote> или <blockquote expandable>...</blockquote>), списки, код (<code>, <pre>), "
            "спойлеры (<tg-spoiler>) или жирные акценты — обязательно перенеси эти же теги в итоговый текст!\n"
            "3. ОБЯЗАТЕЛЬНО СОХРАНЯЙ полезные внешние ссылки по смыслу поста (на сервисы, сайты, GitHub, раздачи, инструменты, статьи) "
            'через HTML-тег <a href="https://...">текст</a>.\n'
            "4. УДАЛЯЙ ТОЛЬКО ссылки на чужие Telegram-каналы (t.me/...), чужие @юзернеймы и призывы подписаться.\n"
            "5. РУБРИКА ХАЛЯВА: если в посте речь идет о халяве (бесплатная раздача игры/программы/ресурсов, полностью бесплатный доступ "
            "к крутому платному инструменту/ИИ, промокод на бесплатную подписку, 100% скидка и т.п.) — поставь в JSON поле "
            '"is_freebie": true (иначе false). Сам хэштег #халява внутрь текста НЕ пиши — система добавит его после заголовка автоматически!\n'
            "6. Используй ТОЛЬКО HTML-теги Telegram: <b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>, <blockquote expandable>, "
            "<tg-spoiler>, <a href=\"...\"> (никакого Markdown **текст** или > цитата)."
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
            '  "is_freebie": false,\n'
            '  "rewritten_text": "Бережно отредактированный текст с сохранением <blockquote>, <b>, <a href=...>"\n'
            "}"
        )

        raw_response = await self._call_llm(
            system_prompt=system_prompt,
            user_text=f"Исходный текст поста (с HTML-разметкой оригинала):\n\n{original_text}",
        )

        parsed = self._parse_json_response(raw_response, fallback_text=original_text)
        if parsed.is_rejected:
            return parsed

        cleaned_text = self._sanitize_html_and_links(parsed.rewritten_text)
        if parsed.is_freebie or self.has_freebie_tag(cleaned_text):
            cleaned_text = self.set_freebie_tag(cleaned_text, enabled=True)

        if signature and signature.strip():
            cleaned_text = f"{cleaned_text}\n\n{signature.strip()}".strip()

        return AIResult(
            is_rejected=False,
            reject_reason="",
            rewritten_text=cleaned_text,
            is_freebie=parsed.is_freebie or self.has_freebie_tag(cleaned_text),
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

                # Приоритет проверенным быстрым стабильным моделям без deperecated 2.5 и медленных preview
                def _model_rank(name: str) -> tuple[int, str]:
                    if name == "gemini-3.5-flash-lite":
                        return (0, name)
                    if name == "gemini-3.1-flash-lite":
                        return (1, name)
                    if name == "gemini-3.5-flash":
                        return (2, name)
                    if name in ("gemini-flash-lite-latest", "gemini-flash-latest"):
                        return (3, name)
                    if "2.5" in name:
                        return (9, name)
                    if "preview" in name or "3.8" in name or "3.7" in name:
                        return (6, name)
                    if "lite" in name:
                        return (4, name)
                    return (5, name)

                candidates.sort(key=_model_rank)
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
        use_json_schema: bool = True,
        timeout_sec: float = 35.0,
    ) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.config.ai_api_key}"
        )
        gen_cfg: dict = {"temperature": 0.3}
        if use_json_schema:
            gen_cfg["responseMimeType"] = "application/json"
            gen_cfg["responseSchema"] = {
                "type": "OBJECT",
                "properties": {
                    "is_rejected": {"type": "BOOLEAN"},
                    "reject_reason": {"type": "STRING"},
                    "is_freebie": {"type": "BOOLEAN"},
                    "rewritten_text": {"type": "STRING"},
                },
                "required": [
                    "is_rejected",
                    "reject_reason",
                    "is_freebie",
                    "rewritten_text",
                ],
            }
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
                    session, primary_model, system_prompt, user_text, proxy, use_low_thinking=True, use_json_schema=True, timeout_sec=25.0
                )
            except (asyncio.TimeoutError, Exception) as first_err:
                logger.warning("Первичная модель %s не ответила быстро (%s: %s), ищем резервную Flash-модель...", primary_model, type(first_err).__name__, first_err)
                err_lower = str(first_err).lower()
                # Если ошибка 400 из-за thinkingConfig или responseSchema — пробуем ту же модель с упрощенным config
                if "thinking" in err_lower or "schema" in err_lower or "mime" in err_lower:
                    try:
                        return await self._call_gemini_single(
                            session, primary_model, system_prompt, user_text, proxy, use_low_thinking=False, use_json_schema=False, timeout_sec=25.0
                        )
                    except Exception:
                        pass

                fallbacks = await self._discover_gemini_models(session, proxy)
                last_err: Exception = first_err
                for fb_model in fallbacks:
                    if fb_model == primary_model or "2.5" in fb_model:
                        continue
                    try:
                        res = await self._call_gemini_single(
                            session, fb_model, system_prompt, user_text, proxy, use_low_thinking=True, use_json_schema=True, timeout_sec=25.0
                        )
                        logger.info("Переключаем рабочую модель по умолчанию на быструю: %s", fb_model)
                        self.config.ai_model = fb_model
                        return res
                    except Exception as fb_err:
                        fb_lower = str(fb_err).lower()
                        if "schema" in fb_lower or "mime" in fb_lower:
                            try:
                                res = await self._call_gemini_single(
                                    session, fb_model, system_prompt, user_text, proxy, use_low_thinking=False, use_json_schema=False, timeout_sec=25.0
                                )
                                logger.info("Переключаем рабочую модель по умолчанию на быструю: %s", fb_model)
                                self.config.ai_model = fb_model
                                return res
                            except Exception as fb_err2:
                                fb_err = fb_err2
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
                    "temperature": 0.3,
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
    def _decode_json_string_fragment(raw_fragment: str) -> str:
        """Безопасно декодирует JSON escape-последовательности из фрагмента строки."""
        def _repl_unicode(m: re.Match) -> str:
            try:
                return chr(int(m.group(1), 16))
            except Exception:
                return m.group(0)

        out = raw_fragment
        out = re.sub(r"\\u([0-9a-fA-F]{4})", _repl_unicode, out)
        # Используем плейсхолдер для экранированного обратного слэша \\ перед заменой \n, \t, \"
        out = out.replace("\\\\", "\x00BSLASH\x00")
        out = out.replace('\\"', '"')
        out = out.replace("\\/", "/")
        out = out.replace("\\n", "\n")
        out = out.replace("\\r", "")
        out = out.replace("\\t", "\t")
        out = out.replace("\x00BSLASH\x00", "\\")
        return out.strip()

    @classmethod
    def _parse_json_response(cls, raw: str, fallback_text: str) -> AIResult:
        # Убираем возможные блоки <think>...</think> и маркдаун-обёртки ```json
        cleaned = re.sub(r"<think>.*?</think>", "", raw.strip(), flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)

        # 1. Стандартный парсинг + strict=False (допускает неэкранированные переносы строк)
        for strict in (True, False):
            try:
                obj = json.loads(cleaned, strict=strict)
                if isinstance(obj, dict):
                    return AIResult(
                        is_rejected=bool(obj.get("is_rejected", False)),
                        reject_reason=str(obj.get("reject_reason", "")),
                        rewritten_text=str(obj.get("rewritten_text", "")).strip(),
                        is_freebie=bool(obj.get("is_freebie", False)),
                    )
            except Exception:
                pass

        # 2. Аварийное извлечение полей (если модель оставила неэкранированные кавычки внутри rewritten_text)
        m_rej = re.search(r'"is_rejected"\s*:\s*(true|false)', cleaned, flags=re.IGNORECASE)
        m_free = re.search(r'"is_freebie"\s*:\s*(true|false)', cleaned, flags=re.IGNORECASE)
        m_reason = re.search(r'"reject_reason"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, flags=re.DOTALL)
        m_rt = re.search(r'"rewritten_text"\s*:\s*"', cleaned)

        if m_rt:
            tail = cleaned[m_rt.end():]
            next_key = re.search(
                r'"\s*,\s*"(?:is_rejected|reject_reason|is_freebie)"\s*:',
                tail,
            )
            if next_key:
                raw_text = tail[: next_key.start()]
            else:
                end_brace = tail.rfind("}")
                search_area = tail[:end_brace] if end_brace != -1 else tail
                last_quote = search_area.rfind('"')
                raw_text = search_area[:last_quote] if last_quote != -1 else search_area

            decoded_text = cls._decode_json_string_fragment(raw_text)
            if decoded_text:
                logger.info("JSON от ИИ восстановлен резервным парсером.")
                return AIResult(
                    is_rejected=(m_rej.group(1).lower() == "true") if m_rej else False,
                    reject_reason=cls._decode_json_string_fragment(m_reason.group(1)) if m_reason else "",
                    rewritten_text=decoded_text,
                    is_freebie=(m_free.group(1).lower() == "true") if m_free else False,
                )

        # 3. Если ИИ вернул вообще не JSON, но там нет обрывков JSON-ключей — берём текст; иначе — оригинал!
        logger.warning("Не удалось распарсить JSON от ИИ, используем безопасный откат.")
        if '"is_rejected"' in cleaned or '"rewritten_text"' in cleaned:
            return AIResult(
                is_rejected=(m_rej.group(1).lower() == "true") if m_rej else False,
                reject_reason=cls._decode_json_string_fragment(m_reason.group(1)) if m_reason else "",
                rewritten_text=fallback_text.strip(),
                is_freebie=(m_free.group(1).lower() == "true") if m_free else False,
            )

        return AIResult(
            is_rejected=False,
            reject_reason="",
            rewritten_text=cleaned or fallback_text,
            is_freebie=False,
        )

    @staticmethod
    def has_freebie_tag(text: str) -> bool:
        return bool(re.search(r"#халява\b", text, flags=re.IGNORECASE))

    @staticmethod
    def set_freebie_tag(text: str, enabled: bool) -> str:
        # 1. Сначала убираем любые существующие варианты #халява (старую верхнюю плашку или встроенный тег)
        cleaned = re.sub(
            r"^\s*(?:🎁\s*)?(?:<(?:b|strong)>)?#халява(?:</(?:b|strong)>)?[ \t]*\n+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"[ \t]*(?:🎁\s*)?(?:<(?:b|strong)>)?#халява\b(?:</(?:b|strong)>)?(?=[ \t]*($|\n|[^\w<]))",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"(?:🎁\s*)?(?:<(?:b|strong)>)?#халява\b(?:</(?:b|strong)>)?[ \t]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        if not enabled:
            return cleaned

        if not cleaned:
            return FREEBIE_BADGE

        # 2. Ставим просто #халява сразу после заголовка поста через пробел
        first_line, sep, rest = cleaned.partition("\n")
        if not sep:
            # Если текст в один абзац без переноса строки и начинается с короткого жирного заголовка с точкой/разделителем
            m = re.match(
                r"^((?:[^\w<\n]{0,6}\s*)?<(?:b|strong)>[^<\n]{2,110}</(?:b|strong)>(?:[.!?:])?)(?:\s+(.{30,}))$",
                cleaned,
            )
            if m:
                return f"{m.group(1).rstrip()} {FREEBIE_BADGE} {m.group(2).lstrip()}"
            return f"{cleaned.rstrip()} {FREEBIE_BADGE}"

        return f"{first_line.rstrip()} {FREEBIE_BADGE}\n{rest}".strip()

    @staticmethod
    def balance_telegram_html(text: str, strip_tg_links: bool = False) -> str:
        """
        Приводит HTML-разметку к строго валидному для Telegram виду:
        - нормализует strong->b, em->i, del/strike->s, ins->u;
        - удаляет одиночные закрывающие теги (Unexpected end tag);
        - исправляет перекрёстное закрытие тегов по LIFO-стеку и закрывает незакрытые теги.
        """
        alias = {
            "strong": "b",
            "em": "i",
            "ins": "u",
            "del": "s",
            "strike": "s",
        }
        allowed = {"b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "a"}
        token_re = re.compile(r"<(/?)([a-zA-Z0-9-]+)([^>]*)>")

        out: list[str] = []
        stack: list[str] = []
        last_idx = 0

        for m in token_re.finditer(text):
            out.append(text[last_idx : m.start()])
            last_idx = m.end()

            is_close = m.group(1) == "/"
            raw_tag = m.group(2).lower()
            norm_tag = alias.get(raw_tag, raw_tag)
            attrs = m.group(3) or ""

            if norm_tag not in allowed:
                continue

            if not is_close:
                if norm_tag in ("a", "blockquote", "pre") and norm_tag in stack:
                    continue

                if norm_tag == "a":
                    href_m = re.search(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", attrs, flags=re.IGNORECASE)
                    if not href_m:
                        continue
                    url = (href_m.group(1) or href_m.group(2) or "").strip()
                    if not re.match(r"^(?:https?://|tg://user\?id=)", url, flags=re.IGNORECASE):
                        continue
                    if strip_tg_links and re.match(r"^https?://(?:t\.me|telegram\.me)/", url, flags=re.IGNORECASE):
                        continue
                    safe_url = url.replace('"', "%22")
                    safe_url = re.sub(r"&(?!(?:lt|gt|amp|quot|#\d+);)", "&amp;", safe_url)
                    stack.append("a")
                    out.append(f'<a href="{safe_url}">')
                elif norm_tag == "blockquote":
                    stack.append("blockquote")
                    out.append("<blockquote expandable>" if "expandable" in attrs.lower() else "<blockquote>")
                elif norm_tag == "code":
                    lang_m = re.search(r'class\s*=\s*["\']?(language-[a-zA-Z0-9_+-]+)', attrs, flags=re.IGNORECASE)
                    stack.append("code")
                    if lang_m and "pre" in stack:
                        out.append(f'<code class="{lang_m.group(1)}">')
                    else:
                        out.append("<code>")
                else:
                    stack.append(norm_tag)
                    out.append(f"<{norm_tag}>")
            else:
                if norm_tag not in stack:
                    continue
                while stack:
                    top = stack.pop()
                    out.append(f"</{top}>")
                    if top == norm_tag:
                        break

        out.append(text[last_idx:])
        while stack:
            out.append(f"</{stack.pop()}>")

        return "".join(out)

    @staticmethod
    def strip_html_tags(text: str) -> str:
        """Безопасно убирает HTML-теги (на случай крайнего фоллбэка в plain text)."""
        import html as _html
        # Сохраняем URL из ссылок рядом со словом, чтобы не терять ссылку
        no_links = re.sub(
            r'<a\s+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
            lambda m: m.group(2) if m.group(1) in m.group(2) else f"{m.group(2)} ({m.group(1)})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        plain = re.sub(r"</?[a-zA-Z0-9_-]+(?:\s+[^>]*)?>", "", no_links)
        return _html.unescape(plain).strip()

    @classmethod
    def truncate_telegram_html(cls, text: str, max_len: int) -> str:
        """Обрезает HTML-текст до max_len символов, не ломая теги и сущности."""
        balanced = cls.balance_telegram_html(text)
        if len(balanced) <= max_len:
            return balanced

        limit = max(0, max_len - 48)
        while limit > 0:
            chunk = balanced[:limit]
            # Не режем внутри незакрытого тега '<...' или HTML-сущности '&...;'
            lt = chunk.rfind("<")
            gt = chunk.rfind(">")
            if lt > gt:
                chunk = chunk[:lt]
            amp = chunk.rfind("&")
            semi = chunk.rfind(";")
            if amp > semi:
                chunk = chunk[:amp]

            candidate = cls.balance_telegram_html(chunk.rstrip() + "...")
            if len(candidate) <= max_len:
                return candidate
            limit -= max(20, len(candidate) - max_len + 16)

        return cls.strip_html_tags(balanced)[:max_len]

    @classmethod
    def _sanitize_html_and_links(cls, text: str) -> str:
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

        # 4. Конвертируем частые HTML/Markdown конструкции в поддерживаемые Telegram HTML
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*p(?:\s+[^>]*)?\s*>", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"</?(?:div|span|h[1-6]|ul|ol|li|section|article|header|footer|font|center|mark|small|sub|sup|tg-emoji)\b[^>]*>",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        # Если ИИ написал цитату через Markdown '> строка', оборачиваем в <blockquote>
        text = re.sub(
            r"(?:^|\n)((?:>\s?.+(?:\n|$))+)",
            lambda m: "\n<blockquote>"
            + "\n".join(re.sub(r"^>\s?", "", line) for line in m.group(1).strip().splitlines())
            + "</blockquote>\n",
            text,
        )

        allowed_pattern = re.compile(
            r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler)\b[^>]*>"
            r"|<a\s+href=[\"'](?:https?://|tg://user\?id=)[^\"']+[\"'][^>]*>|</a>",
            flags=re.IGNORECASE,
        )
        preserved: list[str] = []

        def _stash(match: re.Match) -> str:
            tag_str = match.group(0)
            a_match = re.match(
                r'<a\s+href=(["\'](?:https?://|tg://user\?id=)[^"\']+["\'])[^>]*>',
                tag_str,
                flags=re.IGNORECASE,
            )
            if a_match:
                tag_str = f"<a href={a_match.group(1)}>"
            elif re.match(r"<blockquote\b", tag_str, flags=re.IGNORECASE):
                tag_str = (
                    "<blockquote expandable>"
                    if "expandable" in tag_str.lower()
                    else "<blockquote>"
                )
            idx = len(preserved)
            preserved.append(tag_str)
            return f"\x00TAG{idx}\x00"

        stashed = allowed_pattern.sub(_stash, text)
        # Убираем битые/неподдержанные <a href="..."> (у которых нет http/https)
        stashed = re.sub(r"</?a\b[^>]*>", "", stashed, flags=re.IGNORECASE)
        # Оставшиеся угловые скобки (например, <int> в коде или обрывки битых тегов) безопасно экранируем
        stashed = re.sub(r"&(?!(?:lt|gt|amp|quot|#\d+);)", "&amp;", stashed)
        stashed = stashed.replace("<", "&lt;").replace(">", "&gt;")

        for idx, tag in enumerate(preserved):
            stashed = stashed.replace(f"\x00TAG{idx}\x00", tag)

        balanced = cls.balance_telegram_html(stashed, strip_tg_links=True)
        balanced = re.sub(r"\n{3,}", "\n\n", balanced)
        return balanced.strip()
