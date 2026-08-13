"""The publisher interface: item + caption in, result out.

Deliberately free of any Telegram or Drive concept so the same adapters can be driven by
a different front end later. Adapters live in sibling modules and are added per phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class PostStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    """Published, but not with the media attached natively — e.g. the video exceeded the
    platform's upload ceiling and went out as a hosted link instead. Reported distinctly
    from OK so the result message can say so, and so Retry can offer to re-attempt it."""
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not attempted: the platform is disabled for this profile, or this (item, platform)
    pair already succeeded and must never be posted twice."""

    @property
    def is_success(self) -> bool:
        return self in {PostStatus.OK, PostStatus.DEGRADED}


@dataclass(frozen=True)
class MediaRef:
    """The media for one item, as the platform layer sees it."""

    path: Path | None
    mime: str
    size_bytes: int
    hosted_url: str | None = None
    """Set once the media has been uploaded to a host (RedGifs). Adapters that cannot
    take the file natively post this link instead and return DEGRADED."""

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/")

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass(frozen=True)
class PostRequest:
    """One approved item, ready to publish.

    `title` and `body` come from the caption convention: the first line is the title, and
    everything after it is the body used for the Discord message and the Reddit post body.
    """

    item_id: int
    profile: str
    title: str
    body: str
    media: MediaRef
    dry_run: bool = True

    @property
    def full_text(self) -> str:
        """Title and body as a single block, for platforms that have no separate title."""
        return f"{self.title}\n\n{self.body}".strip() if self.body else self.title


@dataclass(frozen=True)
class PostResult:
    platform: str
    status: PostStatus
    url: str | None = None
    detail: str | None = None
    """Human-readable context: the error for FAILED, or what was traded away for DEGRADED
    (e.g. "video over the 10 MiB webhook limit — posted the RedGifs link instead")."""

    @classmethod
    def ok(cls, platform: str, url: str) -> PostResult:
        return cls(platform=platform, status=PostStatus.OK, url=url)

    @classmethod
    def degraded(cls, platform: str, url: str, detail: str) -> PostResult:
        return cls(platform=platform, status=PostStatus.DEGRADED, url=url, detail=detail)

    @classmethod
    def failed(cls, platform: str, detail: str) -> PostResult:
        return cls(platform=platform, status=PostStatus.FAILED, detail=detail)

    @classmethod
    def skipped(cls, platform: str, detail: str) -> PostResult:
        return cls(platform=platform, status=PostStatus.SKIPPED, detail=detail)


@runtime_checkable
class Publisher(Protocol):
    """What every platform adapter implements. One method, no shared state."""

    name: str

    async def publish(self, request: PostRequest) -> PostResult:
        """Publish the request. Must never raise — failures come back as PostResult.failed
        so that one platform going down cannot take the others with it."""
        ...
