# ============================================================
#  models.py  –  PostgreSQL schema + asyncpg helper layer
#
#  Tables:
#    users    – every Telegram user who has started the bot
#    movies   – code → file_id mapping  (the Telegram cache magic)
#    channels – force-subscription channels
#    admins   – authorised admin user_ids
#    settings – simple key/value store for global toggles
# ============================================================

import asyncpg
from config import config

# ── Module-level pool reference ───────────────────────────────
_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """
    Create and return a connection pool.
    Called once at startup in bot.py.
    """
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=config.DATABASE_URL,
        min_size=config.DB_MIN_POOL,
        max_size=config.DB_MAX_POOL,
        # If the DB is temporarily unavailable, retry up to 3 times.
        command_timeout=60,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Call create_pool() first.")
    return _pool


# ─────────────────────────────────────────────────────────────
#  DDL  –  run once to set up the schema
# ─────────────────────────────────────────────────────────────

CREATE_TABLES_SQL = """
-- Users table
CREATE TABLE IF NOT EXISTS users (
    user_id      BIGINT PRIMARY KEY,
    username     TEXT,
    full_name    TEXT,
    is_blocked   BOOLEAN     NOT NULL DEFAULT FALSE,
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Movies table
-- file_id:  Telegram's unique pointer to the video on their CDN.
-- Sending this file_id costs zero bandwidth on YOUR server and
-- Telegram delivers it to the end-user in ~0.1 s from their edge.
CREATE TABLE IF NOT EXISTS movies (
    code            TEXT        PRIMARY KEY,   -- e.g. "1001"
    title           TEXT        NOT NULL,
    file_id         TEXT        NOT NULL,      -- Telegram file_id (the magic key)
    file_unique_id  TEXT        NOT NULL,      -- for deduplication
    download_count  INT         NOT NULL DEFAULT 0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Channels table
-- channel_type: 'standard' = normal force-sub
--               'join_request' = join-request based subscription
CREATE TABLE IF NOT EXISTS channels (
    channel_id      BIGINT      PRIMARY KEY,
    channel_username TEXT,                     -- @handle (nullable for private channels)
    invite_link     TEXT,                      -- deep link shown to user
    channel_type    TEXT        NOT NULL DEFAULT 'standard',
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Admins table
CREATE TABLE IF NOT EXISTS admins (
    user_id     BIGINT      PRIMARY KEY,
    added_by    BIGINT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Settings table  (key/value for simple flags)
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Default settings
INSERT INTO settings (key, value)
VALUES
    ('force_sub_enabled', 'true'),
    ('video_upload_enabled', 'true'),
    ('verification_text', '✅ Siz muvaffaqiyatli tasdiqlandingiz!')
ON CONFLICT (key) DO NOTHING;
"""


async def init_db():
    """Create all tables if they do not exist yet."""
    async with get_pool().acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)


# ─────────────────────────────────────────────────────────────
#  User helpers
# ─────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str | None, full_name: str):
    """Register a new user or update their info on /start."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
                SET username  = EXCLUDED.username,
                    full_name = EXCLUDED.full_name
            """,
            user_id, username, full_name,
        )


async def get_all_user_ids() -> list[int]:
    """Return all non-blocked user IDs for broadcasting."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM users WHERE is_blocked = FALSE"
        )
    return [r["user_id"] for r in rows]


async def get_user_count() -> int:
    async with get_pool().acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def block_user(user_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id
        )


async def unblock_user(user_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_blocked = FALSE WHERE user_id = $1", user_id
        )


# ─────────────────────────────────────────────────────────────
#  Movie helpers
# ─────────────────────────────────────────────────────────────

async def add_movie(code: str, title: str, file_id: str, file_unique_id: str):
    """
    Persist a movie record.

    The file_id is what Telegram gave us when the admin uploaded the video.
    We NEVER store the actual video bytes – Telegram's CDN holds the file.
    All future sends will just pass this file_id back to Telegram.
    """
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO movies (code, title, file_id, file_unique_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (code) DO UPDATE
                SET title          = EXCLUDED.title,
                    file_id        = EXCLUDED.file_id,
                    file_unique_id = EXCLUDED.file_unique_id
            """,
            code, title, file_id, file_unique_id,
        )


async def get_movie(code: str) -> asyncpg.Record | None:
    """Fetch a movie row by code (no caching – use Redis wrapper above this)."""
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM movies WHERE code = $1", code
        )


async def delete_movie(code: str) -> bool:
    """Delete a movie. Returns True if a row was deleted."""
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM movies WHERE code = $1", code
        )
    return result == "DELETE 1"


async def increment_download(code: str):
    """Atomically bump the download counter."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE movies SET download_count = download_count + 1 WHERE code = $1",
            code,
        )


async def get_movie_count() -> int:
    async with get_pool().acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM movies")


# ─────────────────────────────────────────────────────────────
#  Channel helpers
# ─────────────────────────────────────────────────────────────

async def add_channel(
    channel_id: int,
    username: str | None,
    invite_link: str,
    channel_type: str = "standard",
):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels (channel_id, channel_username, invite_link, channel_type)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (channel_id) DO UPDATE
                SET channel_username = EXCLUDED.channel_username,
                    invite_link      = EXCLUDED.invite_link,
                    channel_type     = EXCLUDED.channel_type
            """,
            channel_id, username, invite_link, channel_type,
        )


async def remove_channel(channel_id: int) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM channels WHERE channel_id = $1", channel_id
        )
    return result == "DELETE 1"


async def get_all_channels() -> list[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM channels")


# ─────────────────────────────────────────────────────────────
#  Admin helpers
# ─────────────────────────────────────────────────────────────

async def add_admin(user_id: int, added_by: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admins (user_id, added_by)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id, added_by,
        )


async def remove_admin(user_id: int) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM admins WHERE user_id = $1", user_id
        )
    return result == "DELETE 1"


async def get_all_admin_ids() -> list[int]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    return [r["user_id"] for r in rows]


# ─────────────────────────────────────────────────────────────
#  Settings helpers
# ─────────────────────────────────────────────────────────────

async def get_setting(key: str) -> str | None:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT value FROM settings WHERE key = $1", key
        )


async def set_setting(key: str, value: str):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key, value,
        )
