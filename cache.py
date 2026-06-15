# ============================================================
#  cache.py  –  Redis caching layer
#
#  Why Redis here instead of just PostgreSQL?
#  ──────────────────────────────────────────
#  Imagine 1,000 users simultaneously type code "1001".
#  Without Redis:  1,000 DB queries hit PostgreSQL at once.
#  With Redis:     First request hits DB → result stored in RAM.
#                  Requests 2-1000 get answer from RAM in <1 ms.
#
#  Two separate caches:
#    1. movie:{code}     – full movie record as JSON
#    2. fsub:{user_id}   – "1" if user passed force-sub check
# ============================================================

import json
import datetime
import redis.asyncio as aioredis

from config import config

_redis: aioredis.Redis | None = None


async def create_redis() -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(
        config.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,    # pool size
    )
    return _redis


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised. Call create_redis() first.")
    return _redis


# ── Movie cache ───────────────────────────────────────────────

async def cache_get_movie(code: str) -> dict | None:
    """
    Return a dict with {code, title, file_id, download_count}
    or None if the key is not in cache.
    """
    data = await get_redis().get(f"movie:{code}")
    if data:
        return json.loads(data)
    return None


async def cache_set_movie(record: dict):
    """
    Store a movie record dict in Redis.
    Converts any datetime fields to ISO strings before serialising.
    """
    serialisable = {
        k: v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v
        for k, v in record.items()
    }
    await get_redis().setex(
        f"movie:{serialisable['code']}",
        config.MOVIE_CACHE_TTL,
        json.dumps(serialisable),
    )


async def cache_invalidate_movie(code: str):
    """
    Remove a movie from cache (call after add/delete in DB).
    The next request will re-populate from the fresh DB value.
    """
    await get_redis().delete(f"movie:{code}")


# ── Force-sub cache ───────────────────────────────────────────

async def cache_get_fsub(user_id: int) -> bool | None:
    """
    Returns True  → user passed check recently (cached).
    Returns None  → no cached result, must check live.
    """
    val = await get_redis().get(f"fsub:{user_id}")
    if val == "1":
        return True
    return None


async def cache_set_fsub_passed(user_id: int):
    """
    Mark that this user passed the force-sub check.
    TTL is short (60 s default) so a newly subscribed user
    doesn't have to wait long.
    """
    await get_redis().setex(
        f"fsub:{user_id}",
        config.FSUB_CACHE_TTL,
        "1",
    )


async def cache_clear_fsub(user_id: int):
    """Call this when you want to force a re-check (e.g. after channel change)."""
    await get_redis().delete(f"fsub:{user_id}")


# ── Join-request cache (zayafka channels) ─────────────────────
#
#  When a user clicks a join-request invite link, Telegram sends
#  the bot a ChatJoinRequest update BEFORE an admin approves it.
#  We store that event in Redis so the access gate can grant
#  access while the user is still on the waiting list.

_JOIN_REQUEST_TTL = 7 * 24 * 3600  # 7 days


async def cache_set_join_request(channel_id: int, user_id: int):
    """Record that user_id has a pending join request for channel_id."""
    await get_redis().setex(
        f"join_req:{channel_id}:{user_id}",
        _JOIN_REQUEST_TTL,
        "1",
    )


async def cache_get_join_request(channel_id: int, user_id: int) -> bool:
    """Return True if there is a cached pending join request."""
    val = await get_redis().get(f"join_req:{channel_id}:{user_id}")
    return val == "1"


async def cache_clear_join_request(channel_id: int, user_id: int):
    """Remove a join-request record (e.g. after user is approved or kicked)."""
    await get_redis().delete(f"join_req:{channel_id}:{user_id}")
