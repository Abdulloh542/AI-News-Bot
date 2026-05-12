"""
db.py — SQLite database layer for AI News Bot.

Tables:
  users      — per-user settings (language, frequency)
  sent_news  — deduplication: hashes of news sent in the last 7 days
"""

import hashlib
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "users.db")


# ─────────────────────────────────────────────────────────────────────────────
# Schema init
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they do not exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            first_name TEXT,
            language   TEXT    NOT NULL DEFAULT 'uz',
            frequency  TEXT    NOT NULL DEFAULT '1d',
            is_active  INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_news (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            news_hash TEXT    NOT NULL,
            sent_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_sent_news_lookup
        ON sent_news (user_id, news_hash, sent_at)
    """)

    conn.commit()
    conn.close()
    logger.info("Database ready: %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# User helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_user(user_id: int) -> Optional[dict]:
    """Return user row as dict, or None."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def save_user(user_id: int,
              username: Optional[str],
              first_name: Optional[str]) -> None:
    """
    Insert user with defaults if new.
    Always refreshes username / first_name.
    Does NOT touch language or frequency.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO users (user_id, username, first_name, language, frequency)
        VALUES (?, ?, ?, 'uz', '1d')
    """, (user_id, username, first_name))
    cur.execute("""
        UPDATE users
           SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP
         WHERE user_id = ?
    """, (username, first_name, user_id))
    conn.commit()
    conn.close()


def update_user_settings(user_id: int, language: str, frequency: str) -> None:
    """Persist language and frequency for a user."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
           SET language = ?, frequency = ?, updated_at = CURRENT_TIMESTAMP
         WHERE user_id = ?
    """, (language, frequency, user_id))
    conn.commit()
    conn.close()
    logger.info("Settings saved — user=%d lang=%s freq=%s", user_id, language, frequency)


def get_all_active_users() -> list:
    """Return all active users."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_active = 1")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_news_hash(title: str) -> str:
    """Short MD5 hash for a news title."""
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:16]


def _is_sent(cur: sqlite3.Cursor, user_id: int, news_hash: str) -> bool:
    cur.execute("""
        SELECT 1 FROM sent_news
         WHERE user_id = ? AND news_hash = ?
           AND sent_at > datetime('now', '-7 days')
         LIMIT 1
    """, (user_id, news_hash))
    return cur.fetchone() is not None


def filter_and_mark_unsent(user_id: int, news_list: list) -> list:
    """
    From news_list, keep only items not yet sent to this user.
    Marks kept items as sent in the same transaction.
    Adds '_hash' key to each returned item.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    unsent = []
    for item in news_list:
        h = make_news_hash(item.get("title", ""))
        item = dict(item)           # copy — don't mutate caller's list
        item["_hash"] = h
        if not _is_sent(cur, user_id, h):
            unsent.append(item)
            cur.execute(
                "INSERT INTO sent_news (user_id, news_hash) VALUES (?, ?)",
                (user_id, h),
            )
    conn.commit()
    conn.close()
    return unsent


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_old_news() -> None:
    """Delete sent_news rows older than 7 days."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM sent_news WHERE sent_at < datetime('now', '-7 days')")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    logger.info("DB cleanup: removed %d stale sent_news rows", deleted)
