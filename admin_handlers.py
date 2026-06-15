# ============================================================
#  admin_handlers.py  –  Full /panel with FSM states
# ============================================================

import asyncio
import html
import logging
import re
import time
from datetime import timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatMemberUpdated,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest

import models
import cache
from config import config

log = logging.getLogger(__name__)
router = Router()

# Track bot start time for uptime calculation
BOT_START_TIME = time.monotonic()


def _parse_channel_input(raw: str) -> int | None:
    """Return numeric channel ID from a string, or None if invalid."""
    try:
        return int(raw)
    except ValueError:
        return None


# Matches t.me/<...> and https://t.me/<...> links so admins can paste a
# channel link instead of hunting for its @username or numeric ID.
_TME_LINK_RE = re.compile(r"(?:https?://)?t\.me/(\+|joinchat/)?([\w-]+)/?$", re.IGNORECASE)


def _extract_chat_ref(raw: str) -> tuple[str | int | None, bool]:
    """
    Turn admin input into something bot.get_chat() can use.

    Returns (chat_ref, is_private_invite):
      • chat_ref          – "@username", numeric chat_id, or None if unusable
      • is_private_invite – True if `raw` is a private invite link
                            (t.me/+xxx or t.me/joinchat/xxx), which the
                            Bot API can never resolve directly.
    """
    m = _TME_LINK_RE.match(raw)
    if m:
        if m.group(1):  # "+xxxx" or "joinchat/xxxx"
            return None, True
        return f"@{m.group(2)}", False

    if raw.startswith("@"):
        return raw, False

    return _parse_channel_input(raw), False


