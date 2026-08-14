"""Reddit publishing via asyncpraw.

asyncpraw 8 merged the old `submit_image` / `submit_video` helpers into a single
`Subreddit.submit()`, selected by keyword, and takes media as `PostMedia` objects rather
than paths. It also gained Markdown `selftext` on media posts, which is what makes the
"first line is the title, the rest is the body" convention work here at all.
"""

from __future__ import annotations

import logging
import re

import asyncpraw
from asyncpraw.exceptions import RedditAPIException
from asyncpraw.models import PostMedia

from app.config import RedditConfig
from app.platforms.base import PostRequest, PostResult, PostStatus

log = logging.getLogger(__name__)

NAME = "reddit"

TITLE_LIMIT = 300
"""Reddit rejects a longer title outright."""

URL_PATTERN = re.compile(r"https?://\S+")


def first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    return match.group(0).rstrip(".,);]") if match else None


class RedditPublisher:
    """One instance per post. asyncpraw holds an aiohttp session, so it is closed after use."""

    name = NAME

    def __init__(self, config: RedditConfig, *, dry_run: bool) -> None:
        self._config = config
        self._dry_run = dry_run

    def _user_agent(self, username: str) -> str:
        # Reddit rejects generic user agents; this is their documented shape.
        return f"python:crosspost-engine:0.1.0 (by /u/{username})"

    async def publish(self, request: PostRequest) -> PostResult:
        config = self._config
        title = request.title.strip()
        if not title:
            return PostResult.failed(NAME, "no title — the first line of the caption is the title")
        if len(title) > TITLE_LIMIT:
            return PostResult.failed(
                NAME, f"title is {len(title)} characters, over Reddit's {TITLE_LIMIT}"
            )

        kind, detail = self._choose(request)

        if self._dry_run:
            log.info("[dry run] reddit r/%s <- %r as %s post", config.subreddit, title, kind)
            return PostResult(
                platform=NAME,
                status=PostStatus.DEGRADED if detail else PostStatus.OK,
                url=f"https://reddit.com/r/{config.subreddit}/comments/dry-run/",
                detail=detail or "dry run — nothing was published",
            )

        username = config.username.resolve()
        try:
            reddit = asyncpraw.Reddit(
                client_id=config.client_id.resolve(),
                client_secret=config.client_secret.resolve(),
                username=username,
                password=config.password.resolve(),
                user_agent=self._user_agent(username),
            )
        except Exception as exc:  # noqa: BLE001
            return PostResult.failed(NAME, f"could not build the Reddit client: {exc}")

        try:
            submission = await self._submit(reddit, request, kind)
            if submission is None:
                return PostResult.failed(
                    NAME, "Reddit accepted the upload but returned no submission"
                )
            url = f"https://www.reddit.com{submission.permalink}"
            return PostResult(
                platform=NAME,
                status=PostStatus.DEGRADED if detail else PostStatus.OK,
                url=url,
                detail=detail,
            )
        except RedditAPIException as exc:
            # Subreddit rules land here — missing flair, rate limits, banned domains. The
            # message is the actionable part, so it is passed through verbatim.
            problems = "; ".join(f"{e.error_type}: {e.message}" for e in exc.items) or str(exc)
            log.warning("reddit rejected item %d: %s", request.item_id, problems)
            return PostResult.failed(NAME, problems)
        except Exception as exc:  # noqa: BLE001 — a platform failure is a result, not a crash
            log.exception("reddit publish failed")
            return PostResult.failed(NAME, f"{type(exc).__name__}: {exc}")
        finally:
            await reddit.close()

    def _choose(self, request: PostRequest) -> tuple[str, str | None]:
        """Decide what kind of Reddit post this is, and whether that is a downgrade.

        A URL in the caption wins over the local file: that is how a manually hosted video
        (RedGifs and friends) becomes a properly embedded link post rather than a reupload.
        """
        if first_url(request.body):
            return "link", None
        media = request.media
        if media.path is None:
            if media.hosted_url:
                return "link", "media not available locally — posted the hosted link"
            return "self", "no media — posted as a text post"
        return ("video", None) if media.is_video else ("image", None)

    async def _submit(self, reddit, request: PostRequest, kind: str):
        config = self._config
        subreddit = await reddit.subreddit(config.subreddit)
        common = {
            "selftext": request.body or None,
            "nsfw": config.nsfw,
            "flair_id": config.flair_id,
            "flair_text": config.flair_text,
        }

        if kind == "link":
            url = first_url(request.body) or request.media.hosted_url
            # selftext accompanies a link post as optional body text; strip the bare URL
            # so it does not appear twice.
            body = (request.body or "").replace(url, "").strip() if url else None
            return await subreddit.submit(request.title, url=url, **{**common, "selftext": body or None})

        if kind == "video":
            return await subreddit.submit(
                request.title, video=PostMedia(str(request.media.path)), **common
            )
        if kind == "image":
            return await subreddit.submit(
                request.title, image=PostMedia(str(request.media.path)), **common
            )
        return await subreddit.submit(request.title, **common)
