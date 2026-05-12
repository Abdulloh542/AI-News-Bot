"""
bot.py — AI News Bot.

Reliability fixes:
  • Per-user fetch lock → button spam safe
  • ONE edit per action (no double-edit races)
  • query.answer() popup for "loading" state (instant feedback)
  • Cache hit → instant single edit, no loading screen
  • Cache miss → one loading edit, then one news edit
"""

import asyncio
import logging
import os
from datetime import timedelta, timezone, time as dt_time
from html import escape as he

from aiohttp import web

from dotenv import load_dotenv
from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, JobQueue, MessageHandler, filters,
)

import db
from news import (
    get_news, prefetch_all, invalidate_cache,
    get_cached, CACHE_TTL,
)

# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FREQ_INTERVALS = {"5h": 5*3600, "1d": 24*3600, "3d": 3*24*3600}

FREQ_LABELS = {
    "5h": {"uz": "⚡ Har 5 soatda",  "ru": "⚡ Каждые 5 ч",  "en": "⚡ Every 5h"},
    "1d": {"uz": "📅 Har kuni",      "ru": "📅 Каждый день",  "en": "📅 Daily"},
    "3d": {"uz": "🗓 Har 3 kunda",   "ru": "🗓 Каждые 3 дня", "en": "🗓 Every 3d"},
}

LANG_FLAGS = {"uz": "🇺🇿 O'zbek", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}

# Per-user pending settings (while in settings screen)
_pending: dict = {}

# Per-user fetch lock — prevents button spam from firing multiple API calls
_fetching: set = set()

# ─────────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────

def kb_main(lang: str) -> InlineKeyboardMarkup:
    lbl = {
        "uz": ("📰 Yangiliklar olish", "⚙️ Sozlamalar",  "ℹ️ Ma'lumot"),
        "ru": ("📰 Получить новости",  "⚙️ Настройки",   "ℹ️ О боте"),
        "en": ("📰 Get News",          "⚙️ Settings",    "ℹ️ About"),
    }.get(lang, ("📰 Yangiliklar olish", "⚙️ Sozlamalar", "ℹ️ Ma'lumot"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl[0], callback_data="menu_news")],
        [InlineKeyboardButton(lbl[1], callback_data="menu_settings")],
        [InlineKeyboardButton(lbl[2], callback_data="menu_info")],
    ])


def kb_news(lang: str) -> InlineKeyboardMarkup:
    rf = {"uz": "🔄 Yangilash",  "ru": "🔄 Обновить", "en": "🔄 Refresh"}.get(lang, "🔄")
    bk = {"uz": "⬅️ Menyu",     "ru": "⬅️ Меню",     "en": "⬅️ Menu"   }.get(lang, "⬅️")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(rf, callback_data="news_refresh")],
        [InlineKeyboardButton(bk, callback_data="go_home")],
    ])


def kb_settings(lang: str, freq: str) -> InlineKeyboardMarkup:
    lang_row = []
    for code, lbl in (("uz", "🇺🇿 O'zbek"), ("ru", "🇷🇺 Рус"), ("en", "🇬🇧 Eng")):
        display = f"✅ {lbl}" if code == lang else lbl
        lang_row.append(InlineKeyboardButton(display, callback_data=f"lang_{code}"))

    fs = {
        "5h": {"uz": "⚡ 5 soat", "ru": "⚡ 5 ч",    "en": "⚡ 5h"},
        "1d": {"uz": "📅 1 kun",  "ru": "📅 1 день",  "en": "📅 Daily"},
        "3d": {"uz": "🗓 3 kun",  "ru": "🗓 3 дня",   "en": "🗓 3d"},
    }
    freq_row = []
    for code in ("5h", "1d", "3d"):
        lbl     = fs[code].get(lang, fs[code]["en"])
        display = f"✅ {lbl}" if code == freq else lbl
        freq_row.append(InlineKeyboardButton(display, callback_data=f"freq_{code}"))

    save = {"uz": "💾 Saqlash",  "ru": "💾 Сохранить", "en": "💾 Save"}.get(lang, "💾")
    back = {"uz": "⬅️ Orqaga",  "ru": "⬅️ Назад",     "en": "⬅️ Back"}.get(lang, "⬅️")
    return InlineKeyboardMarkup([
        lang_row, freq_row,
        [InlineKeyboardButton(save, callback_data="settings_save"),
         InlineKeyboardButton(back, callback_data="go_home")],
    ])


