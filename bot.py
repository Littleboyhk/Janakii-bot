import os
import sys
import time
import asyncio
import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ChatType, ChatMemberStatus, ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

import config
import database
import metadata
import ai_parser

# Configure structured logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("MovieLibrarianBot")

# Global bot username used for deep-linking URLs in groups (https://t.me/<username>?start=dl_<id>)
BOT_USERNAME: str = "Janakii_bot"

# In-memory dictionary for user cooldowns: {user_id: last_search_timestamp}
user_last_search: Dict[int, float] = {}

# In-memory tracking of all active message IDs per private chat for comprehensive auto-deletion
# {chat_id: set([message_id, ...])}
chat_messages: Dict[int, set] = {}

def track_chat_message(chat_id: int, message_id: int) -> None:
    """Record a message ID to be automatically cleaned up for this chat."""
    if not chat_id or not message_id:
        return
    if chat_id not in chat_messages:
        chat_messages[chat_id] = set()
    chat_messages[chat_id].add(message_id)

async def delete_messages_safely(bot, chat_id: int, message_ids: List[int]) -> None:
    """
    Delete multiple messages safely using Telegram's batch deleteMessages API.
    Tolerates missing or already deleted messages without throwing errors.
    """
    if not message_ids or not chat_id:
        return
    valid_ids = sorted(list(set(mid for mid in message_ids if mid)))
    # Batch delete in chunks of 100 (Telegram API limit)
    for i in range(0, len(valid_ids), 100):
        chunk = valid_ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except Exception:
            # Fallback to individual message deletion if batch fails
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass


# ==============================================================================
# Helper Functions: Verification, Rate Limiting, and UI Builders
# ==============================================================================

async def is_user_subscribed(user_id: int, bot) -> Tuple[bool, str]:
    """
    Check if the user is a member of the required force-subscription channel.
    Returns (is_subscribed, reason):
      - (True, "ok")
      - (False, "not_joined")
      - (False, "bot_not_admin")
    """
    if not config.FORCE_SUB_CHANNEL_ID:
        return True, "ok"

    try:
        member = await bot.get_chat_member(chat_id=config.FORCE_SUB_CHANNEL_ID, user_id=user_id)
        if member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]:
            return True, "ok"
        return False, "not_joined"
    except BadRequest as e:
        err_msg = str(e)
        if "Chat not found" in err_msg or "chat not found" in err_msg:
            logger.error(
                "⚠️ Force-Sub Channel (%s) not found by Telegram! "
                "You must add your bot (@%s) as an Administrator in that channel.",
                config.FORCE_SUB_CHANNEL_ID, getattr(bot, "username", "Janakii_bot")
            )
            return False, "bot_not_admin"
        else:
            logger.warning("BadRequest during force-sub check for user %s: %s", user_id, e)
        return False, "not_joined"
    except Forbidden as e:
        logger.error(
            "Bot is not an administrator in the FORCE_SUB channel (%s)! "
            "Please add the bot as admin with invite rights. Error: %s",
            config.FORCE_SUB_CHANNEL_ID, e
        )
        return False, "bot_not_admin"
    except Exception as e:
        logger.error("Unexpected error checking subscription for user %s: %s", user_id, e)
        return False, "not_joined"


async def get_force_sub_invite_link(bot) -> str:
    """Return the join link for the force-sub channel."""
    if config.FORCE_SUB_CHANNEL_LINK:
        return config.FORCE_SUB_CHANNEL_LINK

    channel = config.FORCE_SUB_CHANNEL_ID
    if isinstance(channel, str) and channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"

    try:
        chat = await bot.get_chat(channel)
        if chat.invite_link:
            return chat.invite_link
        # Create an invite link if none exists
        invite = await bot.create_chat_invite_link(chat_id=channel, name="MovieBot Auto Invite")
        return invite.invite_link
    except Exception as e:
        logger.error("Failed to generate invite link for channel %s: %s", channel, e)
        return "https://t.me/"


def check_rate_limit(user_id: int) -> Tuple[bool, float]:
    """
    Verify if the user is within the allowed request frequency.
    Returns (is_allowed, remaining_seconds).
    """
    now = time.time()
    last_time = user_last_search.get(user_id, 0.0)
    cooldown = config.SEARCH_COOLDOWN_SECONDS
    elapsed = now - last_time

    if elapsed < cooldown:
        return False, round(cooldown - elapsed, 1)

    user_last_search[user_id] = now
    return True, 0.0


