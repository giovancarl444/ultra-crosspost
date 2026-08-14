"""Telegram handlers: commands, the approval → caption → preview → post conversation,
and media sent straight to the bot."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import ForceReply, Update
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
    send_preview,
)
from app.models import ItemStatus, split_caption
from app.platforms.base import PostStatus
from app.publish import publish_item
from app.telegram.cards import (
    CALLBACK_SEPARATOR,
    Action,
    close_card,
    format_size,
    results_text,
    retry_keyboard,
)

log = logging.getLogger(__name__)

RUNTIME = "runtime"
TEST_IMAGE = Path("assets/test.png")

TEXT_PROMPT = (
    "✅ Approved — now send the post text.\n\n"
    "First line becomes the Reddit title; everything after it is the caption."
)


def _runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    return context.bot_data[RUNTIME]


def _profile_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Profile | None:
    return next(
        (p for p in _runtime(context).settings.profiles if p.telegram_chat_id == chat_id), None
    )


# -- commands ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt, chat = _runtime(context), update.effective_chat
    profile = _profile_for(context, chat.id)
    if profile is None:
        return
    counts = await db.counts_by_status(rt.conn, profile.name)
    queue = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "empty"
    log.info("chat %s: /start", chat.id)
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"Crosspost Engine is up.\n\n"
            f"profile: {profile.name}\n"
            f"platforms: {', '.join(profile.enabled_platforms) or 'none'}\n"
            f"drive: {'connected' if rt.drive else 'not configured'}\n"
            f"mode: {'DRY RUN — nothing publishes' if rt.settings.dry_run else 'LIVE'}\n"
            f"queue: {queue}\n\n"
            "Send me a photo or video to queue it, or /test for a sample card."
        ),
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue the bundled test image as a real item, exercising the whole path."""
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


# -- ingestion --------------------------------------------------------------------------


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

    item = await enqueue_telegram_media(rt, profile, filename=filename, mime=mime, size_bytes=size)
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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message is the post text for whichever item is awaiting one."""
    rt, chat, message = _runtime(context), update.effective_chat, update.effective_message
    profile = _profile_for(context, chat.id)
    if profile is None:
        return

    item = await db.active_item(rt.conn, profile.name)
    if item is None or item.status is not ItemStatus.AWAITING_TEXT:
        return  # nothing is waiting for text; stay quiet rather than backseat-driving

    title, body = split_caption(message.text or "")
    if not title:
        await message.reply_text("That was empty — send the post text.")
        return

    await db.set_caption(rt.conn, item.id, title, body)
    item.title, item.body = title, body
    log.info("item %d: caption set (%d chars)", item.id, len(message.text or ""))
    await send_preview(rt, profile, item)


# -- buttons ----------------------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tap.

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

    handlers = {
        Action.APPROVE: _approve,
        Action.DECLINE: _decline,
        Action.LATER: _later,
        Action.POST: _post,
        Action.EDIT: _edit,
        Action.CANCEL: _cancel,
        Action.RETRY: _post,
    }
    expected = {
        Action.APPROVE: ItemStatus.PENDING_APPROVAL,
        Action.DECLINE: ItemStatus.PENDING_APPROVAL,
        Action.LATER: ItemStatus.PENDING_APPROVAL,
        Action.POST: ItemStatus.PREVIEWING,
        Action.EDIT: ItemStatus.PREVIEWING,
        Action.CANCEL: ItemStatus.PREVIEWING,
        Action.RETRY: ItemStatus.FAILED,
    }[action]

    if item.status is not expected:
        await query.answer("Already handled.")
        await close_card(query.message, f"⚠️ Ignored — item is {item.status}, not {expected}.")
        return

    log.info("chat %s: %s item %d", chat.id, action.value, item.id)
    await handlers[action](rt, profile, item, query)


async def _approve(rt: Runtime, profile: Profile, item, query) -> None:
    busy = await db.active_item(rt.conn, profile.name)
    if busy is not None:
        # One conversation at a time, otherwise a reply carrying the post text would be
        # ambiguous. Leave this card tappable so it can be approved once the other is done.
        await query.answer(
            f"Finish item {busy.id} first — it is still {busy.status}.", show_alert=True
        )
        return
    await query.answer()
    await db.set_status(rt.conn, item.id, ItemStatus.AWAITING_TEXT)
    await close_card(query.message, "✅ Approved")
    await rt.bot.send_message(
        chat_id=profile.telegram_chat_id,
        text=TEXT_PROMPT,
        reply_markup=ForceReply(input_field_placeholder="Title line, then the caption"),
    )


async def _decline(rt: Runtime, profile: Profile, item, query) -> None:
    await query.answer()
    problem = await archive(rt, item, status=ItemStatus.DECLINED)
    outcome = "❌ Declined\nMoved to rejected/." if problem is None else f"❌ Declined\n⚠️ {problem}"
    await close_card(query.message, outcome)


async def _later(rt: Runtime, profile: Profile, item, query) -> None:
    await query.answer()
    await db.defer(rt.conn, item.id, rt.settings.later_cooldown_minutes)
    await close_card(
        query.message, f"⏭ Skipped\nBack in about {rt.settings.later_cooldown_minutes} min."
    )


async def _edit(rt: Runtime, profile: Profile, item, query) -> None:
    await query.answer()
    await db.set_status(rt.conn, item.id, ItemStatus.AWAITING_TEXT)
    await close_card(query.message, "✏️ Discarded — send the replacement text.")
    await rt.bot.send_message(
        chat_id=profile.telegram_chat_id,
        text="Send the new post text.",
        reply_markup=ForceReply(input_field_placeholder="Title line, then the caption"),
    )


async def _cancel(rt: Runtime, profile: Profile, item, query) -> None:
    await query.answer()
    await db.defer(rt.conn, item.id, rt.settings.later_cooldown_minutes)
    await close_card(query.message, "✖️ Cancelled — the item goes back to the queue.")


async def _post(rt: Runtime, profile: Profile, item, query) -> None:
    """The only path that publishes anything."""
    await query.answer("Posting…")
    await db.set_status(rt.conn, item.id, ItemStatus.POSTING)
    await close_card(query.message, "🚀 Posting…")

    results = await publish_item(rt, profile, item)
    failed = [r for r in results if r.status is PostStatus.FAILED]

    await rt.bot.send_message(
        chat_id=profile.telegram_chat_id,
        text=results_text(results, dry_run=rt.settings.dry_run),
        reply_markup=retry_keyboard(item.id) if failed else None,
        disable_web_page_preview=True,
    )
    settled = await db.get_item(rt.conn, item.id)
    if settled and settled.status is ItemStatus.POSTED and item.drive_file_id:
        await rt.bot.send_message(
            chat_id=profile.telegram_chat_id, text="📁 Drive file moved to posted/."
        )


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
