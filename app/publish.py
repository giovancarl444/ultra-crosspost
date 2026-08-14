"""Fan an approved item out to every enabled platform, concurrently.

Independent of Telegram: it takes an item and returns results. The dedupe rule lives here
— a platform that already succeeded for this item is never asked again, so Retry only
re-attempts what actually failed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app import db
from app.config import Profile
from app.intake import Runtime, archive
from app.models import Item, ItemStatus
from app.platforms.base import MediaRef, PostRequest, PostResult, PostStatus, Publisher
from app.platforms.discord import DiscordPublisher
from app.platforms.reddit import RedditPublisher

log = logging.getLogger(__name__)


def build_publishers(
    rt: Runtime, profile: Profile, *, skip: set[str], client: httpx.AsyncClient
) -> list[Publisher]:
    """One publisher per platform that is enabled, configured, and not already done."""
    publishers: list[Publisher] = []
    if profile.reddit.enabled and "reddit" not in skip:
        missing = [s.env_var for s in profile.reddit.secrets() if not s.is_set]
        if missing:
            log.warning("reddit enabled for %s but unset: %s", profile.name, ", ".join(missing))
        else:
            publishers.append(RedditPublisher(profile.reddit, dry_run=rt.settings.dry_run))

    if profile.discord.enabled and profile.discord.webhook and "discord" not in skip:
        webhook = profile.discord.webhook.resolve_optional()
        if webhook:
            publishers.append(
                DiscordPublisher(webhook, dry_run=rt.settings.dry_run, client=client)
            )
        else:
            log.warning(
                "discord enabled for %s but %s is unset",
                profile.name,
                profile.discord.webhook.env_var,
            )
    return publishers


def build_request(rt: Runtime, item: Item) -> PostRequest:
    path = Path(item.local_path) if item.local_path else None
    return PostRequest(
        item_id=item.id,
        profile=item.profile,
        title=item.title or "",
        body=item.body or "",
        media=MediaRef(
            path=path if path and path.is_file() else None,
            mime=item.mime,
            size_bytes=item.size_bytes,
            hosted_url=item.hosted_url,
        ),
        dry_run=rt.settings.dry_run,
    )


async def publish_item(rt: Runtime, profile: Profile, item: Item) -> list[PostResult]:
    """Publish to everything outstanding, then settle the item's status.

    Returns results for *all* enabled platforms, including ones skipped because they had
    already succeeded, so the report always shows the full picture.
    """
    already = await db.succeeded_platforms(rt.conn, item.id)
    request = build_request(rt, item)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        publishers = build_publishers(rt, profile, skip=already, client=client)
        log.info(
            "publishing item %d to %s%s",
            item.id,
            [p.name for p in publishers] or "nothing",
            f" (skipping already-posted {sorted(already)})" if already else "",
        )
        fresh = await asyncio.gather(
            *(p.publish(request) for p in publishers), return_exceptions=True
        )

    results: list[PostResult] = []
    for publisher, outcome in zip(publishers, fresh, strict=True):
        if isinstance(outcome, BaseException):
            # A publisher is contracted never to raise; treat a breach as a failure
            # rather than letting it take down the other platforms.
            log.exception("publisher %s raised", publisher.name, exc_info=outcome)
            outcome = PostResult.failed(publisher.name, f"{type(outcome).__name__}: {outcome}")
        await db.record_result(
            rt.conn, item.id, outcome.platform, outcome.status, outcome.url, outcome.detail
        )
        results.append(outcome)

    for row in await db.results_for(rt.conn, item.id):
        if row["platform"] in already:
            results.append(
                PostResult(
                    platform=row["platform"],
                    status=PostStatus(row["status"]),
                    url=row["url"],
                    detail="already posted — not sent again",
                )
            )

    results.sort(key=lambda r: r.platform)
    await settle(rt, profile, item, results)
    return results


async def settle(
    rt: Runtime, profile: Profile, item: Item, results: list[PostResult]
) -> str | None:
    """Archive to posted/ once every enabled platform has succeeded; otherwise mark failed
    so the Retry button has something to re-attempt."""
    expected = set(profile.enabled_platforms)
    succeeded = {r.platform for r in results if r.status.is_success}

    if expected and expected <= succeeded:
        return await archive(rt, item, status=ItemStatus.POSTED)
    await db.set_status(rt.conn, item.id, ItemStatus.FAILED)
    return None
