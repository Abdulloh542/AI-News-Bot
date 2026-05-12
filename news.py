"""
news.py — RSS + Gemini AI news pipeline (optimised for speed & reliability).

Architecture:
  • 11 diverse RSS sources fetched in parallel
  • In-memory cache per language (TTL 2 h) → instant response
  • Sequential Gemini calls during prefetch (avoids rate-limit collisions)
  • Lean prompt: 15 articles, titles + 120-char summary → fast AI response
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
# RSS sources  (11 feeds — diverse tech & AI coverage)
# ─────────────────────────────────────────────────────────────────────────────

RSS_FEEDS: List[Dict] = [
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=ChatGPT+OpenAI+LLM&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=machine+learning+deep+learning&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://news.google.com/rss/search?q=AI+robotics+autonomous&hl=en&gl=US&ceid=US:en",
     "source": "Google News"},
    {"url": "https://techcrunch.com/category/artificial-intelligence/feed/",
     "source": "TechCrunch"},
    {"url": "https://www.theverge.com/rss/index.xml",
     "source": "The Verge"},
    {"url": "https://venturebeat.com/category/ai/feed/",
     "source": "VentureBeat"},
    {"url": "https://feeds.arstechnica.com/arstechnica/technology-lab",
     "source": "Ars Technica"},
    {"url": "https://www.wired.com/feed/category/artificial-intelligence/latest/rss",
     "source": "Wired"},
    {"url": "https://www.reddit.com/r/artificial/.rss",
     "source": "Reddit"},
    {"url": "https://www.reddit.com/r/MachineLearning/.rss",
     "source": "Reddit"},
]

_TIMEOUT = aiohttp.ClientTimeout(total=12)
_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# ─────────────────────────────────────────────────────────────────────────────
# Cache  { lang → {ts, msg, news} }
# ─────────────────────────────────────────────────────────────────────────────

_cache: Dict[str, dict] = {}
CACHE_TTL = 7200   # 2 hours in seconds


def get_cached(lang: str) -> Optional[Tuple[str, List, int]]:
    """Return (msg, news_list, age_minutes) if cache is fresh, else None."""
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
    "uz": "❌ Yangiliklar olinmadi. Internet aloqasini tekshiring.",
    "ru": "❌ Не удалось получить новости. Проверьте подключение.",
    "en": "❌ Failed to fetch news. Check your connection.",
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
    """Remove any stray HTML tags from AI-generated text before sending to Telegram."""
    return re.sub(r"<[^>]+>", "", text).strip()


async def _fetch_one(session: aiohttp.ClientSession, feed: Dict) -> List[Dict]:
    arts: List[Dict] = []
    url, src = feed["url"], feed["source"]
    try:
        async with session.get(url, headers=_HEADERS, timeout=_TIMEOUT) as r:
            if r.status != 200:
                logger.debug("Feed %s → %d", src, r.status)
                return arts
            raw  = await r.text(errors="replace")
            feed_data = feedparser.parse(raw)
            for entry in feed_data.entries[:10]:
                title   = _clean(entry.get("title", ""))
                summary = _clean(entry.get("summary",
                                 entry.get("description", "")))[:120]
                link    = entry.get("link", "")
                if title and link:
                    arts.append({"title": title, "summary": summary,
                                 "link": link,  "source": src})
    except asyncio.TimeoutError:
        logger.debug("Timeout: %s", src)
    except Exception as exc:
        logger.debug("Feed error %s: %s", src, exc)
    return arts


async def fetch_all_feeds() -> List[Dict]:
    """Fetch all feeds concurrently; return deduplicated article list."""
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_one(session, f) for f in RSS_FEEDS],
            return_exceptions=True,
        )
    pool: List[Dict] = []
    for r in results:
        if isinstance(r, list):
            pool.extend(r)

    seen: set  = set()
    unique: List[Dict] = []
    for art in pool:
        key = re.sub(r"\W+", "", art["title"].lower())[:50]
        if key not in seen:
            seen.add(key)
            unique.append(art)

    logger.info("Fetched %d unique articles (%d feeds)", len(unique), len(RSS_FEEDS))
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Gemini AI
# ─────────────────────────────────────────────────────────────────────────────

_MODELS = [
    "gemini-2.5-flash-lite-preview-06-17",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash-latest",
]

_SYS = (
    "You are an AI news curator. "
    "Always respond with ONLY a valid JSON array — no markdown, no extra text."
)

_gemini_client: Optional[genai.Client] = None


def _client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        key = os.getenv("GOOGLE_API_KEY", "")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY not set")
        _gemini_client = genai.Client(api_key=key)
        logger.info("Gemini client ready  models=%s", _MODELS)
    return _gemini_client


def _prompt(articles: List[Dict], lang: str) -> str:
    lang_name = LANG_FULL.get(lang, "English")
    block = ""
    for i, a in enumerate(articles[:15], 1):       # max 15 — fast prompt
        block += f"{i}. [{a['source']}] {a['title']}\n   {a['summary']}\n   {a['link']}\n\n"

    return (
        f"Pick the 7 best AI/ML news from the list. Rules:\n"
        f"1. Prefer diverse sources.\n"
        f"2. Translate title + write 2-sentence summary in {lang_name}.\n"
        f"3. Rate importance 1-5.\n"
        f"4. Keep original link and include source name.\n\n"
        f'Return ONLY JSON: [{{"title":"","summary":"","link":"","source":"","importance":5}}]\n\n'
        f"ARTICLES:\n{block}"
    )


def _parse(raw: str) -> List[Dict]:
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "",          raw).strip()
    if not raw.startswith("["):
        m = re.search(r"\[[\s\S]*?\]", raw)
        raw = m.group() if m else "[]"
    out = []
    for item in json.loads(raw):
        if not isinstance(item, dict):
            continue
        t = str(item.get("title", "")).strip()
        l = str(item.get("link",  "")).strip()
        if not t or not l:
            continue
        out.append({
            "title":      t,
            "summary":    str(item.get("summary", "")).strip(),
            "link":       l,
            "source":     str(item.get("source", "")).strip(),
            "importance": max(1, min(5, int(item.get("importance", 3)))),
        })
    return out[:7]


async def _gemini(prompt: str) -> Optional[List[Dict]]:
    """Call Gemini; try model chain on 429/404; return list or None."""
    cl   = _client()
    loop = asyncio.get_event_loop()
    cfg  = gtypes.GenerateContentConfig(
        system_instruction=_SYS,
        temperature=0.2,
        max_output_tokens=2500,
        response_mime_type="application/json",
    )

    for model in _MODELS:
        for attempt in range(2):
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda m=model: cl.models.generate_content(
                        model=m, contents=prompt, config=cfg),
                )
                result = _parse(resp.text.strip())
                logger.info("Gemini (%s) OK → %d items", model, len(result))
                return result

            except json.JSONDecodeError:
                logger.warning("JSON parse fail on %s", model)
                break

            except Exception as exc:
                s = str(exc)
                if "429" in s or "RESOURCE_EXHAUSTED" in s:
                    delay = 12
                    mm = re.search(r"retryDelay.*?(\d+)s", s)
                    if mm:
                        delay = int(mm.group(1)) + 1
                    if attempt == 0:
                        logger.warning("Rate-limit %s → wait %ds", model, delay)
                        await asyncio.sleep(delay)
                    else:
                        logger.warning("Still limited %s → next model", model)
                        break
                elif "404" in s or "NOT_FOUND" in s or "503" in s:
                    logger.warning("%s unavailable → next model", model)
                    break
                else:
                    logger.error("Gemini %s: %s", model, s[:100])
                    break

    logger.error("All Gemini models failed")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Message formatter
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(news_list: List[Dict], lang: str, age_min: Optional[int]) -> str:
    now     = datetime.now().strftime("%d.%m.%Y · %H:%M")
    header  = HDR.get(lang, HDR["en"])
    read    = READ_BTN.get(lang, "Read →")

    lines   = [f"<b>{header}</b> — {now}", ""]

    for i, item in enumerate(news_list, 1):
        stars   = "⭐" * item.get("importance", 3)
        title   = he(_safe_html(item.get("title",   "")))
        summary = he(_safe_html(item.get("summary", "")))
        link    = item.get("link", "#")
        src     = he(item.get("source",  ""))

        lines += [
            f"<b>{i}. {stars}</b>",
            f"<b>{title}</b>",
            f"📝 {summary}",
            f'📰 <i>{src}</i>  ·  <a href="{link}">{read}</a>',
            "─────────────────────────────",
            "",
        ]

    # Footer: cache age or fresh label
    if age_min:
        footer = AGE_FMT.get(lang, AGE_FMT["en"]).format(m=age_min)
    else:
        footer = {"uz": "🟢 Yangi",  "ru": "🟢 Свежие", "en": "🟢 Fresh"}.get(lang, "🟢")
    lines.append(footer)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def get_news(lang: str, force: bool = False) -> Tuple[str, List[Dict], bool]:
    """
    Returns (html_message, news_list, from_cache).
    Serves from cache when available and force=False.
    """
    if not force:
        cached = get_cached(lang)
        if cached:
            msg, news, age = cached
            # Rebuild footer with current age
            rebuilt = _fmt(news, lang, age)
            logger.info("Cache HIT %s age=%dm", lang, age)
            return rebuilt, news, True

    logger.info("Cache MISS %s — fetching…", lang)
    try:
        articles = await fetch_all_feeds()
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
    """Pre-warm cache for all languages sequentially (avoids rate limits)."""
    logger.info("Prefetch start  cache=%s", cache_status())
    try:
        articles = await fetch_all_feeds()
        if not articles:
            logger.warning("Prefetch: no articles")
            return
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
            await asyncio.sleep(2)
        logger.info("Prefetch done  cache=%s", cache_status())
    except Exception as e:
        logger.error("Prefetch global: %s", e)