def _truncate(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _channel_display(chat) -> str:
    return f"@{chat.username}" if chat.username else (chat.title or str(chat.id))


async def _resolve_target_chat(message: Message, bot: Bot):
    """
    Resolve a Chat object the admin wants to wire up for force-sub.

    Three supported inputs, in order of reliability:
      1. A forwarded post from the channel – works for PRIVATE channels
         too, since Telegram includes the source chat even without an
         @username.
      2. @username, a t.me/<username> link, or a numeric chat_id – for
         public channels / chats the bot already knows about.
      3. A private invite link (t.me/+xxx) – the Bot API can never
         resolve this, so we explain the alternatives instead.

    Returns (chat, error_message). `chat` is None on failure.
    """
    if isinstance(message.forward_origin, MessageOriginChannel):
        try:
            chat = await bot.get_chat(message.forward_origin.chat.id)
            return chat, None
        except Exception:
            return None, (
                "❌ Bot bu kanalga hali a'zo emas.\n\n"
                "Avval botni kanalga <b>admin</b> qilib qo'shing, so'ngra "
                "shu postni qayta forward qiling."
            )

    if not message.text:
        return None, "❌ Kanal postini forward qiling yoki @username / link yuboring."

    raw = message.text.strip()
    chat_ref, is_private_invite = _extract_chat_ref(raw)

    if is_private_invite:
        return None, (
            "❌ Bu shaxsiy taklif havolasi — botni shu yo'l bilan ulab bo'lmaydi.\n\n"
            "1️⃣ Botni kanalga <b>admin</b> qilib qo'shing (keyin u yuqorida "
            "tugma sifatida chiqadi)\n"
            "2️⃣ Yoki kanaldagi istalgan postni shu yerga <b>forward</b> qiling"
        )

    if chat_ref is None:
        return None, "❌ Noto'g'ri format. @username, kanal linki yoki ID yuboring."

    try:
        chat = await bot.get_chat(chat_ref)
        return chat, None
    except Exception as e:
        return None, (
            f"❌ Kanal topilmadi: {e}\n\n"
            "Bot kanalga admin qilib qo'shilganini tekshiring."
        )


async def _bot_admin_rights(bot: Bot, chat_id: int) -> tuple[bool, str | None]:
    """Make sure the bot is an admin with invite-link rights in chat_id."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        return False, f"❌ Bot bu kanalga kira olmadi: {e}"

    if member.status not in ("administrator", "creator"):
        return False, "❌ Bot kanalda admin emas. Botni admin qilib qo'shing."

    if member.status == "administrator" and not member.can_invite_users:
        return False, (
            "❌ Botga admin huquqlari ichida 'Foydalanuvchilarni taklif "
            "qilish' (Invite Users via link) ruxsatini bering."
        )

    return True, None


async def _finalize_add_channel(bot: Bot, chat_id: int, channel_type: str) -> str:
    """
    Validate permissions, (re)create the invite link and persist the
    channel. Returns a human-readable result message (✅ on success).
    """
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        return f"❌ Kanal topilmadi: {e}"

    if chat.type not in ("channel", "supergroup"):
        return "❌ Bu kanal emas. Faqat kanal yoki super-guruh qo'shish mumkin."

    ok, err = await _bot_admin_rights(bot, chat.id)
    if not ok:
        return err

    try:
        invite = await bot.create_chat_invite_link(
            chat.id, creates_join_request=(channel_type == "join_request")
        )
        invite_link = invite.invite_link
    except Exception as e:
        return f"❌ Havola yaratilmadi: {e}"

    display = _channel_display(chat)
    await models.add_channel(chat.id, display, invite_link, channel_type)
    safe_display = html.escape(display)

    if channel_type == "join_request":
        return (
            f"✅ Zayafka kanal ulandi: {safe_display}\n"
            f"🔗 So'rov havolasi: {invite_link}\n\n"
            "ℹ️ Foydalanuvchi shu havola orqali 'so'rov' yuborgani bilanoq "
            "botdan foydalana oladi. So'rovlarni kanal ichida ko'rib chiqing."
        )
    return f"✅ Kanal ulandi: {safe_display}\n🔗 {invite_link}"


def _pending_channels_kb(pending: list[dict], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"➕ {_truncate(ch['title'])}", callback_data=f"{prefix}:{ch['chat_id']}")]
        for ch in pending
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _safe_edit_text(call: CallbackQuery, text: str, reply_markup=None):
    """edit_text that silently ignores Telegram's 'message is not modified'."""
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ─────────────────────────────────────────────────────────────
#  FSM States
# ─────────────────────────────────────────────────────────────

class AdminStates(StatesGroup):
    # Movie management
    waiting_movie_code  = State()
    waiting_movie_video = State()
    waiting_delete_code = State()

    # User management
    waiting_block_id    = State()
    waiting_unblock_id  = State()
    waiting_post_msg    = State()
    waiting_forward_msg = State()
    waiting_verify_text = State()

    # Admin management
    waiting_add_admin_id    = State()
    waiting_remove_admin_id = State()

    # Channel management
    waiting_add_channel        = State()
    waiting_add_zayafka_channel = State()


# ─────────────────────────────────────────────────────────────
#  Keyboards
# ─────────────────────────────────────────────────────────────

def kb_main_panel():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Userlar"),  KeyboardButton(text="👮‍♂️ Adminlar")],
            [KeyboardButton(text="💬 Kanallar"), KeyboardButton(text="📘 Qo'llanma")],
            [KeyboardButton(text="📦 Ma'lumotlar bo'limi")],
            [KeyboardButton(text="🚪 Paneldan chiqish")],
        ],
        resize_keyboard=True,
    )


def kb_users():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Tasdiqlash matni")],
            [KeyboardButton(text="🔒 Video yuklash holati")],
            [KeyboardButton(text="🔴 Bloklash"),    KeyboardButton(text="🟢 Blokdan olish")],
            [KeyboardButton(text="✍️ Post xabar"),  KeyboardButton(text="📋 Forward xabar")],
            [KeyboardButton(text="📈 Statistika")],
            [KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True,
    )


def kb_admins():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👮‍♂️ Admin qo'shish"),
             KeyboardButton(text="👮‍♂️ Adminlikdan olish")],
            [KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True,
    )


