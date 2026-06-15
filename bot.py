# ============================================================
#  bot.py  –  Entry point
#
#  Start the bot:
#    pip install aiogram asyncpg redis python-dotenv
#    python bot.py
# ============================================================

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update

import models
import cache
from config import config
import user_handlers
import admin_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  Blocked-user middleware
#  Drops ALL updates from blocked users before any handler runs.
# ─────────────────────────────────────────────────────────────

from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware

class BlockedUserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        # Extract user_id from the update regardless of type
        user = None
        if hasattr(event, "from_user"):
            user = event.from_user
        elif hasattr(event, "message") and event.message:
            user = event.message.from_user

        if user:
            pool = models.get_pool()
            async with pool.acquire() as conn:
                is_blocked = await conn.fetchval(
                    "SELECT is_blocked FROM users WHERE user_id = $1", user.id
                )
            if is_blocked:
                return  # silently drop the update

        return await handler(event, data)


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

async def main():
    # ── 1. Initialise DB pool ──────────────────────────────────
    log.info("Connecting to PostgreSQL…")
    await models.create_pool()
    await models.init_db()
    log.info("PostgreSQL ready.")

    # ── 2. Initialise Redis ────────────────────────────────────
    log.info("Connecting to Redis…")
    redis_client = await cache.create_redis()
    log.info("Redis ready.")

    # ── 3. FSM storage backed by Redis ────────────────────────
    #  RedisStorage keeps admin FSM states across bot restarts.
    storage = RedisStorage(redis=redis_client)

    # ── 4. Bot & Dispatcher ────────────────────────────────────
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # ── 5. Middlewares ─────────────────────────────────────────
    dp.update.outer_middleware(BlockedUserMiddleware())

    # ── 6. Include routers ─────────────────────────────────────
    #  Order matters: admin router first so /panel is handled
    #  before the generic numeric-code handler.
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)

    # ── 7. Seed super-admins into the DB ──────────────────────
    for uid in config.SUPER_ADMINS:
        await models.add_admin(uid, added_by=uid)

    # ── 8. Start polling ───────────────────────────────────────
    log.info("Bot polling started.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
