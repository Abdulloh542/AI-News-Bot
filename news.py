"""
news.py — RSS + Gemini AI news pipeline.
Optimised for Render free tier (slow CPU/network).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from html import escape as he, unescape
from typing import Dict, List, Optional, Tuple

import aiohttp
import feedparser
from dotenv import load_dotenv
from google import genai
from google.genai import types as gtypes

load_dotenv()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RSS sources — fast, reliable feeds only
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS: List[Dict] = [
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=ChatGPT+OpenAI&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=machine+learning&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/",
     "source": "TechCrunch"},
    {"url": "https://venturebeat.com/category/ai/feed/",
     "source": "VentureBeat"},
]

_TIMEOUT = aiohttp.ClientTimeout(total=8)   # 8s max per feed
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"
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
    "uz": "🕐 {m} daqiqa oldin",
    "ru": "🕐 {m} мин. назад",
    "en": "🕐 {m} min ago",
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
# RSS helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _safe_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


async def _fetch_one(session: aiohttp.ClientSession, feed: Dict) -> List[Dict]:
    arts: List[Dict] = []
    url, src = feed["url"], feed["source"]
    try:
        async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return arts
            raw = await r.text(errors="replace")
            for entry in feedparser.parse(raw).entries[:8]:
                title = _clean(entry.get("title", ""))
                link  = entry.get("link", "")
                if title and link:
                    arts.append({"title": title, "link": link, "source": src})
    except Exception as exc:
        logger.debug("Feed %s: %s", src, exc)
    return arts


async def fetch_all_feeds() -> List[Dict]:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_one(session, f) for f in RSS_FEEDS],
            return_exceptions=True,
        )
    pool: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            pool.extend(r)

    seen: set = set()
    unique: List[Dict] = []
    for art in pool:
        key = re.sub(r"\W+", "", art["title"].lower())[:40]
        if key not in seen:
            seen.add(key)
            unique.append(art)

    logger.info("Fetched %d unique articles", len(unique))
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Gemini AI
# ─────────────────────────────────────────────────────────────────────────────

_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-2.5-flash",
]

_SYS = "You are an AI news curator. Respond ONLY with a valid JSON array, no markdown."

_gemini_client: Optional[genai.Client] = None


def _client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        key = os.getenv("GOOGLE_API_KEY", "")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY not set")
        _gemini_client = genai.Client(api_key=key)
    return _gemini_client


def _prompt(articles: List[Dict], lang: str) -> str:
    lang_name = LANG_FULL.get(lang, "English")
    block = "\n".join(
        f"{i}. [{a['source']}] {a['title']} | {a['link']}"
        for i, a in enumerate(articles[:8], 1)
    )
    return (
        f"From these articles pick 5 best AI/ML news items.\n"
        f"For each: translate title to {lang_name}, write 1-sentence summary in {lang_name}, "
        f"rate importance 1-5, keep original link and source.\n"
        f'Return ONLY JSON: [{{"title":"","summary":"","link":"","source":"","importance":3}}]\n\n'
        f"{block}"
    )


def _parse(raw: str) -> List[Dict]:
    try:
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        if not raw.startswith("["):
            m = re.search(r"\[[\s\S]*?\]", raw)
            raw = m.group() if m else "[]"
        out = []
        for item in json.loads(raw):
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
    except Exception as exc:
        logger.warning("_parse failed: %s | raw=%s", exc, raw[:100])
        return []


async def _gemini(prompt: str) -> Optional[List[Dict]]:
    cl   = _client()
    loop = asyncio.get_event_loop()
    cfg  = gtypes.GenerateContentConfig(
        system_instruction=_SYS,
        temperature=0.1,
        max_output_tokens=1000,
        response_mime_type="application/json",
    )

    for model in _MODELS:
        try:
            resp = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda m=model: cl.models.generate_content(
                        model=m, contents=prompt, config=cfg),
                ),
                timeout=25.0,
            )
            result = _parse(resp.text.strip())
            if result:
                logger.info("Gemini (%s) OK → %d items", model, len(result))
                return result
            logger.warning("Gemini (%s) returned empty list", model)
        except asyncio.TimeoutError:
            logger.warning("Gemini (%s) timeout → next model", model)
        except Exception as exc:
            s = str(exc)
            if "429" in s or "RESOURCE_EXHAUSTED" in s:
                logger.warning("Rate-limit %s → next model", model)
            elif "404" in s or "NOT_FOUND" in s:
                logger.warning("%s not found → next model", model)
            else:
                logger.error("Gemini %s: %s", model, s[:120])

    logger.error("All Gemini models failed")
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
        stars   = "⭐" * item.get("importance", 3)
        title   = he(_safe_html(item.get("title",   "")))
        summary = he(_safe_html(item.get("summary", "")))
        link    = item.get("link", "#")
        src     = he(item.get("source", ""))
        lines += [
            f"<b>{i}. {stars}</b>",
            f"<b>{title}</b>",
            f"📝 {summary}",
            f'📰 <i>{src}</i>  ·  <a href="{link}">{read}</a>',
            "─────────────────────────────",
            "",
        ]

    if age_min:
        footer = AGE_FMT.get(lang, AGE_FMT["en"]).format(m=age_min)
    else:
        footer = {"uz": "🟢 Yangi", "ru": "🟢 Свежие", "en": "🟢 Fresh"}.get(lang, "🟢")
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
        articles = await asyncio.wait_for(fetch_all_feeds(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.error("fetch_all_feeds timeout")
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False
    except Exception as e:
        logger.error("fetch_all_feeds: %s", e)
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False

    if not articles:
        return ERR_FETCH.get(lang, ERR_FETCH["en"]), [], False

    news_list = await _gemini(_prompt(articles, lang))
    if not news_list:
        return ERR_AI.get(lang, ERR_AI["en"]), [], False

    msg = _fmt(news_list, lang, 0)
    set_cache(lang, msg, news_list)
    return msg, news_list, False


async def prefetch_all() -> None:
    logger.info("Prefetch start  cache=%s", cache_status())
    try:
        articles = await asyncio.wait_for(fetch_all_feeds(), timeout=15.0)
        if not articles:
            logger.warning("Prefetch: no articles"); return
        for lang in ("uz", "ru", "en"):
            try:
                news = await _gemini(_prompt(articles, lang))
                if news:
                    set_cache(lang, _fmt(news, lang, 0), news)
                    logger.info("Prefetch OK %s (%d)", lang, len(news))
                else:
                    logger.warning("Prefetch FAIL %s", lang)
            except Exception as e:
                logger.error("Prefetch %s: %s", lang, e)
            await asyncio.sleep(3)
        logger.info("Prefetch done  cache=%s", cache_status())
    except Exception as e:
        logger.error("Prefetch global: %s", e)
