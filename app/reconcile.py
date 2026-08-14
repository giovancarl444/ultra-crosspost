"""Bring the queue back to a coherent state after a restart.

The service is a daemon on a machine that sleeps, reboots and loses its network. State
lives in SQLite rather than memory, so nothing is lost — but an item can be left mid
conversation, and the operator should not have to remember where they were.

The one case that needs care is an item that was `posting` when the process died: whether
it reached each platform is genuinely unknown. That is reported, never silently retried.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import db
from app.config import Profile
from app.intake import Runtime, fetch_media, offer, send_preview
from app.models import Item, ItemSource, ItemStatus

log = logging.getLogger(__name__)

RESUMABLE = (
    ItemStatus.PENDING_APPROVAL,
    ItemStatus.AWAITING_TEXT,
    ItemStatus.PREVIEWING,
    ItemStatus.POSTING,
)

TEXT_PROMPT_AGAIN = (
    "↩️ Picking up where we left off — send the post text for this item.\n\n"
    "First line becomes the Reddit title; everything after it is the caption."
)


async def reconcile(rt: Runtime) -> None:
    """Called once on boot, before polling starts."""
    items = await db.items_with_status(rt.conn, RESUMABLE)
    if not items:
        log.info("reconcile: nothing in flight")
        return

    log.info("reconcile: %d item(s) were in flight", len(items))
    for item in items:
        profile = rt.profile(item.profile)
        if profile is None:
            log.warning(
                "reconcile: item %d belongs to profile %r which is no longer configured; "
                "leaving it alone",
                item.id,
                item.profile,
            )
            continue
        try:
            await _resume(rt, profile, item)
        except Exception:  # noqa: BLE001 — one bad item must not stop the service booting
            log.exception("reconcile: could not resume item %d", item.id)


async def _resume(rt: Runtime, profile: Profile, item: Item) -> None:
    if item.status is ItemStatus.POSTING:
        await _resume_interrupted_post(rt, profile, item)
        return

    if not await _ensure_media(rt, profile, item):
        return

    if item.status is ItemStatus.PENDING_APPROVAL:
        # Re-offer rather than trust the old card: its buttons still work, but after an
        # outage it may be far up the operator's history.
        await offer(rt, profile, item)
    elif item.status is ItemStatus.AWAITING_TEXT:
        await rt.bot.send_message(chat_id=profile.telegram_chat_id, text=TEXT_PROMPT_AGAIN)
        log.info("reconcile: re-asked for text on item %d", item.id)
    elif item.status is ItemStatus.PREVIEWING:
        await send_preview(rt, profile, item)
        log.info("reconcile: re-sent preview for item %d", item.id)


async def _ensure_media(rt: Runtime, profile: Profile, item: Item) -> bool:
    """Make sure the bytes are back on disk. Returns False if they are gone for good."""
    if item.local_path and Path(item.local_path).is_file():
        return True

    if item.source is ItemSource.DRIVE and rt.drive and item.drive_file_id:
        log.info("reconcile: re-downloading item %d", item.id)
        await fetch_media(rt, item)
        return True

    # Telegram-sourced media has no durable source to re-fetch from: Telegram's file
    # references expire, and there is no Drive copy. Say so rather than failing later.
    await db.set_status(rt.conn, item.id, ItemStatus.FAILED)
    await rt.bot.send_message(
        chat_id=profile.telegram_chat_id,
        text=(
            f"⚠️ Item {item.id} ({item.filename}) lost its local media during a restart and "
            "came from Telegram, so there is nothing to re-download. Send it again."
        ),
    )
    log.warning("reconcile: item %d has no recoverable media", item.id)
    return False


async def _resume_interrupted_post(rt: Runtime, profile: Profile, item: Item) -> None:
    """The process died mid-publish. Report what is known and let the operator decide.

    Auto-retrying an unknown outcome is how something gets posted twice, so this only ever
    reports.
    """
    recorded = {row["platform"]: row for row in await db.results_for(rt.conn, item.id)}
    succeeded = await db.succeeded_platforms(rt.conn, item.id)
    unknown = [p for p in profile.enabled_platforms if p not in recorded]

    await db.set_status(rt.conn, item.id, ItemStatus.FAILED)

    lines = [f"⚠️ Item {item.id} ({item.filename}) was mid-post when the service stopped."]
    if succeeded:
        lines.append(f"Confirmed posted: {', '.join(sorted(succeeded))} — will not be resent.")
    if unknown:
        lines.append(
            f"Unknown: {', '.join(unknown)}. Check the channel before retrying — I cannot "
            "tell whether these went out."
        )
    lines.append("Use 🔁 Retry on the result message once you have checked.")

    await rt.bot.send_message(chat_id=profile.telegram_chat_id, text="\n".join(lines))
    log.warning(
        "reconcile: item %d interrupted mid-post; confirmed=%s unknown=%s",
        item.id,
        sorted(succeeded),
        unknown,
    )