def kb_channels():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📌 Zayafka kanal ulash")],
            [KeyboardButton(text="🔷 Kanal ulash"),
             KeyboardButton(text="🔶 Kanal uzish")],
            [KeyboardButton(text="🟩 Majburiy a'zolik")],
            [KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True,
    )


def kb_data():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Kino qo'shish")],
            [KeyboardButton(text="🗑 Kino o'chirish")],
            [KeyboardButton(text="⬅️ Orqaga")],
        ],
        resize_keyboard=True,
    )


def kb_cancel():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🟡 Orqaga")]],
        resize_keyboard=True,
    )


# ─────────────────────────────────────────────────────────────
#  Auth middleware helper
# ─────────────────────────────────────────────────────────────

async def is_admin(user_id: int) -> bool:
    if user_id in config.SUPER_ADMINS:
        return True
    admins = await models.get_all_admin_ids()
    return user_id in admins


# ─────────────────────────────────────────────────────────────
#  /panel entry point
# ─────────────────────────────────────────────────────────────

@router.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return  # silently ignore non-admins

    await state.clear()
    await message.answer(
        f"👋 Bosh sahifa.\n🆔 Admin: {message.from_user.id}",
        reply_markup=kb_main_panel(),
    )


# ─────────────────────────────────────────────────────────────
#  Exit panel
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "🚪 Paneldan chiqish")
async def exit_panel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("✅ Paneldan chiqildi.", reply_markup=ReplyKeyboardRemove())


# ─────────────────────────────────────────────────────────────
#  Back buttons  (handle at top so they work from any state)
# ─────────────────────────────────────────────────────────────

@router.message(F.text.in_({"⬅️ Orqaga", "🟡 Orqaga"}))
async def go_back(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"👋 Bosh sahifa.\n🆔 Admin: {message.from_user.id}",
        reply_markup=kb_main_panel(),
    )


# ─────────────────────────────────────────────────────────────
#  ── SECTION: Users ──
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Userlar")
async def section_users(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"👥 Userlar boshqaruv bo'limi.\n🤵 Admin: {message.from_user.id}",
        reply_markup=kb_users(),
    )


# ── Statistics ────────────────────────────────────────────────

@router.message(F.text == "📈 Statistika")
async def show_stats(message: Message):
    if not await is_admin(message.from_user.id):
        return
    user_count  = await models.get_user_count()
    movie_count = await models.get_movie_count()

    uptime_secs = int(time.monotonic() - BOT_START_TIME)
    uptime_str  = str(timedelta(seconds=uptime_secs))

    await message.answer(
        f"📊 Bot statistikasi:\n\n"
        f"👥 Barcha userlar: {user_count} ta\n"
        f"🎬 Barcha kinolar: {movie_count} ta\n\n"
        f"⏰ Uptime: {uptime_str}"
    )


# ── Verification text ─────────────────────────────────────────