def build_search_keyboard(
    results: list,
    current_page: int,
    total_pages: int,
    available_qualities: Optional[List[str]] = None,
    active_filter: str = "ALL",
    is_group: bool = False,
    bot_username: Optional[str] = None
) -> InlineKeyboardMarkup:
    """
    Construct the inline keyboard displaying movie search results and pagination buttons.
    In Groups: Movie buttons use deep-linking URLs (https://t.me/Janakii_bot?start=dl_<id>)
               with external link indicator (↗) to deliver files securely in PM.
    In PM: Movie buttons use direct callback_data (dl:<id>) for instant in-chat delivery.
    Top row: Quality Filter Buttons (All, 4K, 1080p, 720p, etc.)
    Bottom row: Pagination (<< Prev, Page X/Y, Next >>)
    """
    username = bot_username or BOT_USERNAME
    keyboard = []

    # 1. Quality Filter Row(s) if multiple qualities exist
    if available_qualities and len(available_qualities) > 0:
        filter_buttons = []
        all_label = "🌟 All" if active_filter.upper() == "ALL" else "All"
        filter_buttons.append(
            InlineKeyboardButton(text=all_label, callback_data="qfilter:ALL")
        )
        for q in available_qualities:
            btn_label = f"✅ {q}" if q.upper() == active_filter.upper() else q
            filter_buttons.append(
                InlineKeyboardButton(text=btn_label, callback_data=f"qfilter:{q}")
            )

        # Chunk filter buttons into rows of at most 4
        for i in range(0, len(filter_buttons), 4):
            keyboard.append(filter_buttons[i : i + 4])

    # 2. Movie Result Buttons (1 per row)
    for movie in results:
        btn_text = database.format_movie_button_text(movie)
        if is_group:
            # Group mode: Deep-link URL button pointing to bot's PM
            url = f"https://t.me/{username}?start=dl_{str(movie['_id'])}"
            keyboard.append([InlineKeyboardButton(text=btn_text, url=url)])
        else:
            # PM mode: Direct callback for instant download in current chat
            callback_data = f"dl:{str(movie['_id'])}"
            keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=callback_data)])

    # 3. Pagination Navigation Row
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(
            InlineKeyboardButton(text="<< Prev", callback_data=f"page_{current_page - 1}")
        )

    # Page indicator display button (informs user of position)
    nav_buttons.append(
        InlineKeyboardButton(text=f"{current_page}/{total_pages}", callback_data="noop")
    )

    if current_page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(text="Next >>", callback_data=f"page_{current_page + 1}")
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    return InlineKeyboardMarkup(keyboard)


# ==============================================================================
# JobQueue Callbacks: Ban Prevention & Auto-Deletion
# ==============================================================================