def kb_back(lang: str) -> InlineKeyboardMarkup:
    lbl = {"uz": "⬅️ Menyuga", "ru": "⬅️ В меню", "en": "⬅️ Menu"}.get(lang, "⬅️")
    return InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data="go_home")]])


# ─────────────────────────────────────────────────────────────────────────────
# Texts
# ─────────────────────────────────────────────────────────────────────────────

def txt_welcome(name: str, lang: str) -> str:
    n = he(name)
    return {
        "uz": (
            f"👋 Salom, <b>{n}</b>!\n\n"
            "🤖 <b>AI News Bot</b> — eng so'nggi AI yangiliklari.\n\n"
            "📡 <b>11 manba:</b> Google News · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit va boshqalar\n\n"
            "⚡ Yangiliklar cache'dan <b>bir zumda</b> keladi!\n"
            "🔄 Cache 2 soatda yangilanadi.\n\n"
            "👇 Amalni tanlang:"
        ),
        "ru": (
            f"👋 Привет, <b>{n}</b>!\n\n"
            "🤖 <b>AI News Bot</b> — последние новости об ИИ.\n\n"
            "📡 <b>11 источников:</b> Google News · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit и другие\n\n"
            "⚡ Новости из кэша — <b>мгновенно</b>!\n"
            "🔄 Обновление кэша каждые 2 часа.\n\n"
            "👇 Выберите действие:"
        ),
        "en": (
            f"👋 Hello, <b>{n}</b>!\n\n"
            "🤖 <b>AI News Bot</b> — latest AI news.\n\n"
            "📡 <b>11 sources:</b> Google News · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit & more\n\n"
            "⚡ News served from cache <b>instantly</b>!\n"
            "🔄 Cache refreshes every 2 hours.\n\n"
            "👇 Choose an action:"
        ),
    }.get(lang, "")


def txt_settings(lang: str, sl: str, sf: str) -> str:
    ld = LANG_FLAGS.get(sl, sl)
    fd = FREQ_LABELS[sf].get(lang, sf)
    h  = {"uz": "⚙️ <b>Sozlamalar</b>", "ru": "⚙️ <b>Настройки</b>",
          "en": "⚙️ <b>Settings</b>"}.get(lang, "⚙️ Settings")
    c  = {"uz": "Hozirgi", "ru": "Сейчас", "en": "Current"}.get(lang, "Current")
    s  = {"uz": "Tanlang → <b>💾 Saqlash</b>",
          "ru": "Выберите → <b>💾 Сохранить</b>",
          "en": "Select → <b>💾 Save</b>"}.get(lang, "")
    return f"{h}\n\n🌐 {c}: <b>{ld}</b>\n⏰ {c}: <b>{fd}</b>\n\n{s}"


def txt_saved(lang: str, sl: str, sf: str) -> str:
    ld = LANG_FLAGS.get(sl, sl)
    fd = FREQ_LABELS[sf].get(lang, sf)
    return {
        "uz": f"✅ <b>Saqlandi!</b>\n\n🌐 {ld}  ·  ⏰ {fd}\n\nYangiliklar avtomatik keladi.",
        "ru": f"✅ <b>Сохранено!</b>\n\n🌐 {ld}  ·  ⏰ {fd}\n\nНовости будут приходить автоматически.",
        "en": f"✅ <b>Saved!</b>\n\n🌐 {ld}  ·  ⏰ {fd}\n\nNews will be delivered automatically.",
    }.get(lang, "")


def txt_info(lang: str) -> str:
    return {
        "uz": (
            "ℹ️ <b>AI News Bot</b>\n\n"
            "📡 <b>Manbalar (11):</b>\n"
            "Google News (×4) · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit (×2)\n\n"
            "🧠 <b>AI:</b> Gemini 2.5 Flash Lite\n"
            "⚡ <b>Cache:</b> 2 soatlik xotira — tezlik kafolati\n"
            "🔄 <b>Avtofon yangilash:</b> har 2 soatda\n\n"
            "/start — botni qayta ishga tushirish"
        ),
        "ru": (
            "ℹ️ <b>AI News Bot</b>\n\n"
            "📡 <b>Источники (11):</b>\n"
            "Google News (×4) · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit (×2)\n\n"
            "🧠 <b>ИИ:</b> Gemini 2.5 Flash Lite\n"
            "⚡ <b>Кэш:</b> 2-часовая память — гарантия скорости\n"
            "🔄 <b>Авто-обновление:</b> каждые 2 часа\n\n"
            "/start — перезапустить бота"
        ),
        "en": (
            "ℹ️ <b>AI News Bot</b>\n\n"
            "📡 <b>Sources (11):</b>\n"
            "Google News (×4) · TechCrunch · The Verge\n"
            "VentureBeat · Ars Technica · Wired · Reddit (×2)\n\n"
            "🧠 <b>AI:</b> Gemini 2.5 Flash Lite\n"
            "⚡ <b>Cache:</b> 2-hour memory — speed guarantee\n"
            "🔄 <b>Auto-refresh:</b> every 2 hours\n\n"
            "/start — restart the bot"
        ),
    }.get(lang, "")