@router.message(F.text == "✍️ Tasdiqlash matni")
async def ask_verify_text(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    current = await models.get_setting("verification_text") or ""
    await state.set_state(AdminStates.waiting_verify_text)
    await message.answer(
        f"Hozirgi matn:\n{current}\n\nYangi matnni yuboring:",
        reply_markup=kb_cancel(),
    )


@router.message(AdminStates.waiting_verify_text)
async def save_verify_text(message: Message, state: FSMContext):
    await models.set_setting("verification_text", message.text)
    await state.clear()
    await message.answer("✅ Tasdiqlash matni saqlandi.", reply_markup=kb_users())


# ── Video upload toggle ───────────────────────────────────────

@router.message(F.text == "🔒 Video yuklash holati")
async def toggle_video_upload(message: Message):
    if not await is_admin(message.from_user.id):
        return
    current = await models.get_setting("video_upload_enabled")
    new_val = "false" if current == "true" else "true"
    await models.set_setting("video_upload_enabled", new_val)
    status = "✅ Yoqildi" if new_val == "true" else "🔒 O'chirildi"
    await message.answer(f"Video yuklash holati: {status}")


# ── Block / Unblock user ──────────────────────────────────────

@router.message(F.text == "🔴 Bloklash")
async def ask_block_id(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_block_id)
    await message.answer("Bloklash kerak bo'lgan user ID sini yuboring:", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_block_id)
async def do_block(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Raqam yuboring.")
        return
    await models.block_user(uid)
    await cache.cache_clear_fsub(uid)
    await state.clear()
    await message.answer(f"🔴 {uid} bloklandi.", reply_markup=kb_users())


@router.message(F.text == "🟢 Blokdan olish")
async def ask_unblock_id(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_unblock_id)
    await message.answer("Blokdan olish kerak bo'lgan user ID sini yuboring:", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_unblock_id)
async def do_unblock(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Raqam yuboring.")
        return
    await models.unblock_user(uid)
    await state.clear()
    await message.answer(f"🟢 {uid} blokdan olindi.", reply_markup=kb_users())


# ─────────────────────────────────────────────────────────────
#  ── BROADCAST  (Post xabar / Forward xabar) ──
#
#  This MUST run as a background asyncio.Task so the bot
#  remains fully responsive while messages are being sent
#  to 50,000+ users.  We also handle TelegramRetryAfter
#  (FloodWait) automatically.
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "✍️ Post xabar")
async def ask_post_msg(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_post_msg)
    await message.answer("📝 Xabaringizni yuboring.", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_post_msg)
async def do_post_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await message.answer("📤 Xabar yuborilmoqda...", reply_markup=kb_users())
    # Fire-and-forget – bot continues handling other users instantly
    asyncio.create_task(
        _broadcast_copy(bot=bot, source_message=message, admin_id=message.from_user.id)
    )


@router.message(F.text == "📋 Forward xabar")
async def ask_forward_msg(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_forward_msg)
    await message.answer("📝 Xabaringizni yuboring.", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_forward_msg)
async def do_forward_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await message.answer("📤 Xabar yuborilmoqda...", reply_markup=kb_users())
    asyncio.create_task(
        _broadcast_forward(bot=bot, source_message=message, admin_id=message.from_user.id)
    )


async def _broadcast_copy(bot: Bot, source_message: Message, admin_id: int):
    """
    Copy (plain broadcast) a message to all users.

    Rate-limiting strategy:
      – Send BROADCAST_RATE messages per second (default 25).
      – On TelegramRetryAfter: sleep exactly as long as Telegram demands.
      – On TelegramForbiddenError: user blocked the bot → mark as blocked.
    """
    user_ids = await models.get_all_user_ids()
    total = len(user_ids)
    ok = failed = blocked = 0

    for i, uid in enumerate(user_ids):
        try:
            await source_message.copy_to(uid)
            ok += 1
        except TelegramRetryAfter as e:
            # Telegram says: slow down, wait e.retry_after seconds
            log.warning("FloodWait: sleeping %s s", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            # Retry once after sleeping
            try:
                await source_message.copy_to(uid)
                ok += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            # User blocked the bot
            await models.block_user(uid)
            blocked += 1
        except Exception as exc:
            log.error("Broadcast error for %s: %s", uid, exc)
            failed += 1

        # Throttle: after every BROADCAST_RATE messages, pause 1 second
        if (i + 1) % config.BROADCAST_RATE == 0:
            await asyncio.sleep(config.BROADCAST_SLEEP)

    # Report to admin
    await bot.send_message(
        admin_id,
        f"✅ Broadcast yakunlandi!\n"
        f"👥 Jami: {total}\n"
        f"✔️ Muvaffaqiyatli: {ok}\n"
        f"🚫 Bloklagan: {blocked}\n"
        f"❌ Xato: {failed}",
    )


async def _broadcast_forward(bot: Bot, source_message: Message, admin_id: int):
    """Forward (with original sender info) to all users."""
    user_ids = await models.get_all_user_ids()
    total = len(user_ids)
    ok = failed = blocked = 0

    for i, uid in enumerate(user_ids):
        try:
            await bot.forward_message(
                chat_id=uid,
                from_chat_id=source_message.chat.id,
                message_id=source_message.message_id,
            )
            ok += 1
        except TelegramRetryAfter as e:
            log.warning("FloodWait: sleeping %s s", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.forward_message(
                    chat_id=uid,
                    from_chat_id=source_message.chat.id,
                    message_id=source_message.message_id,
                )
                ok += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            await models.block_user(uid)
            blocked += 1
        except Exception as exc:
            log.error("Forward error for %s: %s", uid, exc)
            failed += 1

        if (i + 1) % config.BROADCAST_RATE == 0:
            await asyncio.sleep(config.BROADCAST_SLEEP)

    await bot.send_message(
        admin_id,
        f"✅ Forward yakunlandi!\n"
        f"👥 Jami: {total}\n"
        f"✔️ Muvaffaqiyatli: {ok}\n"
        f"🚫 Bloklagan: {blocked}\n"
        f"❌ Xato: {failed}",
    )


# ─────────────────────────────────────────────────────────────
#  ── SECTION: Admins ──
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "👮‍♂️ Adminlar")
async def section_admins(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"👮 Adminlar bo'limi.\n🆔 Admin: {message.from_user.id}",
        reply_markup=kb_admins(),
    )


@router.message(F.text == "👮‍♂️ Admin qo'shish")
async def ask_add_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_add_admin_id)
    await message.answer("Admin qilish kerak bo'lgan user ID sini yuboring:", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_add_admin_id)
async def do_add_admin(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID.")
        return
    await models.add_admin(uid, added_by=message.from_user.id)
    await state.clear()
    await message.answer(f"✅ {uid} admin qilindi.", reply_markup=kb_admins())


@router.message(F.text == "👮‍♂️ Adminlikdan olish")
async def ask_remove_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_remove_admin_id)
    await message.answer("Adminlikdan olish kerak bo'lgan ID ni yuboring:", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_remove_admin_id)
async def do_remove_admin(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID.")
        return
    removed = await models.remove_admin(uid)
    await state.clear()
    status = f"✅ {uid} adminlikdan olindi." if removed else f"⚠️ {uid} admin emas edi."
    await message.answer(status, reply_markup=kb_admins())


# ─────────────────────────────────────────────────────────────
#  ── SECTION: Channels ──
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "💬 Kanallar")
async def section_channels(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"📚 Kanallar bo'limi.\n🆔 Admin: {message.from_user.id}",
        reply_markup=kb_channels(),
    )


@router.my_chat_member()
async def on_bot_chat_member_update(update: ChatMemberUpdated, bot: Bot):
    """
    Fired whenever the bot's own membership status changes in a chat.

    When an admin adds the bot to a channel/supergroup and promotes it
    to admin, cache that chat under the admin's user_id AND immediately
    message them with one-tap buttons to wire it up — as a standard or
    a zayafka (join-request) force-sub channel. This is the only
    reliable way to onboard PRIVATE channels, since the Bot API can't
    resolve private invite links or @usernames for chats the bot
    doesn't already belong to.
    """
    if update.chat.type not in ("channel", "supergroup"):
        return
    if update.new_chat_member.status != "administrator":
        return
    actor = update.from_user
    if not actor or not await is_admin(actor.id):
        return

    await cache.cache_add_pending_channel(
        actor.id, update.chat.id, update.chat.title or str(update.chat.id), update.chat.username,
    )

    display = html.escape(update.chat.title or str(update.chat.id))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔷 Ochiq kanal sifatida ulash", callback_data=f"addch:{update.chat.id}")],
            [InlineKeyboardButton(text="📌 Zayafka (so'rov) kanal sifatida ulash", callback_data=f"zch:{update.chat.id}")],
        ]
    )
    try:
        await bot.send_message(
            actor.id,
            f"✅ Bot <b>{display}</b> kanaliga admin qilib qo'shildi.\n\n"
            "Majburiy a'zolik ro'yxatiga qaysi turda ulaymiz?",
            reply_markup=kb,
        )
    except Exception:
        log.warning("Could not notify admin %s about new channel %s", actor.id, update.chat.id)


