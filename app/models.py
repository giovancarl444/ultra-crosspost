"""Domain types shared by the queue, the Telegram handlers and the platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ItemSource(StrEnum):
    DRIVE = "drive"
    TELEGRAM = "telegram"
    """Media sent straight to the bot. Has no Drive file, so it is never archive-moved."""


class ItemStatus(StrEnum):
    QUEUED = "queued"
    PENDING_APPROVAL = "pending_approval"
    AWAITING_TEXT = "awaiting_text"
    PREVIEWING = "previewing"
    POSTING = "posting"
    POSTED = "posted"
    DECLINED = "declined"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Terminal means the local media file can be cleaned up.

        FAILED is deliberately excluded: the Retry button re-attempts only the platforms
        that did not already succeed, so a failed item keeps its media until it posts.
        """
        return self in {ItemStatus.POSTED, ItemStatus.DECLINED}

    @property
    def needs_prompt_reissue(self) -> bool:
        """States where a restart leaves the operator mid-conversation with the bot and
        the prompt has to be sent again."""
        return self in {
            ItemStatus.PENDING_APPROVAL,
            ItemStatus.AWAITING_TEXT,
            ItemStatus.PREVIEWING,
        }


@dataclass
class Item:
    id: int
    profile: str
    source: ItemSource
    drive_file_id: str | None
    filename: str
    mime: str
    size_bytes: int
    status: ItemStatus
    title: str | None = None
    body: str | None = None
    local_path: str | None = None
    hosted_url: str | None = None
    telegram_message_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def split_caption(text: str) -> tuple[str, str]:
    """Apply the caption convention: first line is the Reddit title, the rest is the body.

    The body is used verbatim as the Discord message and the Reddit post body.
    """
    title, _, body = text.strip().partition("\n")
    return title.strip(), body.strip()