# ─────────────────────────────────────────────────────────────────────────────
# Safe message edit
# ─────────────────────────────────────────────────────────────────────────────

async def _edit(query, text: str, kb=None) -> bool:
    """Edit message. Returns True on success, False on error."""
    try:
        await query.edit_message_text(
            text, reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except RetryAfter as exc:
        logger.warning("Telegram flood wait %ds", exc.retry_after)
        return False
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.warning("Edit BadRequest: %s", exc)
        return False
    except Exception as exc:
        logger.error("Edit error: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    db.save_user(tg.id, tg.username, tg.first_name)
    u    = db.get_user(tg.id)
    lang = u.get("language", "uz")
    freq = u.get("frequency", "1d")
    schedule_job(ctx.job_queue, tg.id, freq)
    _pending.pop(tg.id, None)
    name = tg.first_name or tg.username or "Do'stim"
    await update.message.reply_text(
        txt_welcome(name, lang), reply_markup=kb_main(lang),
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %d /start", tg.id)


# ─────────────────────────────────────────────────────────────────────────────
# Plain text → show menu
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    db.save_user(tg.id, tg.username, tg.first_name)
    u    = db.get_user(tg.id)
    lang = u.get("language", "uz")
    hint = {"uz": "👇 Menyudan foydalaning:",
            "ru": "👇 Используйте меню:",
            "en": "👇 Use the menu:"}.get(lang, "👇")
    await update.message.reply_text(hint, reply_markup=kb_main(lang),
                                    parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# Callback router
# ─────────────────────────────────────────────────────────────────────────────

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data

    db.save_user(user_id, query.from_user.username, query.from_user.first_name)
    u    = db.get_user(user_id)
    lang = u.get("language", "uz")
    freq = u.get("frequency", "1d")

    # Seed pending settings
    if data in ("menu_settings",) or data.startswith(("lang_", "freq_", "settings_")):
        _pending.setdefault(user_id, {"language": lang, "frequency": freq})
    p = _pending.get(user_id, {"language": lang, "frequency": freq})

    # ── News ──────────────────────────────────────────────────────────────
    if data in ("menu_news", "news_refresh"):
        force = (data == "news_refresh")

        # Lock: prevent concurrent fetches for same user
        if user_id in _fetching:
            await query.answer(
                {"uz": "⏳ Hali yuklanmoqda, kuting...",
                 "ru": "⏳ Уже загружается...",
                 "en": "⏳ Already loading, please wait..."}.get(lang, "⏳"),
                show_alert=True,
            )
            return

        # Check cache first (instant — no loading screen needed)
        cached = get_cached(lang)
        if cached and not force:
            await query.answer()
            msg, news, age = cached
            from news import _fmt
            rebuilt = _fmt(news, lang, age)
            await _edit(query, rebuilt, kb_news(lang))
            return

        # Cache miss or forced refresh → show loading + fetch
        loading = {
            "uz": "⏳ Yangiliklar olinmoqda...\nBiroz kuting (10-20 soniya).",
            "ru": "⏳ Загружаю новости...\nПодождите (10-20 секунд).",
            "en": "⏳ Fetching news...\nPlease wait (10-20 seconds).",
        }.get(lang, "⏳ Loading...")
        await query.answer()
        await _edit(query, loading)

        _fetching.add(user_id)
        try:
            msg, news, from_cache = await get_news(lang, force=force)
        except Exception as exc:
            logger.error("get_news error: %s", exc)
            msg = {"uz": "❌ Xatolik. Keyinroq urining.",
                   "ru": "❌ Ошибка. Попробуйте позже.",
                   "en": "❌ Error. Please try again."}.get(lang, "❌")
        finally:
            _fetching.discard(user_id)

        await _edit(query, msg, kb_news(lang))

    # ── Settings ──────────────────────────────────────────────────────────
    elif data == "menu_settings":
        await query.answer()
        await _edit(query, txt_settings(p["language"], p["language"], p["frequency"]),
                    kb_settings(p["language"], p["frequency"]))

    elif data.startswith("lang_"):
        new_lang = data[5:]
        _pending[user_id] = {"language": new_lang, "frequency": p["frequency"]}
        p = _pending[user_id]
        await query.answer()
        await _edit(query, txt_settings(p["language"], p["language"], p["frequency"]),
                    kb_settings(p["language"], p["frequency"]))

    elif data.startswith("freq_"):
        new_freq = data[5:]
        _pending[user_id] = {"language": p["language"], "frequency": new_freq}
        p = _pending[user_id]
        await query.answer()
        await _edit(query, txt_settings(p["language"], p["language"], p["frequency"]),
                    kb_settings(p["language"], p["frequency"]))

    elif data == "settings_save":
        nl, nf = p["language"], p["frequency"]
        db.update_user_settings(user_id, nl, nf)
        schedule_job(ctx.job_queue, user_id, nf)
        _pending.pop(user_id, None)
        await query.answer()
        await _edit(query, txt_saved(nl, nl, nf), kb_main(nl))

    # ── Info ──────────────────────────────────────────────────────────────
    elif data == "menu_info":
        await query.answer()
        await _edit(query, txt_info(lang), kb_back(lang))

    # ── Home ──────────────────────────────────────────────────────────────
    elif data == "go_home":
        _pending.pop(user_id, None)
        u    = db.get_user(user_id)
        lang = u.get("language", "uz")
        name = query.from_user.first_name or query.from_user.username or "Do'stim"
        await query.answer()
        await _edit(query, txt_welcome(name, lang), kb_main(lang))

    else:
        await query.answer()
        logger.warning("Unknown callback '%s' from %d", data, user_id)


# ─────────────────────────────────────────────────────────────────────────────
# Job scheduler
# ─────────────────────────────────────────────────────────────────────────────

def schedule_job(jq: JobQueue, user_id: int, freq: str) -> None:
    name = f"news_{user_id}"
    for old in jq.get_jobs_by_name(name):
        old.schedule_removal()
    interval = FREQ_INTERVALS.get(freq, FREQ_INTERVALS["1d"])
    jq.run_repeating(_job_news, interval=timedelta(seconds=interval),
                     first=timedelta(seconds=interval), name=name,
                     data={"user_id": user_id})
    logger.info("Job '%s' every %ds", name, interval)


async def _job_news(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = ctx.job.data["user_id"]
    try:
        u    = db.get_user(uid)
        if not u:
            ctx.job.schedule_removal(); return
        lang = u.get("language", "uz")
        msg, news, _  = await get_news(lang)
        unsent = db.filter_and_mark_unsent(uid, news)
        if news and not unsent:
            logger.info("All news sent already → skip user %d", uid); return
        await ctx.bot.send_message(
            uid, msg, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info("Sent scheduled news → user %d", uid)
    except Exception as exc:
        logger.error("Scheduled send user %d: %s", uid, exc)


async def _job_cache(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh all language caches in the background every 2 hours."""
    invalidate_cache()
    await prefetch_all()


async def _job_cleanup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db.cleanup_old_news()
    logger.info("DB cleanup done")


# ─────────────────────────────────────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Ishga tushirish / Запустить / Start"),
    ])
    db.init_db()

    users = db.get_all_active_users()
    for u in users:
        schedule_job(app.job_queue, u["user_id"], u.get("frequency", "1d"))
    logger.info("Re-scheduled %d users", len(users))

    # Start prefetch 10 s after boot
    app.job_queue.run_once(_job_cache, when=10, name="initial_prefetch")

    # Repeat every 2 h
    app.job_queue.run_repeating(
        _job_cache,
        interval=timedelta(seconds=CACHE_TTL),
        first=timedelta(seconds=CACHE_TTL),
        name="cache_refresh",
    )

    # Daily cleanup at 03:00 UTC
    app.job_queue.run_daily(
        _job_cleanup,
        time=dt_time(3, 0, tzinfo=timezone.utc),
        name="db_cleanup",
    )
    logger.info("Bot ready")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Health-check web server (keeps Render free tier alive)
# ─────────────────────────────────────────────────────────────────────────────

async def _health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _run_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(web.Application())
    runner.app.router.add_get("/", _health)
    runner.app.router.add_get("/health", _health)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health server on port %d", port)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN not set"); return
    if not GOOGLE_API_KEY:
        logger.critical("GOOGLE_API_KEY not set"); return

    logger.info("Starting AI News Bot…")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_text))

    # Start health-check server (for Render free tier keep-alive)
    await _run_health_server()

    logger.info("Polling — Ctrl+C to stop")
    await app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    asyncio.run(_main())
