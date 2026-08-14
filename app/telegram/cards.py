"""Composing the messages the operator sees: the approval card and its buttons.

Presentation only — nothing here touches the database, Drive, or any platform. That keeps
it importable from both the poller and the handlers without a cycle.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import FileSizeLimit, MessageLimit
from telegram.error import BadRequest

CALLBACK_SEPARATOR = ":"


class Action(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    LATER = "later"
    POST = "post"
    EDIT = "edit"
    CANCEL = "cancel"
    RETRY = "retry"


APPROVAL_BUTTONS = (
    (Action.APPROVE, "✅ Approve"),
    (Action.DECLINE, "❌ Decline"),
    (Action.LATER, "⏭ Later"),
)
PREVIEW_BUTTONS = (
    (Action.POST, "🚀 Post"),
    (Action.EDIT, "✏️ Edit text"),
    (Action.CANCEL, "✖️ Cancel"),
)


def _keyboard(item_id: int, buttons) -> InlineKeyboardMarkup:
    """callback_data stays well inside Telegram's 64-byte cap."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"{action}{CALLBACK_SEPARATOR}{item_id}")
                for action, label in buttons
            ]
        ]
    )


def approval_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return _keyboard(item_id, APPROVAL_BUTTONS)


def preview_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return _keyboard(item_id, PREVIEW_BUTTONS)


def retry_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return _keyboard(item_id, ((Action.RETRY, "🔁 Retry failed"),))


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def truncate_caption(caption: str) -> str:
    limit = MessageLimit.CAPTION_LENGTH
    return caption if len(caption) <= limit else caption[: limit - 1] + "…"


def drive_view_link(file_id: str) -> str:
    """Opens for anyone who can already see the folder — i.e. the operator."""
    return f"https://drive.google.com/file/d/{file_id}/view"


async def send_approval_card(
    bot: Bot,
    chat_id: int,
    *,
    item_id: int,
    path: Path | None,
    filename: str,
    mime: str,
    size_bytes: int,
    link: str | None = None,
) -> Message:
    """Send one item for approval, choosing a delivery Telegram will actually accept.

    Ceilings come from PTB's own constants: photos cap at 10 MB, any other upload at
    50 MB. Past that the bot cannot attach the file at all, so the card carries the
    filename and a link instead — enough to judge it.
    """
    header = f"{filename}\n{format_size(size_bytes)} · {mime}"
    keyboard = approval_keyboard(item_id)
    too_big = size_bytes > FileSizeLimit.FILESIZE_UPLOAD

    if path is None or not path.is_file() or too_big:
        reason = (
            f"⚠️ {format_size(size_bytes)} exceeds Telegram's "
            f"{format_size(FileSizeLimit.FILESIZE_UPLOAD)} upload limit — preview not attached."
            if too_big
            else "⚠️ Media not available locally."
        )
        body = f"{header}\n\n{reason}" + (f"\n{link}" if link else "")
        return await bot.send_message(chat_id=chat_id, text=truncate_caption(body), reply_markup=keyboard)

    with path.open("rb") as handle:
        if mime.startswith("image/") and size_bytes <= FileSizeLimit.PHOTOSIZE_UPLOAD:
            return await bot.send_photo(
                chat_id=chat_id, photo=handle, caption=truncate_caption(header), reply_markup=keyboard
            )
        # Video, or an image too large to send compressed: as a document, so Telegram
        # neither re-encodes nor rejects it.
        return await bot.send_document(
            chat_id=chat_id,
            document=handle,
            filename=filename,
            caption=truncate_caption(header),
            reply_markup=keyboard,
        )


def preview_text(
    *, filename: str, title: str, body: str, platforms: tuple[str, ...], warnings: list[str]
) -> str:
    """What goes out, shown before anything leaves the machine."""
    lines = [
        "📋 Preview",
        f"file: {filename}",
        f"to: {', '.join(platforms) or 'nothing enabled'}",
        "",
        f"title (Reddit): {title}",
        "",
        f"caption: {body}" if body else "caption: (title is reused as the caption)",
    ]
    if warnings:
        lines += ["", *(f"⚠️ {w}" for w in warnings)]
    lines += ["", "Nothing is published until you tap 🚀 Post."]
    return "\n".join(lines)


SYMBOL = {"ok": "✅", "degraded": "⚠️", "failed": "❌", "skipped": "⏭"}


def results_text(results, *, dry_run: bool) -> str:
    lines = ["🚀 Dry run — nothing was published" if dry_run else "🚀 Posted"]
    for result in results:
        line = f"{SYMBOL.get(result.status, '•')} {result.platform}"
        if result.url:
            line += f" — {result.url}"
        if result.detail:
            line += f"\n    {result.detail}"
        lines.append(line)
    return "\n".join(lines)


async def close_card(message: Message | None, outcome: str) -> None:
    """Fold the outcome into the card and drop the buttons, so it cannot be tapped twice."""
    if message is None:
        return
    original = message.caption or message.text or ""
    updated = f"{original}\n\n{outcome}".strip()
    try:
        if message.caption is not None:
            await message.edit_caption(caption=truncate_caption(updated), reply_markup=None)
        else:
            await message.edit_text(text=truncate_caption(updated), reply_markup=None)
    except BadRequest as exc:
        # "Message is not modified" on a double tap is expected and harmless.
        if "not modified" not in str(exc).lower():
            raise
