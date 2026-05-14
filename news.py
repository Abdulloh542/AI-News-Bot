"""
news.py — RSS + Multi-AI news pipeline.
Sources : 18 RSS feeds + Twitter/Nitter (best-effort)
AI      : Google Gemini + Groq (up to 3 backends in parallel)
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from html import escape as he, unescape
from typing import Dict, List, Optional, Tuple

import aiohttp
import feedparser
from dotenv import load_dotenv
from google import genai
from google.genai import types as gtypes
from groq import AsyncGroq

load_dotenv()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RSS sources — 18 diverse feeds
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS: List[Dict] = [
    # Google News targeted
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence+breakthrough&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=large+language+model+LLM+GPT&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},

    # Top AI tech media
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/",    "source": "TechCrunch"},
    {"url": "https://venturebeat.com/category/ai/feed/",                         "source": "VentureBeat"},
    {"url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "source": "The Verge"},
    {"url": "https://arstechnica.com/tag/ai/feed/",                              "source": "Ars Technica"},
    {"url": "https://www.wired.com/feed/tag/ai/latest/rss",                      "source": "Wired"},
    {"url": "https://www.technologyreview.com/feed/",                            "source": "MIT Tech Review"},

    # AI Lab official blogs (primary sources)
    {"url": "https://openai.com/blog/rss.xml",          "source": "OpenAI Blog"},
    {"url": "https://www.anthropic.com/rss.xml",         "source": "Anthropic"},
    {"url": "https://huggingface.co/blog/feed.xml",      "source": "HuggingFace"},
    {"url": "https://deepmind.google/blog/rss.xml",      "source": "DeepMind"},

    # Reddit AI communities
    {"url": "https://www.reddit.com/r/MachineLearning/.rss?sort=hot&limit=15", "source": "Reddit r/ML"},
    {"url": "https://www.reddit.com/r/artificial/.rss?sort=hot&limit=15",      "source": "Reddit r/AI"},
    {"url": "https://www.reddit.com/r/OpenAI/.rss?sort=hot&limit=15",          "source": "Reddit r/OpenAI"},
    {"url": "https://www.reddit.com/r/LocalLLaMA/.rss?sort=hot&limit=15",      "source": "Reddit r/LLaMA"},

    # HackerNews — popular AI posts
    {"url": "https://hnrss.org/newest?q=AI+LLM+machine+learning&points=30",       "source": "HackerNews"},
    {"url": "https://hnrss.org/newest?q=OpenAI+Anthropic+Gemini+Claude&points=30", "source": "HackerNews"},
]

# Twitter/X via Nitter — best-effort (short timeout, gracefully skipped if down)
NITTER_FEEDS: List[Dict] = [
    {"url": "https://nitter.poast.org/OpenAI/rss",         "source": "𝕏 @OpenAI"},
    {"url": "https://nitter.poast.org/AnthropicAI/rss",    "source": "𝕏 @Anthropic"},
    {"url": "https://nitter.poast.org/GoogleDeepMind/rss",  "source": "𝕏 @DeepMind"},
    {"url": "https://nitter.poast.org/sama/rss",            "source": "𝕏 @Altman"},
    {"url": "https://nitter.poast.org/ylecun/rss",          "source": "𝕏 @LeCun"},
    {"url": "https://nitter.poast.org/karpathy/rss",        "source": "𝕏 @Karpathy"},
]

_TIMEOUT_MAIN   = aiohttp.ClientTimeout(total=8)
_TIMEOUT_NITTER = aiohttp.ClientTimeout(total=4)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AINewsBot/2.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

_cache: Dict[str, dict] = {}
CACHE_TTL = 7200  # 2 hours


def get_cached(lang: str) -> Optional[Tuple[str, List, int]]:
    e = _cache.get(lang)
    if not e:
        return None
    age = int(time.time() - e["ts"])
    if age > CACHE_TTL:
        return None
    return e["msg"], e["news"], age // 60


def set_cache(lang: str, msg: str, news: List) -> None:
    _cache[lang] = {"ts": time.time(), "msg": msg, "news": list(news)}


def invalidate_cache(lang: Optional[str] = None) -> None:
    if lang:
        _cache.pop(lang, None)
    else:
        _cache.clear()


def cache_status() -> str:
    lines = []
    for lang, e in _cache.items():
        age = int(time.time() - e["ts"]) // 60
        lines.append(f"{lang}:{age}m")
    return " | ".join(lines) if lines else "empty"


# ─────────────────────────────────────────────────────────────────────────────
# Text maps
# ─────────────────────────────────────────────────────────────────────────────

LANG_FULL = {"uz": "O'zbek", "ru": "Russian", "en": "English"}
READ_BTN  = {"uz": "O'qish →", "ru": "Читать →", "en": "Read →"}
HDR       = {"uz": "🔥 AI Yangiliklari", "ru": "🔥 AI Новости", "en": "🔥 AI News"}
AGE_FMT   = {
    "uz": "🕐 {m} daqiqa oldin yangilangan",
    "ru": "🕐 Обновлено {m} мин. назад",
    "en": "🕐 Updated {m} min ago",
}
ERR_FETCH = {
    "uz": "❌ Yangiliklar olinmadi. Keyinroq qayta urining.",
    "ru": "❌ Не удалось получить новости. Попробуйте позже.",
    "en": "❌ Failed to fetch news. Please try again.",
}
ERR_AI = {
    "uz": "❌ AI ishlamadi. Keyinroq qayta urining.",
    "ru": "❌ ИИ не ответил. Попробуйте позже.",
    "en": "❌ AI unavailable. Please try again later.",
}

# ─────────────────────────────────────────────────────────────────────────────
# RSS fetching
# ─────────────────────────────────────────────────────────────────────────────

_AI_KW = re.compile(
    r"\b(AI|artificial intelligence|machine learning|deep learning|neural|LLM|GPT|"
    r"Claude|Gemini|ChatGPT|OpenAI|Anthropic|DeepMind|HuggingFace|transformer|"
    r"diffusion|generative|model|agent|robot|autonom|chip|GPU|benchmark|"
    r"dataset|training|inference|RAG|fine.?tun|embeddings|multimodal)\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _safe_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


async def _fetch_one(
    session: aiohttp.ClientSession,
    feed: Dict,
    timeout: aiohttp.ClientTimeout,
    max_items: int = 12,
    ai_filter: bool = True,
) -> List[Dict]:
    arts: List[Dict] = []
    url, src = feed["url"], feed["source"]
    try:
        async with session.get(url, headers=_HEADERS, timeout=timeout) as r:
            if r.status != 200:
                logger.debug("Feed %s HTTP %d", src, r.status)
                return arts
            raw = await r.text(errors="replace")
            for entry in feedparser.parse(raw).entries[:max_items]:
                title = _clean(entry.get("title", ""))
                link  = entry.get("link", "")
                if not (title and link):
                    continue
                if ai_filter and not _AI_KW.search(title):
                    continue
                arts.append({"title": title, "link": link, "source": src})
    except asyncio.TimeoutError:
        logger.debug("Feed timeout: %s", src)
    except Exception as exc:
        logger.debug("Feed %s: %s", src, exc)
    return arts


async def fetch_all_feeds() -> List[Dict]:
    async with aiohttp.ClientSession() as session:
        main_tasks   = [_fetch_one(session, f, _TIMEOUT_MAIN,   12, ai_filter=True)  for f in RSS_FEEDS]
        nitter_tasks = [_fetch_one(session, f, _TIMEOUT_NITTER,  8, ai_filter=False) for f in NITTER_FEEDS]
        all_results  = await asyncio.gather(*main_tasks, *nitter_tasks, return_exceptions=True)

    pool: List[Dict] = []
    for r in all_results:
        if isinstance(r, list):
            pool.extend(r)

    seen: set = set()
    unique: List[Dict] = []
    for art in pool:
        key = re.sub(r"\W+", "", art["title"].lower())[:50]
        if key not in seen:
            seen.add(key)
            unique.append(art)

    logger.info("Fetched %d unique articles total", len(unique))
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Multi-backend AI  (Gemini + Groq in parallel)
# ─────────────────────────────────────────────────────────────────────────────

_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]
_GROQ_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",  # Llama 4 Scout — fast & capable
    "llama-3.1-8b-instant",                        # fallback — always available
]

_SYS = (
    "You are an elite AI news curator. "
    "Respond ONLY with a valid JSON array — no markdown, no explanation."
)

_BACKENDS: List[Dict] = []
_backends_lock = threading.Lock()


def _init_backends() -> List[Dict]:
    global _BACKENDS
    with _backends_lock:
        if _BACKENDS:
            return _BACKENDS

        gemini_key = os.getenv("GOOGLE_API_KEY", "")
        if gemini_key:
            _BACKENDS.append({
                "name":   "Gemini",
                "type":   "gemini",
                "client": genai.Client(api_key=gemini_key),
                "models": _GEMINI_MODELS,
                "sem":    asyncio.Semaphore(1),  # 1 concurrent Gemini call
            })

        groq_keys = list(filter(None, [
            os.getenv("GROQ_API_KEY",   ""),
            os.getenv("GROQ_API_KEY_2", ""),
        ]))
        for i, key in enumerate(groq_keys, 1):
            _BACKENDS.append({
                "name":   f"Groq#{i}",
                "type":   "groq",
                "key":    key,  # keep raw key for fresh client per call
                "models": _GROQ_MODELS,
                "sem":    asyncio.Semaphore(1),  # Groq free tier: 1 concurrent per key
            })

        if not _BACKENDS:
            raise RuntimeError("No AI API keys configured (GOOGLE_API_KEY or GROQ_API_KEY)")
        logger.info("AI backends ready: %s", [b["name"] for b in _BACKENDS])
        return _BACKENDS


def _get_backends() -> List[Dict]:
    return _init_backends()


def _prompt(articles: List[Dict], lang: str) -> str:
    lang_name = LANG_FULL.get(lang, "English")
    block = "\n".join(
        f"{i}. [{a['source']}] {a['title']} | {a['link']}"
        for i, a in enumerate(articles[:20], 1)
    )
    return (
        f"Curate AI/ML news for a Telegram channel in {lang_name}. "
        f"Pick the 5 most IMPRESSIVE stories — breakthroughs, major launches, shocking developments, "
        f"viral AI moments, or significant industry moves. SKIP boring routine news. "
        f"Prefer diverse sources (not all from the same site). "
        f"For each: punchy title in {lang_name} (max 10 words), "
        f"exciting one-sentence summary in {lang_name} (max 25 words), "
        f"importance 1-5 (5=revolutionary, 4=major, 3=notable). "
        f'Return ONLY a JSON array: [{{"title":"","summary":"","link":"","source":"","importance":5}}]\n\n'
        f"{block}"
    )


def _parse(raw: str) -> List[Dict]:
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()

    def _extract(text: str) -> List[Dict]:
        out = []
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else parsed.get("news", [parsed])
        except json.JSONDecodeError:
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            t = str(item.get("title", "")).strip()
            l = str(item.get("link",  "")).strip()
            if t and l:
                out.append({
                    "title":      t,
                    "summary":    str(item.get("summary", "")).strip(),
                    "link":       l,
                    "source":     str(item.get("source",  "")).strip(),
                    "importance": max(1, min(5, int(item.get("importance", 3)))),
                })
        return out[:5]

    # 1) Try as-is
    result = _extract(raw)
    if result:
        return result

    # 2) Try to find JSON array
    m = re.search(r"\[[\s\S]+\]", raw)
    if m:
        result = _extract(m.group())
        if result:
            return result

    # 3) Fix truncated JSON: cut at last complete object, close the array
    last_close = raw.rfind("},")
    if last_close > 0:
        fixed = raw[:last_close + 1] + "]"
        result = _extract(fixed)
        if result:
            logger.info("_parse: recovered %d items from truncated JSON", len(result))
            return result

    # 4) Extract individual objects via regex
    objects = re.findall(r'\{[^{}]+\}', raw, re.DOTALL)
    recovered = []
    for obj_str in objects:
        try:
            obj = json.loads(obj_str)
            t = str(obj.get("title", "")).strip()
            l = str(obj.get("link",  "")).strip()
            if t and l:
                recovered.append({
                    "title":      t,
                    "summary":    str(obj.get("summary", "")).strip(),
                    "link":       l,
                    "source":     str(obj.get("source",  "")).strip(),
                    "importance": max(1, min(5, int(obj.get("importance", 3)))),
                })
        except Exception:
            pass
    if recovered:
        logger.info("_parse: regex-recovered %d items", len(recovered))
        return recovered[:5]

    logger.warning("_parse failed | raw=%.120s", raw)
    return []


async def _call_gemini(backend: Dict, prompt: str) -> Optional[List[Dict]]:
    cl   = backend["client"]
    sem  = backend["sem"]
    loop = asyncio.get_event_loop()
    cfg  = gtypes.GenerateContentConfig(
        system_instruction=_SYS,
        temperature=0.2,
        max_output_tokens=3500,
        response_mime_type="application/json",  # forces clean JSON output
    )
    async with sem:  # 1 concurrent Gemini call — prevents truncation from parallel use
        for model in backend["models"]:
            try:
                resp = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda m=model: cl.models.generate_content(model=m, contents=prompt, config=cfg),
                    ),
                    timeout=35.0,
                )
                text = (resp.text or "").strip()
                if not text:
                    logger.warning("%s model=%s empty response", backend["name"], model)
                    continue
                result = _parse(text)
                if result:
                    logger.info("%s model=%s → %d items", backend["name"], model, len(result))
                    return result
                logger.warning("%s model=%s parse failed | raw=%.80s", backend["name"], model, text)
            except asyncio.TimeoutError:
                logger.warning("%s model=%s timeout → next model", backend["name"], model)
            except Exception as exc:
                s = str(exc)
                if "429" in s or "RESOURCE_EXHAUSTED" in s:
                    logger.warning("%s rate-limited (429) — waiting 15s → next model", backend["name"])
                    await asyncio.sleep(15)
                    break  # wait then try next model (not same model again)
                elif "503" in s or "UNAVAILABLE" in s:
                    logger.warning("%s unavailable (503) → next model", backend["name"])
                    continue
                elif "404" in s or "NOT_FOUND" in s:
                    logger.warning("%s model %s not found → next model", backend["name"], model)
                else:
                    logger.error("%s error: %s", backend["name"], s[:200])
    return None


async def _call_groq(backend: Dict, prompt: str) -> Optional[List[Dict]]:
    sem = backend["sem"]
    key = backend["key"]
    async with sem:  # Groq free tier: 1 concurrent request per key
        async with AsyncGroq(api_key=key) as cl:
            for model in backend["models"]:
                try:
                    resp = await asyncio.wait_for(
                        cl.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": _SYS},
                                {"role": "user",   "content": prompt},
                            ],
                            temperature=0.2,
                            max_tokens=3500,
                        ),
                        timeout=30.0,
                    )
                    text   = (resp.choices[0].message.content or "").strip()
                    result = _parse(text)
                    if result:
                        logger.info("%s model=%s → %d items", backend["name"], model, len(result))
                        return result
                    logger.warning("%s model=%s returned empty | raw=%.80s", backend["name"], model, text)
                except asyncio.TimeoutError:
                    logger.warning("%s model=%s timeout → next model", backend["name"], model)
                except Exception as exc:
                    s = str(exc)
                    if "429" in s or "rate_limit" in s.lower():
                        logger.warning("%s rate-limited → skip backend", backend["name"])
                        return None
                    elif "401" in s or "Invalid API Key" in s:
                        logger.warning("%s key rejected (401) → skip backend", backend["name"])
                        return None  # key invalid or quota hit, try next backend
                    elif "model_not_found" in s or "does not exist" in s or "decommission" in s:
                        logger.warning("%s model %s not found → next model", backend["name"], model)
                    else:
                        logger.error("%s error: %s", backend["name"], s[:200])
    return None


async def _ai_generate(prompt: str, preferred: int = 0) -> Optional[List[Dict]]:
    """Try preferred backend first, fall back to others on failure."""
    backends = _get_backends()
    n        = len(backends)

    for offset in range(n):
        backend = backends[(preferred + offset) % n]
        try:
            if backend["type"] == "gemini":
                result = await _call_gemini(backend, prompt)
            else:
                result = await _call_groq(backend, prompt)
            if result:
                return result
        except Exception as exc:
            logger.error("Backend %s unexpected: %s", backend["name"], exc)

    logger.error("All AI backends failed")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Message formatter
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(news_list: List[Dict], lang: str, age_min: Optional[int]) -> str:
    now    = datetime.now().strftime("%d.%m.%Y · %H:%M")
    header = HDR.get(lang, HDR["en"])
    read   = READ_BTN.get(lang, "Read →")
    lines  = [f"<b>{header}</b> — {now}", ""]

    for i, item in enumerate(news_list, 1):
        imp   = item.get("importance", 3)
        badge = {5: "🚀", 4: "⭐", 3: "📌", 2: "📎", 1: "•"}.get(imp, "📌")
        title   = he(_safe_html(item.get("title",   "")))
        summary = he(_safe_html(item.get("summary", "")))
        link    = item.get("link", "#")
        src     = he(item.get("source", ""))
        lines += [
            f"<b>{i}. {badge}</b>",
            f"<b>{title}</b>",
            f"📝 {summary}",
            f'📰 <i>{src}</i>  ·  <a href="{link}">{read}</a>',
            "─────────────────────",
            "",
        ]

    if age_min:
        footer = AGE_FMT.get(lang, AGE_FMT["en"]).format(m=age_min)
    else:
        footer = {"uz": "🟢 Yangi yangiliklar!", "ru": "🟢 Свежие новости!", "en": "🟢 Fresh news!"}.get(lang, "🟢")
    lines.append(footer)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def get_news(lang: str, force: bool = False) -> Tuple[str, List[Dict], bool]:
    if not force:
        cached = get_cached(lang)
        if cached:
            msg, news, age = cached
            rebuilt = _fmt(news, lang, age)
            logger.info("Cache HIT %s age=%dm", lang, age)
            return rebuilt, news, True

    logger.info("Cache MISS %s — fetching…", lang)
    try:
        articles = await asyncio.wait_for(fetch_all_feeds(), timeout=20.0)
    except asyncio.TimeoutError:
        logger.error("fetch_all_feeds timeout")
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False
    except Exception as e:
        logger.error("fetch_all_feeds: %s", e)
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False

    if not articles:
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False

    news_list = await _ai_generate(_prompt(articles, lang))
    if not news_list:
        return ERR_AI.get(lang, ERR_AI["en"]), [], False

    msg = _fmt(news_list, lang, 0)
    set_cache(lang, msg, news_list)
    return msg, news_list, False


async def prefetch_all() -> None:
    """Parallel prefetch: uz→Gemini, ru→Groq#1, en→Groq#2 (with cross-fallback)."""
    logger.info("Prefetch start  cache=%s", cache_status())
    try:
        articles = await asyncio.wait_for(fetch_all_feeds(), timeout=20.0)
        if not articles:
            logger.warning("Prefetch: no articles"); return

        # Sequential: avoids rate-limit when using the same API key per language
        # Backends with separate semaphores handle concurrency control internally
        for i, lang in enumerate(("uz", "ru", "en")):
            try:
                news = await _ai_generate(_prompt(articles, lang), preferred=i)
                if news:
                    set_cache(lang, _fmt(news, lang, 0), news)
                    logger.info("Prefetch OK %s (%d items)", lang, len(news))
                else:
                    logger.warning("Prefetch FAIL %s", lang)
            except Exception as e:
                logger.error("Prefetch %s: %s", lang, e)
            if i < 2:
                await asyncio.sleep(3)  # small pause to avoid rate-limit bursts
        logger.info("Prefetch done  cache=%s", cache_status())
    except asyncio.TimeoutError:
        logger.error("Prefetch: feed fetch timeout")
    except Exception as e:
        logger.error("Prefetch global: %s", e)