async def auto_delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    JobQueue callback to automatically delete ALL messages related to the search and file
    delivery (file message, inline keyboard results, user query, welcome prompt) after 300s.
    Leaves the chat completely clean and spotless!
    """
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return

    mids_to_delete = set()
    if data.get("message_ids"):
        mids_to_delete.update(data["message_ids"])
    if data.get("file_msg_id"):
        mids_to_delete.add(data["file_msg_id"])
    if data.get("results_msg_id"):
        mids_to_delete.add(data["results_msg_id"])
    if data.get("search_msg_id"):
        mids_to_delete.add(data["search_msg_id"])

    # If clean_all is enabled (e.g. after file delivery self-destructs), include all tracked chat messages
    if data.get("clean_all", False):
        tracked = chat_messages.get(chat_id, set())
        mids_to_delete.update(tracked)

    logger.info("Executing auto-delete job for chat %s on %d message(s)", chat_id, len(mids_to_delete))

    if mids_to_delete:
        await delete_messages_safely(context.bot, chat_id, list(mids_to_delete))

    # Remove deleted IDs from memory tracking
    if chat_id in chat_messages:
        chat_messages[chat_id].difference_update(mids_to_delete)


# ==============================================================================
# Telegram Handlers & Delivery Engine
# ==============================================================================

async def deliver_movie_to_user(
    chat_id: int,
    user,
    movie_id_str: str,
    context: ContextTypes.DEFAULT_TYPE,
    status_reply_to_mid: Optional[int] = None
) -> None:
    """
    Core delivery engine:
    1. Checks force-subscription (offers join prompt if not subscribed).
    2. Fetches movie by ID.
    3. Delivers file via copy_message (preserving authentic caption) or send_document/send_video.
    4. Sends 5-minute self-destruct warning.
    5. Schedules 300s total chat cleanup via JobQueue.
    """
    # 1. Force-Subscription Check
    subscribed, reason = await is_user_subscribed(user.id, context.bot)
    if not subscribed:
        if reason == "bot_not_admin":
            bot_me = await context.bot.get_me()
            admin_req_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ **Bot Configuration Required**\n\n"
                    f"The bot cannot verify channel members because **@{bot_me.username}** has not been added as an **Administrator** in your channel.\n\n"
                    "👉 **To fix this:**\n"
                    f"1. Open your channel settings\n"
                    f"2. Go to **Administrators** ➔ **Add Administrator**\n"
                    f"3. Add **@{bot_me.username}**"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
            track_chat_message(chat_id, admin_req_msg.message_id)
            return

        context.user_data["pending_dl"] = movie_id_str
        invite_link = await get_force_sub_invite_link(context.bot)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Our Channel", url=invite_link)],
            [InlineKeyboardButton("🔄 I Have Joined / Verify", callback_data=f"check_fsub_dl:{movie_id_str}")]
        ])
        sub_prompt_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"👋 Hello **{user.first_name}**!\n\n"
                "⚠️ **Access Required**\n"
                "To download this movie/series, please join our official channel first.\n"
                "After joining, tap **'I Have Joined / Verify'** below to receive your file immediately!"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        track_chat_message(chat_id, sub_prompt_msg.message_id)
        return

    # 2. Retrieve movie from database
    movie = await database.get_movie_by_id(movie_id_str)
    if not movie:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Sorry, this file is no longer available in the database."
        )
        return

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="📤 Delivering file...",
        reply_to_message_id=status_reply_to_mid
    )
    sent_file_msg = None
    try:
        # Option 1: Direct copy from channel if channel_id & message_id are present
        channel_id = movie.get("channel_id")
        msg_id = movie.get("message_id")
        if channel_id and msg_id:
            try:
                # Do NOT specify caption so Telegram preserves authentic original caption!
                sent_file_msg = await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=channel_id,
                    message_id=msg_id
                )
            except Exception as e:
                logger.warning("copy_message failed (%s), attempting file_id fallback...", e)

        # Option 2: Fallback or primary delivery via Telegram File ID
        file_id = movie.get("file_id")
        if not sent_file_msg and file_id:
            fallback_caption = f"🎬 **{movie.get('title')}**\n📦 Size: `{movie.get('file_size')}`"
            try:
                sent_file_msg = await context.bot.send_document(
                    chat_id=chat_id,
                    document=file_id,
                    caption=fallback_caption,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest as e:
                logger.warning("Failed to send as document (%s), attempting fallback to send_video...", e)
                try:
                    sent_file_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=file_id,
                        caption=fallback_caption,
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as err2:
                    logger.error("Failed to send media via fallback: %s", err2)
                    await status_msg.edit_text(f"❌ Failed to send file. Error: {err2}")
                    return
            except Exception as e:
                logger.error("Failed to send file: %s", e)
                await status_msg.edit_text(f"❌ Error sending file: {e}")
                return
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if not sent_file_msg:
        return

    track_chat_message(chat_id, sent_file_msg.message_id)

    # 4. Self-destruct warning reply
    sent_warning_msg = None
    try:
        sent_warning_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ **This file will self-destruct in 5 minutes!**\n"
                "👉 *Please forward or save it to your Saved Messages immediately.*"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_to_message_id=sent_file_msg.message_id
        )
        track_chat_message(chat_id, sent_warning_msg.message_id)
    except Exception as e:
        logger.warning("Could not send warning reply: %s", e)

    # 5. Schedule auto-deletion of all related messages
    search_msg_id = context.user_data.get("last_search_msg_id")
    results_msg_id = context.user_data.get("last_results_msg_id")
    delete_delay = config.AUTO_DELETE_SECONDS

    messages_to_clean = [sent_file_msg.message_id]
    if sent_warning_msg:
        messages_to_clean.append(sent_warning_msg.message_id)
    if results_msg_id:
        messages_to_clean.append(results_msg_id)
    if search_msg_id:
        messages_to_clean.append(search_msg_id)
    for mid in context.user_data.get("messages_to_clean", []):
        messages_to_clean.append(mid)

    if context.job_queue:
        context.job_queue.run_once(
            auto_delete_job,
            when=delete_delay,
            data={
                "chat_id": chat_id,
                "message_ids": list(set(messages_to_clean)),
                "clean_all": True  # Purges all messages, search results, queries, and files!
            },
            name=f"del_{chat_id}_{sent_file_msg.message_id}"
        )
        logger.info(
            "Scheduled TOTAL chat cleanup & file self-destruct in %ss for user %s",
            delete_delay, user.id
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for /start command in private messages.
    Supports deep-linking (e.g., /start dl_<ObjectId>) from groups and external links.
    """
    if not update.effective_user or not update.effective_chat:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id

    track_chat_message(chat_id, update.message.message_id)

    # Check for deep-linking download request: /start dl_<movie_id>
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("dl_"):
            movie_id_str = arg[3:]
            await deliver_movie_to_user(
                chat_id=chat_id,
                user=user,
                movie_id_str=movie_id_str,
                context=context,
                status_reply_to_mid=update.message.message_id
            )
            return

    # Check Force-Subscription
    subscribed, reason = await is_user_subscribed(user.id, context.bot)

    if not subscribed:
        if reason == "bot_not_admin":
            bot_me = await context.bot.get_me()
            admin_req_msg = await update.message.reply_text(
                "⚠️ **Bot Configuration Required**\n\n"
                f"The bot cannot verify channel members because **@{bot_me.username}** has not been added as an **Administrator** in your channel.\n\n"
                "👉 **To fix this:**\n"
                f"1. Open your channel settings\n"
                f"2. Go to **Administrators** ➔ **Add Administrator**\n"
                f"3. Add **@{bot_me.username}**\n\n"
                "💡 *Tip: Forward any message from your channel here to check the channel ID!*",
                parse_mode=ParseMode.MARKDOWN
            )
            track_chat_message(chat_id, admin_req_msg.message_id)
            return

        invite_link = await get_force_sub_invite_link(context.bot)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Our Channel", url=invite_link)],
            [InlineKeyboardButton("🔄 I Have Joined / Verify", callback_data="check_fsub")]
        ])
        sub_prompt_msg = await update.message.reply_text(
            f"👋 Hello {user.first_name}!\n\n"
            "⚠️ **Access Required**\n"
            "To use this bot to search and download movies/series, please join our official channel first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        track_chat_message(chat_id, sub_prompt_msg.message_id)
        return

    welcome_text = (
        f"👋 Welcome **{user.first_name}** to the **Movie & Series Librarian Bot**!\n\n"
        "🎬 **How to use:**\n"
        "Simply type the name of any Movie or TV Series you are looking for.\n"
        "*(e.g., `Spider-Man`, `Inception`, `Breaking Bad`)*\n\n"
        "⚡️ **Features:**\n"
        "• Instant search across our library\n"
        "• Clean pagination for multiple results\n"
        "• High speed cloud download via Telegram\n"
        "• ⚠️ Files are protected and will self-destruct 5 minutes after delivery."
    )

    sent_welcome = await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)
    track_chat_message(chat_id, sent_welcome.message_id)

    if "messages_to_clean" not in context.user_data:
        context.user_data["messages_to_clean"] = []
    context.user_data["messages_to_clean"].extend([update.message.message_id, sent_welcome.message_id])

    # Auto-delete welcome message after AUTO_DELETE_SECONDS (300s) so it doesn't linger forever
    if context.job_queue:
        context.job_queue.run_once(
            auto_delete_job,
            when=config.AUTO_DELETE_SECONDS,
            data={
                "chat_id": chat_id,
                "message_ids": [update.message.message_id, sent_welcome.message_id],
                "clean_all": False
            },
            name=f"del_welcome_{chat_id}_{sent_welcome.message_id}"
        )


