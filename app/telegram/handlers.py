"""Telegram handlers: the approval keyboard, the taps, and the size-aware send helper.

Everything here is deliberately thin — it turns Telegram events into decisions and back
into messages. The queue and the platform adapters know nothing about this module.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import FileSizeLimit, MessageLimit
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.config import Profile

log = logging.getLogger(__name__)

CALLBACK_SEPARATOR = ":"
PROFILES_BY_CHAT = "profiles_by_chat"
TEST_IMAGE = Path("assets/test.png")


class Action(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    LATER = "later"


ACTION_RESULT = {
    Action.APPROVE: "✅ Approved\nPhase 2 will ask for the post text here.",
    Action.DECLINE: "❌ Declined\nThe Drive file moves to rejected/ from Phase 2.",
    Action.LATER: "⏭ Skipped for now\nIt stays in the queue and comes back around.",
}


def approval_keyboard(item_id: int | str) -> InlineKeyboardMarkup:
    """✅ Approve · ❌ Decline · ⏭ Later. callback_data stays well inside Telegram's 64 bytes."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label, callback_data=f"{action}{CALLBACK_SEPARATOR}{item_id}"
                )
                for action, label in (
                    (Action.APPROVE, "✅ Approve"),
                    (Action.DECLINE, "❌ Decline"),
                    (Action.LATER, "⏭ Later"),
                )
            ]
        ]
    )


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _truncate(caption: str) -> str:
    limit = MessageLimit.CAPTION_LENGTH
    return caption if len(caption) <= limit else caption[: limit - 1] + "…"


async def send_for_approval(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    item_id: int | str,
    path: Path | None,
    filename: str,
    mime: str,
    size_bytes: int,
    link: str | None = None,
) -> Message:
    """Send one item for approval, choosing the delivery that Telegram will actually accept.

    Limits come from PTB's own constants rather than hardcoded numbers: photos cap at 10 MB,
    any other upload at 50 MB. Past that the bot cannot send the file at all, so the item
    goes out as filename plus a link instead — the operator can still judge it.
    """
    header = f"{filename}\n{format_size(size_bytes)} · {mime}"
    keyboard = approval_keyboard(item_id)

    too_big_to_upload = size_bytes > FileSizeLimit.FILESIZE_UPLOAD
    if path is None or not path.is_file() or too_big_to_upload:
        reason = (
            f"⚠️ {format_size(size_bytes)} is over Telegram's "
            f"{format_size(FileSizeLimit.FILESIZE_UPLOAD)} upload limit — preview not attached."
            if too_big_to_upload
            else "⚠️ Media not available locally."
        )
        body = f"{header}\n\n{reason}"
        if link:
            body += f"\n{link}"
        return await context.bot.send_message(
            chat_id=chat_id, text=_truncate(body), reply_markup=keyboard
        )

    with path.open("rb") as handle:
        send_as_photo = mime.startswith("image/") and size_bytes <= FileSizeLimit.PHOTOSIZE_UPLOAD
        if send_as_photo:
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=handle,
                caption=_truncate(header),
                reply_markup=keyboard,
            )
        # Video, or an image too large to send compressed: send it as a document so
        # Telegram neither re-encodes nor rejects it.
        return await context.bot.send_document(
            chat_id=chat_id,
            document=handle,
            filename=filename,
            caption=_truncate(header),
            reply_markup=keyboard,
        )


def _profile_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Profile | None:
    return context.bot_data.get(PROFILES_BY_CHAT, {}).get(chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    profile = _profile_for(context, chat.id)
    if profile is None:  # unreachable via the allow-list filter, but cheap to be sure
        return
    platforms = ", ".join(profile.enabled_platforms) or "none"
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"Crosspost Engine is up.\n\n"
            f"profile: {profile.name}\n"
            f"platforms: {platforms}\n"
            f"chat id: {chat.id}\n\n"
            "Send /test to see an approval card."
        ),
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a local test image with the approval buttons. This is the Phase 1 check."""
    chat = update.effective_chat
    if not TEST_IMAGE.is_file():
        await context.bot.send_message(chat_id=chat.id, text=f"Missing {TEST_IMAGE}.")
        return
    await send_for_approval(
        context,
        chat.id,
        item_id=0,
        path=TEST_IMAGE,
        filename=TEST_IMAGE.name,
        mime="image/png",
        size_bytes=TEST_IMAGE.stat().st_size,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an approval tap.

    Callback queries carry no chat filter of their own, so the allow-list is re-checked
    here rather than relying on the filter that guards the message handlers.
    """
    query = update.callback_query
    chat = update.effective_chat
    if chat is None or _profile_for(context, chat.id) is None:
        log.info("ignoring callback from non-allow-listed chat %s", chat.id if chat else "?")
        await query.answer()
        return

    action_name, _, item_id = (query.data or "").partition(CALLBACK_SEPARATOR)
    try:
        action = Action(action_name)
    except ValueError:
        log.warning("unknown callback action %r", action_name)
        await query.answer("Unknown action.")
        return

    await query.answer()
    log.info("chat %s: %s item %s", chat.id, action.value, item_id or "?")
    await _replace_keyboard(query.message, ACTION_RESULT[action])


async def _replace_keyboard(message: Message | None, outcome: str) -> None:
    """Fold the outcome into the original message and drop the buttons, so the card can
    never be tapped twice."""
    if message is None:
        return
    original = message.caption or message.text or ""
    updated = f"{original}\n\n{outcome}".strip()
    try:
        if message.caption is not None:
            await message.edit_caption(caption=updated, reply_markup=None)
        else:
            await message.edit_text(text=updated, reply_markup=None)
    except BadRequest as exc:
        # "Message is not modified" on a double-tap is expected and harmless.
        if "not modified" not in str(exc).lower():
            raise


async def log_ignored(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every update from outside the allow-list lands here: no reply, but the chat id is
    logged so a new chat can be onboarded without loosening the rule."""
    chat = update.effective_chat
    if chat is not None:
        log.info(
            "ignored update from non-allow-listed chat id=%s type=%s title=%r",
            chat.id,
            chat.type,
            chat.username or chat.title or chat.first_name,
        )