@router.message(F.text == "🔷 Kanal ulash")
async def ask_add_channel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_add_channel)

    pending = await cache.cache_get_pending_channels(message.from_user.id)
    if pending:
        await message.answer(
            "🆕 Bot admin qilib qo'shilgan kanal(lar) topildi.\n"
            "Majburiy a'zolik ro'yxatiga qo'shish uchun tanlang:",
            reply_markup=_pending_channels_kb(pending, "addch"),
        )

    await message.answer(
        "🔷 <b>Kanal ulash</b> — ochiq kanal\n\n"
        "1️⃣ Botni kanalga <b>admin</b> qilib qo'shing\n"
        "   (kanal yuqorida tugma sifatida chiqadi)\n"
        "2️⃣ Yoki kanaldagi postni shu yerga forward qiling\n"
        "3️⃣ Yoki kanal @username yoki havolasini yuboring\n\n"
        "⚠️ Bot kanalda admin bo'lishi va 'Foydalanuvchilarni taklif "
        "qilish' huquqiga ega bo'lishi shart!",
        reply_markup=kb_cancel(),
    )


@router.message(AdminStates.waiting_add_channel)
async def do_add_channel(message: Message, state: FSMContext, bot: Bot):
    chat, err = await _resolve_target_chat(message, bot)
    if err:
        await message.answer(err)
        return

    result = await _finalize_add_channel(bot, chat.id, "standard")
    if result.startswith("✅"):
        await cache.cache_remove_pending_channel(message.from_user.id, chat.id)
        await state.clear()
        await message.answer(result, reply_markup=kb_channels())
    else:
        await message.answer(result)


