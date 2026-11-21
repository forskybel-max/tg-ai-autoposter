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
        custom_palette = (await self.db.get_setting("custom_emoji_palette", "")).strip()
        emoji_list_bullet = (await self.db.get_setting("emoji_list_bullet", "")).strip()
        emoji_headline = (await self.db.get_setting("emoji_headline", "")).strip()
        all_sources = await self.db.get_sources(only_enabled=False)
        donor_titles = [str(s.get("title") or "").strip() for s in all_sources if s.get("title")]

        if not original_text or not original_text.strip():
            final_caption = signature.strip() if signature.strip() else ""
            return AIResult(
                is_rejected=False,
                reject_reason="",
                rewritten_text=final_caption,
            )

        # 1. Предварительная очистка исходника от подписей каналов-доноров («🙂 Не баг, а фича» + их лого-<tg-emoji>)
        #    ДО извлечения кастомных эмодзи и ДО отправки в ИИ!
        precleaned_original = self.strip_donor_watermarks(original_text, donor_titles=donor_titles)
        if not precleaned_original.strip():
            return AIResult(
                is_rejected=True,
                reject_reason="Пост состоял только из ссылки/подписи чужого канала",
                rewritten_text="",
            )

        # 2. Быстрая программная проверка на дубликаты по последним 80 постам (до вызова ИИ)
        if filter_ads and not extra_instruction:
            recent_pairs = await self.db.get_recent_posts_for_dedup(limit=80)
            is_dup, dup_reason = self.is_programmatic_duplicate(precleaned_original, recent_pairs)
            if is_dup:
                return AIResult(
                    is_rejected=True,
                    reject_reason=dup_reason,
                    rewritten_text="",
                )

        recent_snippets = await self.db.get_recent_post_snippets(limit=35)
        recent_context = ""
        if recent_snippets:
            formatted = "\n".join(f"- {s}" for s in recent_snippets)
            recent_context = (
                "\nСписок недавно обработанных постов (ЕСЛИ новый пост про то же самое событие, "
                "ту же новость, тот же сервис или ту же раздачу — ОБЯЗАТЕЛЬНО отклони его как дубликат "
                'с "is_rejected": true и "reject_reason": "Смысловой дубликат уже вышедшего поста"):\n'
                f"{formatted}\n"
            )

        orig_custom_emojis = self.extract_custom_emoji_tags(precleaned_original)
        emoji_context = ""
        if orig_custom_emojis:
            emoji_context += (
                "\nВ исходном посте используются платные анимированные эмодзи — "
                "сохрани эти же точные теги на тех же местах (в начале заголовка и в качестве маркеров пунктов списка):\n"
                + " ".join(orig_custom_emojis)
                + "\n"
            )
        if emoji_list_bullet:
            emoji_context += (
                f"\nФирменный анимированный эмодзи для маркированных списков (ставь его в начале каждого пункта списка вместо обычной точки «•»): {emoji_list_bullet}\n"
            )
        if emoji_headline:
            emoji_context += (
                f"\nФирменные анимированные эмодзи для начала заголовка/первого абзаца: {emoji_headline}\n"
            )
        if custom_palette:
            emoji_context += (
                "\nДополнительные фирменные анимированные эмодзи канала (для акцентов):\n"
                f"{custom_palette}\n"
            )

        ad_rule = (
            "СТРОГИЙ ФИЛЬТР ТЕМАТИКИ, РЕКЛАМЫ И ДУБЛИКАТОВ:\n"
            "Тематика нашего канала СТРОГО ограничена 4 направлениями:\n"
            "1) Искусственный интеллект и нейросети (ИИ-модели, LLM, генераторы, агенты, промпты, ИИ-сервисы и исследования);\n"
            "2) Халява по ИИ и IT (бесплатный доступ к нейросетям/подпискам/API, промокоды, полезные бесплатные тулзы и раздачи) — ставь \"is_freebie\": true;\n"
            "3) Вайбкодинг (Vibe Coding) и разработка с ИИ (Cursor, Claude Code, Windsurf, Copilot, GitHub-репозитории, MCP, автоматизация);\n"
            "4) Технологии — ТОЛЬКО тесно связанные или пересекающиеся с ИИ, нейросетями, вайбкодингом и разработкой.\n\n"
            "ОБЯЗАТЕЛЬНО ОТКЛОНЯЙ ПОСТ (ставь \"is_rejected\": true и укажи причину в \"reject_reason\"), если это:\n"
            "- ОФФТОП НЕ ПО ТЕМЕ: еда, чипсы (Lay's и т.д.), напитки, кино, сериалы, обычные игры (GTA, Dota, CS и др. без прямой связи с ИИ), "
            "автомобили, политика, криминал, погода, бытовые новости и общие гаджеты без ИИ;\n"
            "- ПУСТЫЕ МЕМЫ И ЩИТПОСТИНГ: картинки-приколы или шутки без полезной информационной сути по ИИ/вайбкодингу;\n"
            "- РЕКЛАМА: реклама банков, кредиток, магазинов/маркетплейсов (например, «ЛАЧУГА» и т.п.), курсов, букмекеров, крипто-токенов, "
            "розыгрышей за подписку или чужих Telegram-каналов/ботов;\n"
            "- ДУБЛИКАТ: если пост рассказывает о том же самом событии, релизе, новости или халяве, что и любой из постов в списке недавно обработанных выше!"
            if filter_ads
            else 'Всегда возвращай "is_rejected": false (фильтр рекламы отключен).'
        )

        plain_orig_len = len(self.strip_html_tags(precleaned_original))
        if has_media and plain_orig_len <= 900:
            length_rule = (
                "Поскольку к посту прикреплено медиа и исходник компактный, видимая длина текста "
                "(без учета HTML-тегов) не должна превышать 950 символов, чтобы пост вышел одним цельным сообщением."
            )
        elif has_media:
            length_rule = (
                "К посту прикреплено медиа: сохрани все важные пункты и детали, но уложи видимый текст "
                "(без учета HTML-тегов) максимум в 980 символов, чтобы медиа и текст опубликовались ОДНИМ постом, а не двумя!"
            )
        else:
            length_rule = (
                "Если исходный пост большой и подробный — ОБЯЗАТЕЛЬНО сохрани ВСЕ пункты, списки, детали и абзацы "
                "полностью без сокращений и урезаний (до 3800 символов)."
            )

        extra_block = (
            f"\nДополнительное пожелание редактора к этому варианту: {extra_instruction}\n"
            if extra_instruction
            else ""
        )

        tech_rules = (
            "Обязательные правила бережного сохранения структуры, очистки от нативных вставок и оформления:\n"
            "1. СОХРАНЯЙ СТРУКТУРУ И СПИСКИ ОРИГИНАЛА 1-В-1: НЕ перекраивай композицию поста! "
            "Если в оригинале идёт подводка и обычный список из N пунктов — сохрани все N пунктов списка БЕЗ придумывания "
            "искусственных жирных ярлыков в начале каждого пункта (НЕ пиши от себя «• <b>Последствия:</b>», если в оригинале просто список!).\n"
            "2. ЦИТАТЫ (<blockquote>) — ТОЛЬКО ПО ОРИГИНАЛУ: оборачивай в <blockquote>...</blockquote> или <blockquote expandable>...</blockquote> "
            "ТОЛЬКО те блоки, которые были цитатой в исходнике. НЕ оборачивай финальный абзац в <blockquote> от себя!\n"
            "3. ВЫРЕЗАЙ НАТИВНУЮ РЕКЛАМУ И ПОДПИСИ КАНАЛОВ ВНУТРИ ПОСТА: если в полезном посте про ИИ/технологии автор вклеил "
            "рекламное предложение про магазин/спонсора (например: «а для клиентов ЛАЧУГИ ничего не изменится — всё станет только лучше!») — "
            "ПОЛНОСТЬЮ УДАЛИ это рекламное предложение и ссылку на магазин! Также всегда удаляй названия каналов-доноров в конце текста "
            "(«🙂 Не баг, а фича», «КиберТопор», «Бэкдор» и их смайлик-логотип перед названием).\n"
            "4. ВЫРЕЗАЙ ОБЩЕНИЕ АВТОРА С АУДИТОРИЕЙ: полностью удаляй фразы автора донора, обращённые к его подписчикам: "
            "выпрашивание реакций и лайков («50 реакций и я делаю подборку с халявой», «Накидайте 🔥», «Пишите в комментах»), "
            "обещания («Завтра сделаю разбор», «Как я писал вчера»).\n"
            "5. ПЛАТНЫЕ АНИМИРОВАННЫЕ ЭМОДЗИ (<tg-emoji>): сохраняй смысловые теги <tg-emoji emoji-id=\"ЧИСЛО\">эмодзи</tg-emoji> "
            "перед заголовком и перед каждым пунктом списка (но НЕ сохраняй <tg-emoji>, который стоял рядом с названием чужого канала-донора!).\n"
            "6. ССЫЛКИ: сохраняй полезные внешние ссылки на ИИ-сервисы, GitHub, нейронки, инструменты и статьи через "
            '<a href="https://...">текст</a>. Удаляй ссылки на магазины-спонсоры и чужие Telegram-каналы.\n'
            "7. РУБРИКА ХАЛЯВА: если в посте речь идет о халяве по теме ИИ/IT — поставь в JSON поле "
            '"is_freebie": true (иначе false). Сам хэштег #халява внутрь текста НЕ пиши — система добавит его после заголовка автоматически!\n'
            "8. Используй ТОЛЬКО HTML-теги Telegram: <b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>, <blockquote expandable>, "
            "<tg-spoiler>, <a href=\"...\">, <tg-emoji emoji-id=\"...\"> (никакого Markdown **текст** или > цитата)."
        )

        system_prompt = (
            f"{style_prompt}\n\n"
            f"{tech_rules}\n"
            f"{emoji_context}\n"
            f"{length_rule}\n"
            f"{ad_rule}\n"
            f"{recent_context}"
            f"{extra_block}\n"
            "Ответь СТРОГО валидным JSON-объектом без обёрток ```json и лишнего текста:\n"
            "{\n"
            '  "is_rejected": false,\n'
            '  "reject_reason": "",\n'
            '  "is_freebie": false,\n'
            '  "rewritten_text": "Бережно отредактированный текст без подписей доноров, без нативной рекламы и без выпрашивания реакций"\n'
            "}"
        )

        raw_response = await self._call_llm(
            system_prompt=system_prompt,
            user_text=f"Исходный текст поста (с HTML-разметкой оригинала):\n\n{precleaned_original}",
        )

        parsed = self._parse_json_response(raw_response, fallback_text=precleaned_original)
        if parsed.is_rejected:
            return parsed

        cleaned_text = self.strip_donor_watermarks(parsed.rewritten_text, donor_titles=donor_titles)
        cleaned_text = self._sanitize_html_and_links(cleaned_text)
        cleaned_text = self.strip_donor_watermarks(cleaned_text, donor_titles=donor_titles)
        cleaned_text = self._restore_dropped_custom_emojis(
            cleaned_text,
            precleaned_original,
            palette=custom_palette,
            emoji_list_bullet=emoji_list_bullet,
            emoji_headline=emoji_headline,
        )
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
                        if "hydrogen" in name or "flash" in name:
                            candidates.append(name)
                logger.info("Доступные модели Gemini (Hydrogen/Flash) на ключе: %s", candidates)

                # Приоритет: gemini-4-hydrogen (1M контекст, быстрый), затем проверенные 3.5/3.1 Flash
                def _model_rank(name: str) -> tuple[int, str]:
                    if name == "gemini-4-hydrogen" or "hydrogen" in name:
                        return (0, name)
                    if name == "gemini-3.5-flash":
                        return (1, name)
                    if name == "gemini-3.5-flash-lite":
                        return (2, name)
                    if name == "gemini-3.1-flash-lite":
                        return (3, name)
                    if name in ("gemini-flash-lite-latest", "gemini-flash-latest"):
                        return (4, name)
                    if "2.5" in name:
                        return (9, name)
                    if "preview" in name or "3.8" in name or "3.7" in name:
                        return (6, name)
                    if "lite" in name:
                        return (5, name)
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
        gen_cfg: dict = {"temperature": 0.25, "maxOutputTokens": 8192}
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
        if use_low_thinking and (
            "hydrogen" in model or "4" in model or "3.8" in model or "3.7" in model
        ):
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
        """
        Вызов нативного REST API Google Gemini.
        Всегда пробует основную модель (self.config.ai_model, например gemini-4-hydrogen) первой
        с быстрым повтором при временном 503, и НЕ перезаписывает self.config.ai_model при разовом фоллбэке!
        """
        proxy = self._resolve_proxy()
        primary_model = self.config.ai_model.removeprefix("models/")

        async with aiohttp.ClientSession() as session:
            try:
                return await self._call_gemini_single(
                    session, primary_model, system_prompt, user_text, proxy, use_low_thinking=True, use_json_schema=True, timeout_sec=28.0
                )
            except (asyncio.TimeoutError, Exception) as first_err:
                err_lower = str(first_err).lower()
                # Если временный всплеск нагрузки HTTP 503 на основной модели — ждём 1.5 сек и пробуем её ещё раз
                if "503" in err_lower or "unavailable" in err_lower or "high demand" in err_lower:
                    await asyncio.sleep(1.5)
                    try:
                        return await self._call_gemini_single(
                            session, primary_model, system_prompt, user_text, proxy, use_low_thinking=True, use_json_schema=True, timeout_sec=28.0
                        )
                    except Exception as retry_err:
                        first_err = retry_err
                        err_lower = str(first_err).lower()

                logger.warning("Первичная модель %s временно недоступна (%s: %s), используем резервную модель на этот запрос...", primary_model, type(first_err).__name__, first_err)
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
                        return await self._call_gemini_single(
                            session, fb_model, system_prompt, user_text, proxy, use_low_thinking=True, use_json_schema=True, timeout_sec=25.0
                        )
                    except Exception as fb_err:
                        fb_lower = str(fb_err).lower()
                        if "schema" in fb_lower or "mime" in fb_lower:
                            try:
                                return await self._call_gemini_single(
                                    session, fb_model, system_prompt, user_text, proxy, use_low_thinking=False, use_json_schema=False, timeout_sec=25.0
                                )
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
    def extract_custom_emoji_tags(text: str) -> list[str]:
        """Извлекает уникальные теги <tg-emoji emoji-id="...">...</tg-emoji> из HTML."""
        if not text:
            return []
        pattern = re.compile(
            r'<tg-emoji\s+(?:emoji-id|document-id)=["\']?(\d+)["\']?\s*>(.*?)</tg-emoji>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        seen: set[str] = set()
        result: list[str] = []
        for m in pattern.finditer(text):
            eid = m.group(1)
            raw_inner = re.sub(r"</?[a-zA-Z0-9_-]+[^>]*>", "", m.group(2)).strip()
            inner = raw_inner or "✨"
            tag = f'<tg-emoji emoji-id="{eid}">{inner}</tg-emoji>'
            if tag not in seen:
                seen.add(tag)
                result.append(tag)
        return result

    @staticmethod
    def count_custom_emojis(text: str) -> int:
        """Считает количество вхождений <tg-emoji emoji-id="..."> в тексте."""
        if not text:
            return 0
        return len(
            re.findall(
                r'<tg-emoji\s+(?:emoji-id|document-id)=["\']?\d+["\']?\s*>',
                text,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _restore_dropped_custom_emojis(
        cls,
        rewritten: str,
        original: str,
        palette: str = "",
        emoji_list_bullet: str = "",
        emoji_headline: str = "",
    ) -> str:
        """
        1. Восстанавливает <tg-emoji emoji-id="ID">, если ИИ написал просто голый смайлик без тега.
        2. Если в оригинале пункты списка начинались с кастомного <tg-emoji> (например, красной точки)
           или у пользователя задан свой фирменный эмодзи для списков (emoji_list_bullet),
           автоматически заменяет простые маркеры списков («• », «- », «▪️ ») в начале строк на этот <tg-emoji>!
        3. Если задан фирменный эмодзи для заголовка (emoji_headline) или он был в начале оригинала,
           гарантирует его наличие в первой строке.
        """
        if not rewritten:
            return rewritten

        all_source_tags = (
            cls.extract_custom_emoji_tags(original)
            + cls.extract_custom_emoji_tags(emoji_list_bullet)
            + cls.extract_custom_emoji_tags(emoji_headline)
            + cls.extract_custom_emoji_tags(palette)
        )

        out = rewritten

        # 1. Определяем кастомный эмодзи-буллет для списков (приоритет: настройка пользователя -> буллет из оригинала)
        user_bullets = cls.extract_custom_emoji_tags(emoji_list_bullet)
        bullet_tag: str | None = user_bullets[0] if user_bullets else None
        if not bullet_tag and original:
            # Проверяем, есть ли в оригинале строки, начинающиеся с <tg-emoji ...>
            line_start_tags = re.findall(
                r"(?:^|\n)[ \t]*(?:<(?:b|strong|i|em)>)?(<tg-emoji\s+(?:emoji-id|document-id)=[\"']?\d+[\"']?\s*>.*?</tg-emoji>)",
                original,
                flags=re.IGNORECASE,
            )
            # Если какой-то <tg-emoji> стоит в начале строки хотя бы 2 раза (список!) или после него идёт текст пункта
            if len(line_start_tags) >= 2:
                # Берём самый частый <tg-emoji> в начале строк (обычно это маркер списка, а не заголовок)
                counts: dict[str, int] = {}
                for raw_t in line_start_tags:
                    norm_list = cls.extract_custom_emoji_tags(raw_t)
                    if norm_list:
                        t_norm = norm_list[0]
                        counts[t_norm] = counts.get(t_norm, 0) + 1
                for t_norm, cnt in counts.items():
                    if cnt >= 2:
                        bullet_tag = t_norm
                        break
                if not bullet_tag and len(line_start_tags) >= 2:
                    norm_second = cls.extract_custom_emoji_tags(line_start_tags[1])
                    if norm_second:
                        bullet_tag = norm_second[0]

        if bullet_tag:
            # Заменяем простые текстовые маркеры списков в начале строк на анимированный <tg-emoji>
            out = re.sub(
                r"(^|\n)([ \t]*)(?:•|▪|▪️|▫|▫️|🔸|🔹|—|-|\*)[ \t]+",
                lambda m: f"{m.group(1)}{m.group(2)}{bullet_tag} ",
                out,
            )

        # 2. Сопоставляем внутренний смайлик -> полный тег <tg-emoji emoji-id="...">смайлик</tg-emoji>
        if all_source_tags:
            emoji_map: dict[str, str] = {}
            for tag in all_source_tags:
                m = re.match(
                    r'^(<tg-emoji\s+emoji-id="\d+">)(.+?)(</tg-emoji>)$',
                    tag,
                    flags=re.DOTALL,
                )
                if m:
                    inner_char = m.group(2).strip()
                    # Не заменяем обычные буквы/цифры и точку • (её уже обработали выше только в начале строк)
                    if (
                        inner_char
                        and inner_char not in ("•", "-", "*")
                        and not any(ch.isalnum() for ch in inner_char)
                        and inner_char not in emoji_map
                    ):
                        emoji_map[inner_char] = tag

            for plain_emoji, full_tag in emoji_map.items():
                escaped = re.escape(plain_emoji)
                out = re.sub(
                    rf'(?<!">){escaped}(?!</tg-emoji>)',
                    full_tag,
                    out,
                )

        # 3. Проверяем заголовок (первую строку): если задан emoji_headline или в первой строке оригинала был <tg-emoji>
        first_line, sep, rest = out.partition("\n")
        if cls.count_custom_emojis(first_line) == 0:
            head_candidates = cls.extract_custom_emoji_tags(emoji_headline)
            if not head_candidates and original:
                orig_first_line = original.splitlines()[0] if original.splitlines() else ""
                head_candidates = cls.extract_custom_emoji_tags(orig_first_line)
            if head_candidates:
                head_tag = head_candidates[0]
                # Если в начале первой строки стоит обычный эмодзи — заменяем его на кастомный, иначе ставим в начало
                first_line_clean = re.sub(
                    r"^((?:<(?:b|strong)>)?)\s*(?:[^\w\s<]{1,3})\s*",
                    rf"\1{head_tag} ",
                    first_line,
                    count=1,
                )
                if cls.count_custom_emojis(first_line_clean) == 0:
                    if first_line.startswith("<b>"):
                        first_line_clean = f"<b>{head_tag} " + first_line[3:]
                    else:
                        first_line_clean = f"{head_tag} {first_line}"
                out = f"{first_line_clean}{sep}{rest}" if sep else first_line_clean

        return cls.balance_telegram_html(out, strip_tg_links=True)

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
                r"^((?:(?:<tg-emoji[^>]*>.*?</tg-emoji>|[^\w<\n]){0,6}\s*)?<(?:b|strong)>.*?</(?:b|strong)>(?:[.!?:])?)(?:\s+(.{30,}))$",
                cleaned,
            )
            if m and len(m.group(1)) <= 220:
                return f"{m.group(1).rstrip()} {FREEBIE_BADGE} {m.group(2).lstrip()}"
            return f"{cleaned.rstrip()} {FREEBIE_BADGE}"

        return f"{first_line.rstrip()} {FREEBIE_BADGE}\n{rest}".strip()

    @staticmethod
    def balance_telegram_html(text: str, strip_tg_links: bool = False) -> str:
        """
        Приводит HTML-разметку к строго валидному для Telegram виду:
        - нормализует strong->b, em->i, del/strike->s, ins->u;
        - сохраняет платные анимированные эмодзи <tg-emoji emoji-id="...">...</tg-emoji>;
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
        allowed = {"b", "i", "u", "s", "code", "pre", "blockquote", "tg-spoiler", "a", "tg-emoji"}
        token_re = re.compile(r"<(/?)([a-zA-Z0-9-]+)([^>]*)>")

        out: list[str] = []
        # stack хранит кортежи (norm_tag, open_html_str)
        stack: list[tuple[str, str]] = []
        last_idx = 0

        def _in_stack(tag_name: str) -> bool:
            return any(t == tag_name for t, _ in stack)

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
                if norm_tag in ("a", "blockquote", "pre", "tg-emoji") and _in_stack(norm_tag):
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
                    open_str = f'<a href="{safe_url}">'
                    stack.append(("a", open_str))
                    out.append(open_str)
                elif norm_tag == "tg-emoji":
                    eid_m = re.search(
                        r"""(?:emoji-id|document-id)\s*=\s*(?:"(\d+)"|'(\d+)'|(\d+))""",
                        attrs,
                        flags=re.IGNORECASE,
                    )
                    if not eid_m:
                        continue
                    eid = eid_m.group(1) or eid_m.group(2) or eid_m.group(3)
                    open_str = f'<tg-emoji emoji-id="{eid}">'
                    stack.append(("tg-emoji", open_str))
                    out.append(open_str)
                elif norm_tag == "blockquote":
                    open_str = "<blockquote expandable>" if "expandable" in attrs.lower() else "<blockquote>"
                    stack.append(("blockquote", open_str))
                    out.append(open_str)
                elif norm_tag == "code":
                    lang_m = re.search(r'class\s*=\s*["\']?(language-[a-zA-Z0-9_+-]+)', attrs, flags=re.IGNORECASE)
                    if lang_m and _in_stack("pre"):
                        open_str = f'<code class="{lang_m.group(1)}">'
                    else:
                        open_str = "<code>"
                    stack.append(("code", open_str))
                    out.append(open_str)
                else:
                    open_str = f"<{norm_tag}>"
                    stack.append((norm_tag, open_str))
                    out.append(open_str)
            else:
                if not _in_stack(norm_tag):
                    continue
                reopen: list[tuple[str, str]] = []
                while stack:
                    top_tag, top_open = stack.pop()
                    out.append(f"</{top_tag}>")
                    if top_tag == norm_tag:
                        break
                    if top_tag in ("b", "i", "u", "s", "tg-spoiler", "a"):
                        reopen.append((top_tag, top_open))
                for r_tag, r_open in reversed(reopen):
                    stack.append((r_tag, r_open))
                    out.append(r_open)

        out.append(text[last_idx:])
        while stack:
            top_tag, _ = stack.pop()
            out.append(f"</{top_tag}>")

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

    @staticmethod
    def strip_custom_emoji_tags(text: str) -> str:
        """Убирает обёртку <tg-emoji ...>, оставляя внутренний стандартный смайлик (для Bot API фоллбэка)."""
        return re.sub(r"</?tg-emoji\b[^>]*>", "", text, flags=re.IGNORECASE)

    @classmethod
    def visible_length(cls, text: str) -> int:
        """Возвращает видимую длину текста в Telegram (без HTML-тегов)."""
        if not text:
            return 0
        import html as _html

        plain = re.sub(r"</?[a-zA-Z0-9_-]+(?:\s+[^>]*)?>", "", text)
        return len(_html.unescape(plain))

    @classmethod
    def strip_donor_watermarks(
        cls, text: str, donor_titles: list[str] | None = None
    ) -> str:
        """
        Вырезает:
        1) Подписи каналов-доноров («🙂 Не баг, а фича», «КиберТопор», «Бэкдор» и др.)
           вместе с предшествующим им смайликом / <tg-emoji> и ссылкой на t.me/...;
        2) Выпрашивание реакций и общение автора донора с аудиторией («50 реакций и я делаю подборку...»).
        """
        if not text:
            return ""

        out = text

        # 1. Удаляем связку: (опциональный <tg-emoji> или смайлик/тире) + ссылка <a href="t.me/...">Название канала</a>
        out = re.sub(
            r'(?:[ \t]*<tg-emoji\b[^>]*>[^<]*?</tg-emoji>|[ \t]*[🙂😉😎🔥⚡️👉👇📌•|—\-~])*'
            r'[ \t]*<a\s+href=["\'](?:https?://)?(?:t\.me|telegram\.me)/[^"\']*["\'][^>]*>[^<]*?</a>',
            "",
            out,
            flags=re.IGNORECASE,
        )

        # 2. Удаляем известные названия каналов-доноров (в конце поста или отдельной строкой) вместе со смайликом перед ними
        known_donors = {
            "Не баг, а фича",
            "КиберТопор",
            "Бэкдор",
            "Вайб-кодинг",
            "XOR",
            "CodeCamp",
            "Матрица",
            "AI для своих",
            "НЕЙРО ПРОФИТ",
            "Никита Велс | AI",
            "Никита Велс",
            "Бывший Скамер печатает..",
            "Бывший Скамер печатает",
            "Александр Бубнов [ex. ИИШНЫЙ]",
            "Александр Бубнов",
            "ИИШНЫЙ",
            "SGX",
            "выф",
        }
        if donor_titles:
            for dt in donor_titles:
                if dt and len(dt.strip()) >= 3:
                    known_donors.add(dt.strip())

        for title in sorted(known_donors, key=len, reverse=True):
            esc_title = re.escape(title)
            # В конце текста или строки: (<tg-emoji>... или любой смайлик) + Название донора в конце поста
            out = re.sub(
                rf"(?:[ \t\n]*(?:<tg-emoji\b[^>]*>[^<]*?</tg-emoji>|[^\w\s<>\"'.,!?;:(){{\}}\[\]]+))?[ \t]*(?:<(?:b|strong|i|em)>)?{esc_title}(?:</(?:b|strong|i|em)>)?[ \t.!]*$",
                "",
                out,
                flags=re.IGNORECASE,
            )

        # 3. Удаляем выпрашивание реакций/комментариев и обещания автора чужого канала
        reaction_patterns = [
            r"[^\n.!?]*\b\d+\s*(?:реакци[йяи]|лайк(?:ов|а)?|огоньк(?:ов|а)?|буст(?:ов|а)?)\s+и\s+(?:я|мы)\b[^\n.!?]*[.!?]?",
            r"[^\n.!?]*\b(?:набер[её]м|накидайте|ставьте|жду)\s+\d*\s*(?:реакци[йяи]|лайк(?:ов|а)?|огоньк(?:ов|а)?|🔥|❤️|👍)[^\n.!?]*[.!?]?",
            r"[^\n.!?]*\bпишите\s+в\s+комментар(?:иях|ии)\b[^\n.!?]*[.!?]?",
        ]
        for pat in reaction_patterns:
            out = re.sub(pat, "", out, flags=re.IGNORECASE)

        out = "\n".join(line.rstrip() for line in out.splitlines())
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()

    @classmethod
    def is_programmatic_duplicate(
        cls, candidate_text: str, recent_pairs: list[tuple[str, str]]
    ) -> tuple[bool, str]:
        """Быстрая проверка на дубликаты по словам и символьным 4-граммам против последних 80 постов."""
        def _tokenize(raw: str) -> tuple[set[str], set[str]]:
            plain = cls.strip_html_tags(raw).lower()
            plain = re.sub(r"https?://\S+", "", plain)
            words = {w for w in re.findall(r"[a-zа-яё0-9]{3,}", plain) if len(w) >= 3}
            compact = re.sub(r"[^a-zа-яё0-9]", "", plain)
            ngrams = {compact[i : i + 4] for i in range(max(0, len(compact) - 3))}
            return words, ngrams

        cand_words, cand_ngrams = _tokenize(candidate_text)
        if len(cand_words) < 5 and len(cand_ngrams) < 15:
            return False, ""

        for orig_prev, rew_prev in recent_pairs:
            for prev in (orig_prev, rew_prev):
                if not prev:
                    continue
                p_words, p_ngrams = _tokenize(prev)
                if len(p_words) >= 5 and len(cand_words) >= 5:
                    inter_w = len(cand_words & p_words)
                    union_w = len(cand_words | p_words)
                    min_w = min(len(cand_words), len(p_words))
                    if union_w > 0 and (inter_w / union_w >= 0.58 or (min_w >= 8 and inter_w / min_w >= 0.78)):
                        return True, "Программный фильтр: дубликат уже обработанного поста"
                if len(p_ngrams) >= 15 and len(cand_ngrams) >= 15:
                    inter_n = len(cand_ngrams & p_ngrams)
                    union_n = len(cand_ngrams | p_ngrams)
                    if union_n > 0 and (inter_n / union_n >= 0.56):
                        return True, "Программный фильтр: текстовый дубликат уже обработанного поста"

        return False, ""

    @classmethod
    def truncate_telegram_html(cls, text: str, max_len: int) -> str:
        """
        Обрезает HTML-текст так, чтобы его ВИДИМАЯ длина (без учёта HTML-тегов <tg-emoji>, <a> и др.)
        не превышала max_len символов, не ломая теги и сущности.
        """
        balanced = cls.balance_telegram_html(text)
        if cls.visible_length(balanced) <= max_len:
            return balanced

        limit = len(balanced) - 32
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
            vis_len = cls.visible_length(candidate)
            if vis_len <= max_len:
                return candidate
            step = max(24, (vis_len - max_len) + 16)
            limit -= step

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
            r"</?(?:div|span|h[1-6]|ul|ol|li|section|article|header|footer|font|center|mark|small|sub|sup)\b[^>]*>",
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
            r"|<a\s+href=[\"'](?:https?://|tg://user\?id=)[^\"']+[\"'][^>]*>|</a>"
            r"|<tg-emoji\s+(?:emoji-id|document-id)=[\"']?\d+[\"']?[^>]*>|</tg-emoji>",
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
            elif re.match(r"<tg-emoji\b", tag_str, flags=re.IGNORECASE):
                eid_m = re.search(r"(?:emoji-id|document-id)=[\"']?(\d+)[\"']?", tag_str, flags=re.IGNORECASE)
                if eid_m:
                    tag_str = f'<tg-emoji emoji-id="{eid_m.group(1)}">'
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
        # Убираем битые/неподдержанные <a href="..."> и <tg-emoji> без валидных атрибутов
        stashed = re.sub(r"</?(?:a|tg-emoji)\b[^>]*>", "", stashed, flags=re.IGNORECASE)
        # Оставшиеся угловые скобки (например, <int> в коде или обрывки битых тегов) безопасно экранируем
        stashed = re.sub(r"&(?!(?:lt|gt|amp|quot|#\d+);)", "&amp;", stashed)
        stashed = stashed.replace("<", "&lt;").replace(">", "&gt;")

        for idx, tag in enumerate(preserved):
            stashed = stashed.replace(f"\x00TAG{idx}\x00", tag)

        balanced = cls.balance_telegram_html(stashed, strip_tg_links=True)
        balanced = re.sub(r"\n{3,}", "\n\n", balanced)
        return balanced.strip()