async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for /clean or /clear command.
    Immediately wipes recent chat history and old searches in the private chat.
    """
    if not update.effective_chat or not update.effective_message:
        return
    chat_id = update.effective_chat.id
    current_mid = update.effective_message.message_id

    # Gather tracked messages + recent message ID window up to current_mid
    all_mids = set(chat_messages.get(chat_id, set()))
    for m in range(max(1, current_mid - 80), current_mid + 1):
        all_mids.add(m)

    await delete_messages_safely(context.bot, chat_id, list(all_mids))
    chat_messages[chat_id] = set()

    # Send a quick confirmation that self-destructs in 3 seconds
    try:
        confirm = await context.bot.send_message(chat_id=chat_id, text="🧹 Chat cleared!")
        if context.job_queue:
            context.job_queue.run_once(
                auto_delete_job,
                when=3,
                data={"chat_id": chat_id, "message_ids": [confirm.message_id], "clean_all": False},
                name=f"del_confirm_{chat_id}_{confirm.message_id}"
            )
    except Exception:
        pass


async def execute_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query_text: str
) -> None:
    """
    Core search engine:
    Supports both Private Messages (PM) and Groups/Supergroups.
    In Groups: Results are posted with interactive quality filters, and movie buttons use
               deep-linking URLs (https://t.me/<username>?start=dl_<id>) to deliver files safely in PM.
    In PM: Full auto-cleaning and direct downloads.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    is_group = update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    track_chat_message(chat_id, update.message.message_id)

    # 0. In PM, clean up previous search results message and previous query
    if not is_group:
        old_results_mid = context.user_data.get("last_results_msg_id")
        if old_results_mid:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_results_mid)
            except Exception:
                pass

        old_search_mid = context.user_data.get("last_search_msg_id")
        if old_search_mid and old_search_mid != update.message.message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_search_mid)
            except Exception:
                pass

        if context.user_data.get("messages_to_clean"):
            welcomes = context.user_data.pop("messages_to_clean", [])
            await delete_messages_safely(context.bot, chat_id, welcomes)

    # 1. Force-Subscription Check (In PM, enforce strictly; in groups, file download checks it)
    if not is_group:
        subscribed, reason = await is_user_subscribed(user.id, context.bot)
        if not subscribed:
            if reason == "bot_not_admin":
                bot_me = await context.bot.get_me()
                cfg_msg = await update.message.reply_text(
                    "⚠️ **Bot Configuration Required**\n\n"
                    f"The bot cannot verify channel members because **@{bot_me.username}** has not been added as an **Administrator** in your channel.\n\n"
                    "👉 Please add the bot as an Admin in your channel, or forward any message from the channel here to verify the Channel ID!",
                    parse_mode=ParseMode.MARKDOWN
                )
                track_chat_message(chat_id, cfg_msg.message_id)
                return

            invite_link = await get_force_sub_invite_link(context.bot)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Our Channel", url=invite_link)],
                [InlineKeyboardButton("🔄 I Have Joined / Verify", callback_data="check_fsub")]
            ])
            sub_req_msg = await update.message.reply_text(
                "⚠️ **Subscription Required**\n\n"
                "You must join our updates channel before you can search for movies.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            track_chat_message(chat_id, sub_req_msg.message_id)
            return

    # 2. Rate Limiting Check (Anti-spam cooldown)
    allowed, wait_seconds = check_rate_limit(user.id)
    if not allowed:
        cooldown_msg = await update.message.reply_text(
            f"⏳ Please slow down! Wait **{wait_seconds}s** before searching again.",
            parse_mode=ParseMode.MARKDOWN
        )
        track_chat_message(chat_id, cooldown_msg.message_id)
        if is_group and context.job_queue:
            context.job_queue.run_once(
                auto_delete_job,
                when=10,
                data={"chat_id": chat_id, "message_ids": [update.message.message_id, cooldown_msg.message_id]}
            )
        return

    # Minimum search length
    if len(query_text) < 2:
        if not is_group:
            short_msg = await update.message.reply_text("🔍 Please enter at least 2 characters to search.")
            track_chat_message(chat_id, short_msg.message_id)
        return

    # 3. Concurrently Query MongoDB Database & Cinemeta for Netflix Metadata & Poster
    meta_task = asyncio.create_task(metadata.fetch_movie_meta(query_text))
    search_task = asyncio.create_task(
        database.search_movies(
            query=query_text,
            page=1,
            page_size=config.RESULTS_PER_PAGE,
            return_qualities=True
        )
    )

    meta, (results, total_count, total_pages, available_qualities) = await asyncio.gather(
        meta_task,
        search_task
    )

    if total_count == 0:
        # In groups: only send no-results notice if user explicitly typed a command (/search)
        is_cmd = update.message.text.startswith("/") if update.message and update.message.text else False
        if not is_group or is_cmd:
            no_res_msg = await update.message.reply_text(
                f"❌ No results found for: **{query_text}**\n\n"
                "💡 *Tips:* Check the spelling or try searching with just the primary title words.",
                parse_mode=ParseMode.MARKDOWN
            )
            track_chat_message(chat_id, no_res_msg.message_id)
            if context.job_queue:
                context.job_queue.run_once(
                    auto_delete_job,
                    when=20 if is_group else 30,
                    data={
                        "chat_id": chat_id,
                        "message_ids": [update.message.message_id, no_res_msg.message_id],
                        "clean_all": False
                    }
                )
        return

    # 4. Cache search state in user session and chat session
    context.user_data["last_search_query"] = query_text
    context.user_data["last_search_msg_id"] = update.message.message_id
    context.user_data["last_search_meta"] = meta
    context.user_data["available_qualities"] = available_qualities
    context.user_data["active_qfilter"] = "ALL"

    context.chat_data["last_search_query"] = query_text
    context.chat_data["last_search_meta"] = meta
    context.chat_data["available_qualities"] = available_qualities
    context.chat_data["active_qfilter"] = "ALL"

    # 5. Build Netflix Info Card & Adaptive Keyboard
    reply_markup = build_search_keyboard(
        results=results,
        current_page=1,
        total_pages=total_pages,
        available_qualities=available_qualities,
        active_filter="ALL",
        is_group=is_group,
        bot_username=BOT_USERNAME
    )
    card_text = metadata.format_netflix_card(
        meta=meta,
        query=query_text,
        total_count=total_count,
        current_page=1,
        total_pages=total_pages,
        active_filter="ALL"
    )

    # Send rich Markdown text card directly for instant (~0.15s) delivery without remote photo fetch lag
    sent_results_msg = await update.message.reply_text(
        text=card_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

    track_chat_message(chat_id, sent_results_msg.message_id)
    context.user_data["last_results_msg_id"] = sent_results_msg.message_id

    # Store search state for this specific card
    context.chat_data[f"search_{sent_results_msg.message_id}"] = {
        "query": query_text,
        "meta": meta,
        "qualities": available_qualities,
        "active_filter": "ALL"
    }

    # Schedule auto-deletion after AUTO_DELETE_SECONDS (300s)
    if context.job_queue:
        context.job_queue.run_once(
            auto_delete_job,
            when=config.AUTO_DELETE_SECONDS,
            data={
                "chat_id": chat_id,
                "message_ids": [update.message.message_id, sent_results_msg.message_id],
                "clean_all": False
            },
            name=f"del_search_{chat_id}_{sent_results_msg.message_id}"
        )


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process incoming text messages as search queries in PM and Groups."""
    if not update.message or not update.message.text:
        return
    query_text = update.message.text.strip()
    if query_text.startswith("/"):
        return
    await execute_search(update, context, query_text)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /search <movie name> command in PM and Groups."""
    if not update.message:
        return
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        help_msg = await update.message.reply_text(
            "🔍 **Usage:** `/search <movie or series name>`\n*(e.g., `/search Inception` or `/search Jailer`)*",
            parse_mode=ParseMode.MARKDOWN
        )
        track_chat_message(update.effective_chat.id, help_msg.message_id)
        if context.job_queue:
            context.job_queue.run_once(
                auto_delete_job,
                when=15,
                data={"chat_id": update.effective_chat.id, "message_ids": [update.message.message_id, help_msg.message_id]}
            )
        return
    await execute_search(update, context, parts[1].strip())


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline queries (@Janakii_bot <movie name>) anywhere in Telegram (channels, groups, PMs)."""
    if not update.inline_query:
        return
    query = update.inline_query.query.strip()
    if not query or len(query) < 2:
        return

    results, total_count, total_pages = await database.search_movies(
        query=query,
        page=1,
        page_size=15
    )

    articles = []
    for movie in results:
        btn_text = database.format_movie_button_text(movie)
        url = f"https://t.me/{BOT_USERNAME}?start=dl_{str(movie['_id'])}"
        title = movie.get("title", "Movie")
        quality = movie.get("quality", "")
        file_size = movie.get("file_size", "")

        caption_text = (
            f"🎬 **{title}**\n"
            f"📦 **Size:** `{file_size}`\n"
            f"💿 **Quality:** `{quality}`\n\n"
            f"👇 Click below to download securely via bot:"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📥 Download ({file_size}) ↗", url=url)]
        ])

        articles.append(
            InlineQueryResultArticle(
                id=str(movie["_id"]),
                title=f"[{file_size}] {title} {quality}".strip(),
                description=f"Size: {file_size} • Quality: {quality}",
                input_message_content=InputTextMessageContent(
                    message_text=caption_text,
                    parse_mode=ParseMode.MARKDOWN
                ),
                reply_markup=keyboard
            )
        )

    await update.inline_query.answer(articles, cache_time=30)


async def handle_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle pagination button presses (e.g. callback_data='page_2').
    Updates the existing message (photo caption or text) with the new page results.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("page_"):
        return

    try:
        target_page = int(data.split("_")[1])
    except (IndexError, ValueError):
        return

    msg_id = query.message.message_id
    stored = context.chat_data.get(f"search_{msg_id}") or {}
    search_query = stored.get("query") or context.chat_data.get("last_search_query") or context.user_data.get("last_search_query")
    if not search_query:
        await query.answer("⚠️ Search session expired. Please search again.", show_alert=True)
        return

    active_filter = stored.get("active_filter") or context.chat_data.get("active_qfilter") or context.user_data.get("active_qfilter", "ALL")
    available_qualities = stored.get("qualities") or context.chat_data.get("available_qualities") or context.user_data.get("available_qualities")

    results, total_count, total_pages, _ = await database.search_movies(
        query=search_query,
        page=target_page,
        page_size=config.RESULTS_PER_PAGE,
        quality_filter=active_filter,
        return_qualities=True
    )

    if not results:
        await query.answer("No results on this page.", show_alert=True)
        return

    is_group = query.message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
    reply_markup = build_search_keyboard(
        results=results,
        current_page=target_page,
        total_pages=total_pages,
        available_qualities=available_qualities,
        active_filter=active_filter,
        is_group=is_group,
        bot_username=BOT_USERNAME
    )
    meta = stored.get("meta") or context.chat_data.get("last_search_meta") or context.user_data.get("last_search_meta")
    card_text = metadata.format_netflix_card(
        meta=meta,
        query=search_query,
        total_count=total_count,
        current_page=target_page,
        total_pages=total_pages,
        active_filter=active_filter
    )

    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text(
                card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error("Error editing pagination message: %s", e)


async def handle_quality_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle one-touch quality filter button presses (e.g. callback_data='qfilter:1080p').
    Filters search results dynamically and refreshes the card without resetting session.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("qfilter:"):
        return

    selected_quality = data.split(":", 1)[1].strip()
    msg_id = query.message.message_id
    stored = context.chat_data.get(f"search_{msg_id}") or {}
    search_query = stored.get("query") or context.chat_data.get("last_search_query") or context.user_data.get("last_search_query")
    if not search_query:
        await query.answer("⚠️ Search session expired. Please search again.", show_alert=True)
        return

    current_active = stored.get("active_filter") or context.chat_data.get("active_qfilter") or context.user_data.get("active_qfilter", "ALL")
    if selected_quality.upper() == current_active.upper():
        return

    stored["active_filter"] = selected_quality
    context.chat_data[f"search_{msg_id}"] = stored
    context.chat_data["active_qfilter"] = selected_quality
    context.user_data["active_qfilter"] = selected_quality

    # Re-run search with quality filter on Page 1
    results, total_count, total_pages, _ = await database.search_movies(
        query=search_query,
        page=1,
        page_size=config.RESULTS_PER_PAGE,
        quality_filter=selected_quality,
        return_qualities=True
    )

    available_qualities = stored.get("qualities") or context.chat_data.get("available_qualities") or context.user_data.get("available_qualities")
    is_group = query.message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
    reply_markup = build_search_keyboard(
        results=results,
        current_page=1,
        total_pages=total_pages if total_pages > 0 else 1,
        available_qualities=available_qualities,
        active_filter=selected_quality,
        is_group=is_group,
        bot_username=BOT_USERNAME
    )
    meta = stored.get("meta") or context.chat_data.get("last_search_meta") or context.user_data.get("last_search_meta")
    card_text = metadata.format_netflix_card(
        meta=meta,
        query=search_query,
        total_count=total_count,
        current_page=1,
        total_pages=total_pages if total_pages > 0 else 1,
        active_filter=selected_quality
    )

    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            await query.edit_message_text(
                card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error("Error editing quality filter message: %s", e)


async def handle_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle direct file download request in PM (dl:<ObjectId>)."""
    query = update.callback_query
    await query.answer("Fetching file from library...", show_alert=False)

    user = update.effective_user
    chat_id = update.effective_chat.id
    callback_data = query.data

    movie_id_str = callback_data.replace("dl:", "")
    await deliver_movie_to_user(
        chat_id=chat_id,
        user=user,
        movie_id_str=movie_id_str,
        context=context,
        status_reply_to_mid=query.message.message_id if query.message else None
    )


async def handle_check_fsub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'I Have Joined / Verify' button press."""
    query = update.callback_query
    user = update.effective_user

    subscribed, reason = await is_user_subscribed(user.id, context.bot)
    if subscribed:
        await query.answer("✅ Verified!", show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass

        # Check if user had a pending download from a group deep-link
        pending_dl = None
        if query.data and query.data.startswith("check_fsub_dl:"):
            pending_dl = query.data.split("check_fsub_dl:")[1]
        elif context.user_data.get("pending_dl"):
            pending_dl = context.user_data.pop("pending_dl")

        if pending_dl:
            await deliver_movie_to_user(
                chat_id=user.id,
                user=user,
                movie_id_str=pending_dl,
                context=context
            )
        else:
            await context.bot.send_message(
                chat_id=user.id,
                text="🎉 Thank you for joining! Type any movie or TV series title to start searching."
            )
    elif reason == "bot_not_admin":
        bot_me = await context.bot.get_me()
        await query.answer(
            f"⚠️ Bot Configuration Error:\n\n"
            f"The bot (@{bot_me.username}) is not an Administrator in the channel yet!\n\n"
            f"Please go to your Channel Settings ➔ Administrators ➔ Add @{bot_me.username} as Admin.",
            show_alert=True
        )
    else:
        await query.answer("❌ You have not joined the channel yet. Please join and try again.", show_alert=True)



async def handle_channel_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Diagnostic handler: when a user forwards any post from their channel to the bot in PM,
    it inspects the forward and prints the channel's title, REAL channel ID, and bot admin status!
    """
    if not update.message:
        return
    msg = update.message
    origin_chat = None
    if getattr(msg, "forward_origin", None) and hasattr(msg.forward_origin, "chat"):
        origin_chat = msg.forward_origin.chat
    elif msg.forward_from_chat:
        origin_chat = msg.forward_from_chat

    if not origin_chat:
        return

    bot_me = await context.bot.get_me()
    is_admin = False
    admin_error = ""
    try:
        member = await context.bot.get_chat_member(chat_id=origin_chat.id, user_id=bot_me.id)
        is_admin = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        admin_error = str(e)

    if is_admin:
        admin_status = "✅ Yes"
    elif admin_error:
        admin_status = f"❌ No ({admin_error})"
    else:
        admin_status = "❌ No (Not added as Admin)"

    reply = (
        "📢 **Forwarded Channel Detected!**\n\n"
        f"• **Channel Title:** `{origin_chat.title}`\n"
        f"• **Channel ID:** `{origin_chat.id}`\n"
        f"• **Bot Is Admin:** {admin_status}\n\n"
    )
    if is_admin:
        reply += (
            f"✅ **Bot is verified as Admin in this channel!**\n"
            f"Make sure your `.env` has:\n`FORCE_SUB_CHANNEL_ID={origin_chat.id}`"
        )
    else:
        reply += (
            f"👉 **Action Needed:**\n"
            f"Open `{origin_chat.title}` settings ➔ **Administrators** ➔ **Add Administrator** ➔ Add **@{bot_me.username}**!"
        )

    await msg.reply_text(reply, parse_mode=ParseMode.MARKDOWN)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Live Real-Time Ingester:
    Automatically indexes new videos and documents as they are uploaded to the channel with AI parsing!
    """
    msg = update.channel_post
    if not msg:
        return

    media = msg.document or msg.video
    if not media:
        return

    file_name = getattr(media, "file_name", None) or msg.caption or "Untitled"
    file_size_raw = getattr(media, "file_size", 0)
    formatted_size = database.format_bytes(file_size_raw)

    # Intelligently parse metadata using Google Gemini AI (with heuristic fallback)
    parsed = await ai_parser.parse_media_metadata_ai(file_name, caption=msg.caption)
    channel_id = msg.chat.id
    message_id = msg.message_id
    file_id = media.file_id

    await database.insert_movie(
        title=parsed["title"],
        file_id=file_id,
        quality=parsed["quality"],
        file_size=formatted_size,
        season_episode=parsed.get("season_episode"),
        channel_id=channel_id,
        message_id=message_id,
        year=parsed.get("year"),
        language=parsed.get("language")
    )
    mode_tag = "🤖 AI" if parsed.get("is_ai") else "⚡ Heuristic"
    logger.info(
        "✨ [%s Ingest] Upload indexed: '%s' (Year: %s | Quality: %s | SE: %s | Lang: %s) [%s] (MsgID: %s)",
        mode_tag,
        parsed["title"],
        parsed.get("year"),
        parsed["quality"],
        parsed.get("season_episode"),
        parsed.get("language"),
        formatted_size,
        message_id
    )


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op callback for non-interactive buttons like the page counter."""
    await update.callback_query.answer()


async def post_init(application: Application) -> None:
    """
    Lifecycle hook called after the bot starts up.
    Initializes database connection and registers Telegram command suggestions.
    """
    global BOT_USERNAME
    try:
        bot_me = await application.bot.get_me()
        if bot_me and bot_me.username:
            BOT_USERNAME = bot_me.username
            logger.info("Bot verified identity: @%s", BOT_USERNAME)
    except Exception as e:
        logger.warning("Could not fetch bot identity: %s", e)

    logger.info("Initializing database connection...")
    await database.init_db()

    commands = [
        BotCommand("start", "Start the bot and search guide"),
        BotCommand("search", "Search for movies or series"),
        BotCommand("clean", "Clear chat history and old searches"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands menu registered successfully.")
    except Exception as e:
        logger.warning("Could not set bot commands menu: %s", e)

    total_movies = await database.count_movies()
    logger.info("Ready! Total movies in database: %d", total_movies)

    # Optional cloud healthcheck server for free hosting (Render, Koyeb, Railway)
    port_env = os.getenv("PORT")
    if port_env:
        try:
            port = int(port_env)
            async def handle_ping(reader, writer):
                try:
                    await reader.read(512)
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Content-Length: 15\r\n"
                        b"Connection: close\r\n\r\n"
                        b"Bot is Healthy!"
                    )
                    writer.write(response)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            await asyncio.start_server(handle_ping, "0.0.0.0", port)
            logger.info("Cloud health-check server listening on 0.0.0.0:%d", port)
        except Exception as e:
            logger.warning("Could not start cloud health-check server: %s", e)


async def post_shutdown(application: Application) -> None:
    """Lifecycle hook called when the bot is shutting down."""
    logger.info("Shutting down... closing database connection pool.")
    await database.close_db()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler to capture and log unhandled exceptions."""
    logger.error("Exception occurred while handling an update:", exc_info=context.error)


# ==============================================================================
# Main Application Entrypoint
# ==============================================================================

def main() -> None:
    """Build and run the Telegram bot."""
    config.validate_config()

    if not config.BOT_TOKEN:
        logger.critical("BOT_TOKEN is empty! Please set BOT_TOKEN in your .env file.")
        return

    logger.info("Starting Movie Librarian Bot...")

    # Build Application with JobQueue enabled
    application = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 0. Live Channel Post Ingester (Listens to new uploads in your channel)
    application.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & (filters.Document.ALL | filters.VIDEO),
            handle_channel_post
        )
    )

    # 1. Command Handlers (PM and Groups)
    application.add_handler(
        CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler("search", search_command)
    )
    application.add_handler(
        CommandHandler(["clean", "clear"], clean_command, filters=filters.ChatType.PRIVATE)
    )

    # 2. Callback Query Handlers
    application.add_handler(CallbackQueryHandler(handle_check_fsub_callback, pattern=r"^check_fsub(_dl:.+)?$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    application.add_handler(CallbackQueryHandler(handle_pagination_callback, pattern=r"^page_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_quality_filter_callback, pattern=r"^qfilter:.+$"))
    application.add_handler(CallbackQueryHandler(handle_download_callback, pattern=r"^dl:.+$"))

    # 3. Diagnostic handler for forwarded messages from channels
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.FORWARDED,
            handle_channel_forward
        )
    )

    # 4. Search Message Handler (PM and Group text messages that are not commands and not forwarded)
    application.add_handler(
        MessageHandler(
            (filters.ChatType.PRIVATE | filters.ChatType.GROUPS) & filters.TEXT & (~filters.COMMAND) & (~filters.FORWARDED),
            handle_search
        )
    )

    # 5. Inline Query Handler (supports @Janakii_bot <query> in ANY channel, group, or chat)
    application.add_handler(InlineQueryHandler(inline_search))

    # 6. Global Error Handler
    application.add_error_handler(error_handler)

    # Run the bot with polling
    logger.info("Bot is polling for updates...")
    application.run_polling(drop_pending_updates=True, bootstrap_retries=-1)



if __name__ == "__main__":
    main()