@router.callback_query(F.data.startswith("addch:"))
async def cb_add_pending_channel(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(call.from_user.id):
        return
    chat_id = int(call.data.split(":", 1)[1])
    result = await _finalize_add_channel(bot, chat_id, "standard")
    if result.startswith("✅"):
        await cache.cache_remove_pending_channel(call.from_user.id, chat_id)
        await state.clear()
        await _safe_edit_text(call, result, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    else:
        # Keep the original buttons so the admin can fix permissions and retry.
        await _safe_edit_text(call, result, reply_markup=call.message.reply_markup)
    await call.answer()


@router.message(F.text == "📌 Zayafka kanal ulash")
async def ask_add_zayafka(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_add_zayafka_channel)

    pending = await cache.cache_get_pending_channels(message.from_user.id)
    if pending:
        await message.answer(
            "🆕 Bot admin qilib qo'shilgan kanal(lar) topildi.\n"
            "So'rov asosida ulash uchun tanlang:",
            reply_markup=_pending_channels_kb(pending, "zch"),
        )

    await message.answer(
        "📌 <b>Zayafka kanal ulash</b> — so'rov asosida (yopiq kanal)\n\n"
        "Bu turdagi kanalga a'zolik so'rov orqali tasdiqlanadi. "
        "Foydalanuvchi 'So'rov yuborish' tugmasini bosgani bilanoq "
        "botdan foydalana oladi — so'rovlarni keyin o'zingiz kanal "
        "ichida ko'rib chiqasiz.\n\n"
        "1️⃣ Botni yopiq kanalga <b>admin</b> qilib qo'shing\n"
        "   (kanal yuqorida tugma sifatida chiqadi)\n"
        "2️⃣ Yoki kanaldagi postni shu yerga forward qiling\n\n"
        "⚠️ Botga 'Foydalanuvchilarni taklif qilish' huquqini bering — "
        "shu orqali so'rov havolasi avtomatik yaratiladi.",
        reply_markup=kb_cancel(),
    )


@router.message(AdminStates.waiting_add_zayafka_channel)
async def do_add_zayafka(message: Message, state: FSMContext, bot: Bot):
    chat, err = await _resolve_target_chat(message, bot)
    if err:
        await message.answer(err)
        return

    result = await _finalize_add_channel(bot, chat.id, "join_request")
    if result.startswith("✅"):
        await cache.cache_remove_pending_channel(message.from_user.id, chat.id)
        await state.clear()
        await message.answer(result, reply_markup=kb_channels())
    else:
        await message.answer(result)


@router.callback_query(F.data.startswith("zch:"))
async def cb_add_pending_zayafka(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(call.from_user.id):
        return
    chat_id = int(call.data.split(":", 1)[1])
    result = await _finalize_add_channel(bot, chat_id, "join_request")
    if result.startswith("✅"):
        await cache.cache_remove_pending_channel(call.from_user.id, chat_id)
        await state.clear()
        await _safe_edit_text(call, result, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    else:
        # Keep the original buttons so the admin can fix permissions and retry.
        await _safe_edit_text(call, result, reply_markup=call.message.reply_markup)
    await call.answer()


def _build_removal_view(channels: list) -> tuple[str, InlineKeyboardMarkup]:
    if not channels:
        return "Hozircha ulangan kanal yo'q.", InlineKeyboardMarkup(inline_keyboard=[])

    lines = ["🔶 <b>Ulangan kanallar</b>", "", "Uzish uchun kerakli kanalni bosing:"]
    buttons = []
    for i, ch in enumerate(channels, start=1):
        icon = "📌" if ch["channel_type"] == "join_request" else "🔷"
        name = ch["channel_username"] or str(ch["channel_id"])
        lines.append(f"{i}. {icon} {html.escape(name)}")
        buttons.append(
            [InlineKeyboardButton(text=f"❌ {i}. {_truncate(name, 30)}", callback_data=f"rmch:{ch['channel_id']}")]
        )

    buttons.append([InlineKeyboardButton(text="✅ Yakunlash", callback_data="rmch_done")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text == "🔶 Kanal uzish")
async def ask_remove_channel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    channels = await models.get_all_channels()
    text, kb = _build_removal_view(channels)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("rmch:"))
async def cb_remove_channel(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    channel_id = int(call.data.split(":", 1)[1])
    removed = await models.remove_channel(channel_id)

    channels = await models.get_all_channels()
    text, kb = _build_removal_view(channels)
    if removed:
        text = "✅ Kanal uzildi.\n\n" + text
    await _safe_edit_text(call, text, reply_markup=kb)
    await call.answer("✅ Uzildi" if removed else "⚠️ Topilmadi")


@router.callback_query(F.data == "rmch_done")
async def cb_remove_channel_done(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return
    await _safe_edit_text(call, "✅ Yakunlandi.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    await call.answer()


@router.message(F.text == "🟩 Majburiy a'zolik")
async def toggle_fsub(message: Message):
    if not await is_admin(message.from_user.id):
        return
    current = await models.get_setting("force_sub_enabled")
    new_val = "false" if current == "true" else "true"
    await models.set_setting("force_sub_enabled", new_val)
    status = "✅ Yoqildi" if new_val == "true" else "🔴 O'chirildi"
    await message.answer(f"Majburiy a'zolik: {status}")


# ─────────────────────────────────────────────────────────────
#  ── SECTION: Data / Movies ──
# ─────────────────────────────────────────────────────────────

@router.message(F.text == "📦 Ma'lumotlar bo'limi")
async def section_data(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        f"👥 Ma'lumotlar bo'limi.\n🤵 Admin: {message.from_user.id}",
        reply_markup=kb_data(),
    )


# ── Add movie (2-step: code → video) ─────────────────────────

@router.message(F.text == "🎬 Kino qo'shish")
async def ask_movie_code(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_movie_code)
    await message.answer("✍️ Kino kodini yuboring.", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_movie_code)
async def save_movie_code(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Iltimos, kino kodini matn sifatida yuboring.")
        return
    code = message.text.strip()
    await state.update_data(movie_code=code)
    await state.set_state(AdminStates.waiting_movie_video)
    await message.answer(
        f"Kod: <b>{code}</b>\n\n"
        "Endi video faylni yuboring (yoki forward qiling).\n\n"
        "⚠️ Video bitta yuklansa, barcha foydalanuvchilarga\n"
        "Telegram CDN orqali ~0.1 soniyada yetkaziladi!",
        parse_mode="HTML",
        reply_markup=kb_cancel(),
    )


@router.message(AdminStates.waiting_movie_video, F.video)
async def save_movie_video(message: Message, state: FSMContext):
    """
    ── THE CORE OF INSTANT DELIVERY ──────────────────────────
    When admin sends the video here, Telegram gives us a file_id.
    This file_id is a permanent pointer to the video on Telegram's
    servers.  We ONLY save this ID – never the video bytes.

    Later, when 50,000 users request this movie, we call:
        bot.send_video(chat_id=uid, video=file_id)
    Telegram routes the video from their CDN directly to the user.
    Our server sends ~200 bytes of JSON, not the actual video.
    """
    data = await state.get_data()
    code = data.get("movie_code", "")

    video = message.video
    file_id        = video.file_id        # The magic pointer
    file_unique_id = video.file_unique_id
    title          = message.caption or code  # Use caption as title if provided

    await models.add_movie(code, title, file_id, file_unique_id)
    # Invalidate any stale cache entry for this code
    await cache.cache_invalidate_movie(code)

    await state.clear()
    await message.answer(
        f"✅ Kino saqlandi!\n"
        f"📌 Kod: {code}\n"
        f"🎬 Nomi: {title}\n"
        f"🆔 file_id: <code>{file_id[:40]}…</code>",
        parse_mode="HTML",
        reply_markup=kb_data(),
    )


@router.message(AdminStates.waiting_movie_video)
async def movie_video_wrong_type(message: Message):
    await message.answer("❌ Iltimos, video fayl yuboring (document emas).")


# ── Delete movie ──────────────────────────────────────────────

@router.message(F.text == "🗑 Kino o'chirish")
async def ask_delete_code(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_delete_code)
    await message.answer("✍️ Kino kodini yuboring.", reply_markup=kb_cancel())


@router.message(AdminStates.waiting_delete_code)
async def do_delete_movie(message: Message, state: FSMContext):
    code = message.text.strip()
    deleted = await models.delete_movie(code)
    if deleted:
        await cache.cache_invalidate_movie(code)
    await state.clear()
    status = f"✅ {code} kodli kino o'chirildi." if deleted else f"❌ {code} kodli kino topilmadi."
    await message.answer(status, reply_markup=kb_data())


# ── Guide ─────────────────────────────────────────────────────

@router.message(F.text == "📘 Qo'llanma")
async def show_guide(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "📘 <b>Qo'llanma</b>\n\n"
        "1️⃣ Kino qo'shish: <b>📦 Ma'lumotlar bo'limi → 🎬 Kino qo'shish</b>\n"
        "   Avval kino kodini, keyin videoni yuboring.\n\n"
        "2️⃣ Force-sub: <b>💬 Kanallar → 🔷 Kanal ulash</b> (ochiq) yoki "
        "<b>📌 Zayafka kanal ulash</b> (yopiq/so'rovli)\n"
        "   Botni kanalga admin qiling — kanal avtomatik tugma sifatida "
        "chiqadi, tugmani bosing.\n"
        "   🔶 Kanal uzish orqali ulangan kanallarni tugma bilan o'chirib "
        "tashlash mumkin.\n\n"
        "3️⃣ Broadcast: <b>👤 Userlar → ✍️ Post xabar</b>\n"
        "   Xabar fonida yuboriladi, bot bloklanmaydi.\n\n"
        "4️⃣ Telegram file_id tizimi:\n"
        "   Admin bir marta yuklaydi → Telegram CDN saqlaydi →\n"
        "   Har bir user ~0.1 s da oladi. Server bandwidth sarflanmaydi.",
        parse_mode="HTML",
    )
