"""Telegram handlers: commands, approval taps, and media sent straight to the bot."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.constants import FileSizeLimit
from telegram.ext import ContextTypes

from app import db
from app.config import Profile
from app.intake import (
    Runtime,
    archive,
    enqueue_telegram_media,
    local_path_for,
    offer,
)
from app.models import ItemStatus
from app.telegram.cards import CALLBACK_SEPARATOR, Action, close_card, format_size

log = logging.getLogger(__name__)

RUNTIME = "runtime"
TEST_IMAGE = Path("assets/test.png")

OUTCOME = {
    Action.APPROVE: "✅ Approved\nThe caption step arrives in Phase 3.",
    Action.DECLINE: "❌ Declined",
    Action.LATER: "⏭ Skipped for now",
}


def _runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    return context.bot_data[RUNTIME]


def _profile_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Profile | None:
    return next(
        (p for p in _runtime(context).settings.profiles if p.telegram_chat_id == chat_id), None
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt, chat = _runtime(context), update.effective_chat
    profile = _profile_for(context, chat.id)
    if profile is None:
        return
    counts = await db.counts_by_status(rt.conn, profile.name)
    queue = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "empty"
    drive_state = "connected" if rt.drive else "not configured"
    log.info("chat %s: /start", chat.id)
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"Crosspost Engine is up.\n\n"
            f"profile: {profile.name}\n"
            f"platforms: {', '.join(profile.enabled_platforms) or 'none'}\n"
            f"drive: {drive_state}\n"
            f"queue: {queue}\n\n"
            "Send me a photo or video to queue it, or /test for a sample card."
        ),
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue the bundled test image as a real item, so the whole path gets exercised."""
    rt, chat = _runtime(context), update.effective_chat
    profile = _profile_for(context, chat.id)
    if profile is None:
        return
    if not TEST_IMAGE.is_file():
        await context.bot.send_message(chat_id=chat.id, text=f"Missing {TEST_IMAGE}.")
        return

    log.info("chat %s: /test", chat.id)
    item = await enqueue_telegram_media(
        rt, profile, filename=TEST_IMAGE.name, mime="image/png",
        size_bytes=TEST_IMAGE.stat().st_size,
    )
    if item is None:
        await context.bot.send_message(chat_id=chat.id, text="Could not queue the test image.")
        return

    destination = local_path_for(rt.settings, item)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(TEST_IMAGE.read_bytes())
    await db.set_local_path(rt.conn, item.id, destination)
    item.local_path = str(destination)
    await offer(rt, profile, item)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Media sent straight to the bot joins the same queue as a Drive file.

    There is no Drive file behind it, so it is never archive-moved. Telegram caps what a
    bot may *download* at 20 MB, well below what it may send, so anything larger is
    refused with an explanation rather than failing opaquely.
    """
    rt, chat, message = _runtime(context), update.effective_chat, update.effective_message
    profile = _profile_for(context, chat.id)
    if profile is None:
        return

    if message.photo:
        media, filename, mime = message.photo[-1], f"photo_{message.message_id}.jpg", "image/jpeg"
    elif message.video:
        media = message.video
        filename = media.file_name or f"video_{message.message_id}.mp4"
        mime = media.mime_type or "video/mp4"
    elif message.document:
        media = message.document
        filename = media.file_name or f"file_{message.message_id}"
        mime = media.mime_type or "application/octet-stream"
        if not mime.startswith(("image/", "video/")):
            await message.reply_text(f"Ignoring {filename} — only images and video are queued.")
            return
    else:
        return

    size = media.file_size or 0
    if size > FileSizeLimit.FILESIZE_DOWNLOAD:
        await message.reply_text(
            f"{filename} is {format_size(size)}. Telegram only lets a bot download up to "
            f"{format_size(FileSizeLimit.FILESIZE_DOWNLOAD)}, so I cannot fetch it. "
            "Put it in the Drive inbox instead."
        )
        return

    item = await enqueue_telegram_media(
        rt, profile, filename=filename, mime=mime, size_bytes=size
    )
    if item is None:
        await message.reply_text("Could not queue that.")
        return

    destination = local_path_for(rt.settings, item)
    destination.parent.mkdir(parents=True, exist_ok=True)
    telegram_file = await media.get_file()
    await telegram_file.download_to_drive(custom_path=destination)
    await db.set_local_path(rt.conn, item.id, destination)
    item.local_path = str(destination)
    item.size_bytes = destination.stat().st_size
    await offer(rt, profile, item)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an approval tap.

    Callback queries carry no chat filter of their own, so the allow-list is re-checked
    here rather than relying on the filter guarding the message handlers.
    """
    rt, query, chat = _runtime(context), update.callback_query, update.effective_chat
    profile = _profile_for(context, chat.id) if chat else None
    if profile is None:
        log.info("ignoring callback from non-allow-listed chat %s", chat.id if chat else "?")
        await query.answer()
        return

    action_name, _, raw_id = (query.data or "").partition(CALLBACK_SEPARATOR)
    try:
        action, item_id = Action(action_name), int(raw_id)
    except ValueError:
        log.warning("unusable callback data %r", query.data)
        await query.answer("Unknown action.")
        return

    item = await db.get_item(rt.conn, item_id)
    if item is None:
        await query.answer("That item is gone.")
        await close_card(query.message, "⚠️ Item no longer in the queue.")
        return
    if item.status is not ItemStatus.PENDING_APPROVAL:
        await query.answer("Already handled.")
        await close_card(query.message, f"⚠️ Already {item.status}.")
        return

    await query.answer()
    log.info("chat %s: %s item %d", chat.id, action.value, item.id)

    outcome = OUTCOME[action]
    if action is Action.DECLINE:
        problem = await archive(rt, item, status=ItemStatus.DECLINED)
        outcome += "\nMoved to rejected/." if problem is None else f"\n⚠️ {problem}"
    elif action is Action.LATER:
        await db.defer(rt.conn, item.id, rt.settings.later_cooldown_minutes)
        outcome += f"\nBack in about {rt.settings.later_cooldown_minutes} min."
    else:
        await db.set_status(rt.conn, item.id, ItemStatus.AWAITING_TEXT)

    await close_card(query.message, outcome)


async def log_ignored(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Updates from outside the allow-list: no reply, but the chat id is logged so a new
    chat can be onboarded without loosening the rule."""
    chat = update.effective_chat
    if chat is not None:
        log.info(
            "ignored update from non-allow-listed chat id=%s type=%s name=%r",
            chat.id,
            chat.type,
            chat.username or chat.title or chat.first_name,
        )
