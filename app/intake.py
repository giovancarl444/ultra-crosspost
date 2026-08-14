"""Getting media into the queue and out of it again.

Both sources — a file appearing in a Drive inbox, and media sent straight to the bot —
land here and become the same kind of item. The only difference is that a Telegram-sourced
item has no Drive file, so it has no archive move at the end.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from telegram import Bot

from app import db
from app.config import Profile, Settings
from app.drive import DriveClient, DriveFile
from app.models import Item, ItemSource, ItemStatus
from app.platforms.discord import ATTACHMENT_LIMIT as DISCORD_ATTACHMENT_LIMIT
from app.platforms.discord import CONTENT_LIMIT as DISCORD_CONTENT_LIMIT
from app.telegram.cards import (
    drive_view_link,
    format_size,
    preview_keyboard,
    preview_text,
    send_approval_card,
)

log = logging.getLogger(__name__)

UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Runtime:
    """Everything the running service shares. Handed to handlers via `bot_data`."""

    settings: Settings
    conn: aiosqlite.Connection
    bot: Bot
    drive: DriveClient | None
    """None when no service-account file is configured — the bot still runs, and media
    sent directly to it still queues. Only Drive polling is unavailable."""

    def profile(self, name: str) -> Profile | None:
        return next((p for p in self.settings.profiles if p.name == name), None)


def local_path_for(settings: Settings, item: Item) -> Path:
    safe = UNSAFE_FILENAME.sub("_", item.filename).strip("_") or "media"
    return settings.media_dir / item.profile / f"{item.id}-{safe}"


async def enqueue_drive_file(rt: Runtime, profile: Profile, file: DriveFile) -> Item | None:
    """Record a newly seen Drive file. Returns None if it is already in the queue."""
    item = await db.add_item(
        rt.conn,
        profile=profile.name,
        source=ItemSource.DRIVE,
        drive_file_id=file.id,
        filename=file.name,
        mime=file.mime,
        size_bytes=file.size_bytes,
    )
    if item is not None:
        log.info("queued %s (%s) from drive as item %d", file.name, file.mime, item.id)
    return item


async def enqueue_telegram_media(
    rt: Runtime, profile: Profile, *, filename: str, mime: str, size_bytes: int
) -> Item | None:
    item = await db.add_item(
        rt.conn,
        profile=profile.name,
        source=ItemSource.TELEGRAM,
        filename=filename,
        mime=mime,
        size_bytes=size_bytes,
    )
    if item is not None:
        log.info("queued %s (%s) from telegram as item %d", filename, mime, item.id)
    return item


async def fetch_media(rt: Runtime, item: Item) -> Path | None:
    """Ensure the Drive item's bytes are on local disk, and remember where."""
    if item.local_path and Path(item.local_path).is_file():
        return Path(item.local_path)
    if item.source is not ItemSource.DRIVE or rt.drive is None or not item.drive_file_id:
        return None

    destination = local_path_for(rt.settings, item)
    log.info("downloading item %d (%s) to %s", item.id, item.filename, destination)
    await rt.drive.download(item.drive_file_id, destination)
    await db.set_local_path(rt.conn, item.id, destination)
    item.local_path = str(destination)
    return destination


async def offer(rt: Runtime, profile: Profile, item: Item) -> None:
    """Send the approval card and mark the item as awaiting a decision."""
    path = Path(item.local_path) if item.local_path else None
    message = await send_approval_card(
        rt.bot,
        profile.telegram_chat_id,
        item_id=item.id,
        path=path,
        filename=item.filename,
        mime=item.mime,
        size_bytes=item.size_bytes,
        link=drive_view_link(item.drive_file_id) if item.drive_file_id else None,
    )
    await db.set_offered(rt.conn, item.id, message.message_id)
    log.info("offered item %d to chat %s", item.id, profile.telegram_chat_id)


def preview_warnings(profile: Profile, item: Item) -> list[str]:
    """Everything worth knowing *before* tapping Post rather than after."""
    problems = []
    caption = item.body or item.title or ""
    if profile.discord.enabled and len(caption) > DISCORD_CONTENT_LIMIT:
        problems.append(
            f"caption is {len(caption)} characters — Discord rejects anything over "
            f"{DISCORD_CONTENT_LIMIT}, so it would fail"
        )
    if profile.discord.enabled and item.size_bytes > DISCORD_ATTACHMENT_LIMIT:
        problems.append(
            f"{format_size(item.size_bytes)} is over Discord's 10 MiB webhook limit — it "
            "will post the text without the file"
        )
    if not profile.enabled_platforms:
        problems.append("no platforms are enabled, so Post will not publish anywhere")
    return problems


async def send_preview(rt: Runtime, profile: Profile, item: Item) -> None:
    """Show exactly what will go where, with Post / Edit / Cancel."""
    await rt.bot.send_message(
        chat_id=profile.telegram_chat_id,
        text=preview_text(
            filename=item.filename,
            title=item.title or "",
            body=item.body or "",
            platforms=profile.enabled_platforms,
            warnings=preview_warnings(profile, item),
        ),
        reply_markup=preview_keyboard(item.id),
    )


async def archive(rt: Runtime, item: Item, *, status: ItemStatus) -> str | None:
    """Move the Drive file to posted/ or rejected/ and drop the local copy.

    Returns a human-readable problem if the move failed, otherwise None. Nothing is ever
    hard-deleted on Drive — archiving is a re-parent.
    """
    profile = rt.profile(item.profile)
    problem = None

    if item.drive_file_id and rt.drive and profile:
        target = (
            profile.drive.posted_folder_id
            if status is ItemStatus.POSTED
            else profile.drive.rejected_folder_id
        )
        try:
            await rt.drive.move(
                item.drive_file_id,
                from_folder=profile.drive.inbox_folder_id,
                to_folder=target,
            )
            log.info("moved item %d on drive to %s", item.id, target)
        except Exception as exc:  # noqa: BLE001 — reported to the operator, never fatal
            problem = f"Drive move failed: {exc}"
            log.exception("drive move failed for item %d", item.id)

    await db.set_status(rt.conn, item.id, status)
    if status.is_terminal:
        cleanup_media(item)
    return problem


def cleanup_media(item: Item) -> None:
    """Remove the local copy once the item can no longer need it."""
    if not item.local_path:
        return
    path = Path(item.local_path)
    try:
        path.unlink(missing_ok=True)
        log.debug("removed local media for item %d", item.id)
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)
