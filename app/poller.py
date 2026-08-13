"""The Drive poll loop: notice new files, queue them, offer them for approval.

Runs as a sibling task to the Telegram updater under the same event loop. A failure in one
profile is logged and skipped — it never stops the loop or affects the other profiles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app import db
from app.config import Profile
from app.intake import Runtime, enqueue_drive_file, fetch_media, offer

log = logging.getLogger(__name__)


async def discover(rt: Runtime, profile: Profile) -> int:
    """List the inbox and queue anything not seen before. Returns how many were new."""
    if rt.drive is None:
        return 0
    files = await rt.drive.list_media(profile.drive.inbox_folder_id)
    known = await db.known_drive_file_ids(rt.conn, profile.name)
    new = 0
    for file in files:
        if file.id in known:
            continue
        if await enqueue_drive_file(rt, profile, file):
            new += 1
    if new:
        log.info("profile %s: %d new file(s) in inbox", profile.name, new)
    return new


async def offer_due(rt: Runtime, profile: Profile) -> int:
    """Offer queued items that are due, up to the per-cycle cap.

    The cap exists because Telegram rate-limits bulk sends to one chat; dropping 40 files
    into the inbox at once should trickle out over cycles rather than get throttled.
    """
    due = await db.claim_offerable(rt.conn, profile.name)
    offered = 0
    for item in due[: rt.settings.max_offers_per_cycle]:
        try:
            await fetch_media(rt, item)
            await offer(rt, profile, item)
            offered += 1
        except Exception:  # noqa: BLE001 — one bad item must not stall the queue
            log.exception("could not offer item %d; leaving it queued", item.id)
    if len(due) > offered:
        log.info(
            "profile %s: offered %d, %d still waiting", profile.name, offered, len(due) - offered
        )
    return offered


async def poll_once(rt: Runtime) -> None:
    for profile in rt.settings.profiles:
        try:
            await discover(rt, profile)
            await offer_due(rt, profile)
        except Exception:  # noqa: BLE001 — keep polling the other profiles
            log.exception("poll failed for profile %s", profile.name)


async def poll_forever(rt: Runtime, stop: asyncio.Event) -> None:
    interval = rt.settings.poll_interval_seconds
    log.info("drive poller started (every %ds)", interval)
    while not stop.is_set():
        await poll_once(rt)
        # Sleep, but wake immediately on shutdown rather than finishing the interval.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    log.info("drive poller stopped")
