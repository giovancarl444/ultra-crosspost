"""Platform adapters. Adapters are added per phase; the interface is in `base`."""

from app.platforms.base import (
    MediaRef,
    PostRequest,
    PostResult,
    PostStatus,
    Publisher,
)

__all__ = ["MediaRef", "PostRequest", "PostResult", "PostStatus", "Publisher"]
