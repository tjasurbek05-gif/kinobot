# ============================================================
#  config.py  –  All environment variables in one place
#  Copy .env.example → .env and fill in your values
# ============================================================

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Telegram ──────────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    # Comma-separated list of super-admin IDs that are ALWAYS
    # authorised even before the DB is seeded.
    # e.g.  SUPER_ADMINS=123456789,987654321
    SUPER_ADMINS: List[int] = field(default_factory=list)

    # ── PostgreSQL ────────────────────────────────────────────
    # Full DSN:  postgresql://user:password@host:5432/dbname
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql://kinobot:secret@localhost:5432/kinobot"
    )
    # asyncpg connection-pool limits
    DB_MIN_POOL: int = int(os.getenv("DB_MIN_POOL", 5))
    DB_MAX_POOL: int = int(os.getenv("DB_MAX_POOL", 20))

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # How long (seconds) a movie record stays in Redis cache.
    # 1 hour is a safe default – adjust to taste.
    MOVIE_CACHE_TTL: int = int(os.getenv("MOVIE_CACHE_TTL", 3600))

    # How long (seconds) a force-sub check result is cached.
    # Keep short so a user who just subscribed is unblocked quickly.
    FSUB_CACHE_TTL: int = int(os.getenv("FSUB_CACHE_TTL", 60))

    # ── Broadcasting ──────────────────────────────────────────
    # Telegram allows ~30 messages/second to different users.
    # We use 25 to stay safely under the limit.
    BROADCAST_RATE: int = int(os.getenv("BROADCAST_RATE", 25))
    # Pause (seconds) between broadcast batches.
    BROADCAST_SLEEP: float = float(os.getenv("BROADCAST_SLEEP", 1.0))

    # ── Bot username (used for share links) ───────────────────
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "your_bot")

    def __post_init__(self):
        raw = os.getenv("SUPER_ADMINS", "")
        self.SUPER_ADMINS = [int(x) for x in raw.split(",") if x.strip().isdigit()]


config = Config()
