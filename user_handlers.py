# ============================================================
#  user_handlers.py  –  /start, force-sub, movie lookup
# ============================================================

import asyncio
import logging

from aiogram import Bot, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

import models
import cache
from config import config

log = logging.getLogger(__name__)
router = Router()


# ─────────────────────────────────────────────────────────────
#  Force-subscription helper
# ─────────────────────────────────────────────────────────────

async def check_force_sub(bot: Bot, user_id: int) -> bool:
    """
    Returns True if the user may use the bot.

    Access is granted when, for EVERY mandatory channel, at least
    one of these is true:
      • user is a member / admin / creator of the channel  (any type)
      • user has a pending join-request cached in Redis    (zayafka only)

    The fsub Redis key caches a full "all channels passed" result so
    subsequent calls inside the same minute are instant.
    """
    enabled = await models.get_setting("force_sub_enabled")
    if enabled != "true":
        return True

    # Fast path: recently verified
    if await cache.cache_get_fsub(user_id):
        return True

    channels = await models.get_all_channels()
    if not channels:
        return True

    for ch in channels:
        channel_id   = ch["channel_id"]
        is_zayafka   = ch["channel_type"] == "join_request"
        passed       = False

        # 1. Live Telegram member check
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status not in ("left", "kicked", "banned"):
                passed = True
        except Exception:
            # Bot is not admin or channel unreachable → let user through
            passed = True

        # 2. For zayafka channels also accept a pending join request
        if not passed and is_zayafka:
            if await cache.cache_get_join_request(channel_id, user_id):
                passed = True

        if not passed:
            return False

    # All channels passed – cache the result
    await cache.cache_set_fsub_passed(user_id)
    return True


async def build_sub_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    """Build inline keyboard with channel links + 'Check' button."""
    channels = await models.get_all_channels()
    buttons = []
    for ch in channels:
        label = ch["channel_username"] or "📢 Kanal"
        buttons.append([InlineKeyboardButton(text=label, url=ch["invite_link"])])

    buttons.append(
        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    user = message.from_user

    # Register / update user in DB
    await models.upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )

    # Force-sub check
    if not await check_force_sub(bot, user.id):
        kb = await build_sub_keyboard(bot)
        await message.answer(
            "⚠️ Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:",
            reply_markup=kb,
        )
        return

    await message.answer(
        "👋 Assalomu alaykum, botimizga xush kelibsiz.\n"
        "✍️ Kino kodini yuboring."
    )


# ─────────────────────────────────────────────────────────────
#  "Check subscription" callback
# ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, bot: Bot):
    if await check_force_sub(bot, call.from_user.id):
        await call.message.edit_text(
            "✅ Tasdiqlandi!\n\n"
            "👋 Assalomu alaykum, botimizga xush kelibsiz.\n"
            "✍️ Kino kodini yuboring."
        )
    else:
        await call.answer(
            "❌ Hali ham barcha kanallarga a'zo emassiz!", show_alert=True
        )


# ─────────────────────────────────────────────────────────────
#  Movie code handler
#
#  KEY OPTIMISATION – Telegram file_id flow:
#  ──────────────────────────────────────────
#  1. Admin uploaded the video once → Telegram returned a file_id.
#  2. We stored that file_id in PostgreSQL (and Redis cache).
#  3. When ANY user sends a code, we just call bot.send_video(file_id=…).
#  4. Telegram routes the video from its own CDN → ~0.1 s delivery.
#  Your server sends only a tiny JSON message, NOT the actual video bytes.
# ─────────────────────────────────────────────────────────────

@router.message(F.text.regexp(r"^\d+$"))   # matches any numeric string
async def handle_movie_code(message: Message, bot: Bot):
    user = message.from_user
    code = message.text.strip()

    # Block check
    # (upsert_user keeps the record; block_user sets is_blocked=True)
    # We rely on the middleware to skip blocked users globally, but as
    # an extra safety net we also check here via the DB.

    # Force-sub check (uses Redis cache for subsequent calls)
    if not await check_force_sub(bot, user.id):
        kb = await build_sub_keyboard(bot)
        await message.answer(
            "⚠️ Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:",
            reply_markup=kb,
        )
        return

    # ── Step 1: check Redis cache ──────────────────────────────
    movie = await cache.cache_get_movie(code)

    if movie is None:
        # ── Step 2: cache miss → query PostgreSQL ──────────────
        row = await models.get_movie(code)

        if row is None:
            await message.answer("❌ Bunday kodli kino mavjud emas!")
            return

        # Populate cache for next request
        movie = dict(row)
        await cache.cache_set_movie(movie)

    # ── Step 3: send the video using file_id (instant!) ────────
    # This is the moment where Telegram's caching shines:
    # bot.send_video sends a ~200-byte JSON request to Telegram.
    # Telegram's CDN pushes the actual video bytes to the user.
    share_url = f"https://t.me/{config.BOT_USERNAME}?start={code}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♻️ Do'stlarga ulashish",
                    url=f"https://t.me/share/url?url={share_url}",
                )
            ]
        ]
    )

    caption = (
        f"#{movie['title']}\n"
        f"📁 Yuklash: {movie['download_count']}\n\n"
        f"🤖 Bizning bot: @{config.BOT_USERNAME}"
    )

    await bot.send_video(
        chat_id=message.chat.id,
        video=movie["file_id"],   # ← Telegram file_id, NOT a local path
        caption=caption,
        reply_markup=kb,
    )

    # ── Step 4: increment download counter (async, non-blocking) ─
    # We fire-and-forget so the user gets their video immediately
    # while the DB update happens in the background.
    asyncio.create_task(_increment_and_refresh_cache(code))


async def _increment_and_refresh_cache(code: str):
    """Increment DB counter and refresh Redis so caption stays accurate."""
    await models.increment_download(code)
    # Invalidate cache so the next request reads the fresh count
    await cache.cache_invalidate_movie(code)


# ─────────────────────────────────────────────────────────────
#  ChatJoinRequest handler  (zayafka channels)
#
#  When a user taps a join-request invite link Telegram fires this
#  update BEFORE any admin approves them.  We store the event in
#  Redis so check_force_sub() can grant access while the user is
#  still on the waiting list.
# ─────────────────────────────────────────────────────────────

from aiogram.types import ChatJoinRequest

@router.chat_join_request()
async def on_chat_join_request(request: ChatJoinRequest):
    """
    Fired when a user clicks a join-request (zayafka) invite link.
    Store in Redis → check_force_sub will grant access immediately.
    """
    await cache.cache_set_join_request(request.chat.id, request.from_user.id)
    log.info(
        "Join request cached: user=%s channel=%s",
        request.from_user.id, request.chat.id,
    )
