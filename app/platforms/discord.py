"""Discord publishing through an incoming webhook."""

from __future__ import annotations

import json
import logging

import httpx

from app.platforms.base import PostRequest, PostResult, PostStatus

log = logging.getLogger(__name__)

NAME = "discord"

CONTENT_LIMIT = 2000
"""Discord rejects a message body longer than this."""

ATTACHMENT_LIMIT = 10 * 1024 * 1024
"""10 MiB is the default ceiling for a webhook upload. Boosted servers allow more, so an
oversized file is still attempted — this only decides whether to try the link path first."""


class DiscordPublisher:
    name = NAME

    def __init__(self, webhook_url: str, *, dry_run: bool, client: httpx.AsyncClient) -> None:
        self._webhook = webhook_url
        self._dry_run = dry_run
        self._client = client
        self._permalink_base: str | None = None

    async def _resolve_permalink_base(self) -> str | None:
        """Ask the webhook which guild and channel it points at, so results carry a real
        link rather than just an id. Cached; a failure here must not fail the post."""
        if self._permalink_base is None:
            try:
                response = await self._client.get(self._webhook, timeout=15)
                response.raise_for_status()
                hook = response.json()
                self._permalink_base = (
                    f"https://discord.com/channels/{hook['guild_id']}/{hook['channel_id']}"
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not resolve discord webhook metadata: %s", exc)
                self._permalink_base = ""
        return self._permalink_base or None

    async def publish(self, request: PostRequest) -> PostResult:
        content = request.caption
        if len(content) > CONTENT_LIMIT:
            return PostResult.failed(
                NAME, f"caption is {len(content)} characters, over Discord's {CONTENT_LIMIT}"
            )

        media = request.media
        attach = media.path is not None and media.path.is_file()
        oversized = attach and media.size_bytes > ATTACHMENT_LIMIT

        if oversized:
            # Too big for the default webhook ceiling. Post the text with whatever link the
            # item carries; a bare caption with no media is still better than a failure.
            body = content if not media.hosted_url else f"{content}\n{media.hosted_url}".strip()
            detail = (
                f"{media.size_bytes / 1024 / 1024:.1f} MB is over Discord's 10 MiB webhook "
                "limit — posted without the file"
            )
            if media.hosted_url:
                detail = detail.replace("without the file", "as a link")
            return await self._send(body, path=None, detail=detail)

        return await self._send(content, path=media.path if attach else None, mime=media.mime)

    async def _send(
        self, content: str, *, path=None, mime: str = "application/octet-stream", detail=None
    ) -> PostResult:
        status = PostStatus.DEGRADED if detail else PostStatus.OK

        if self._dry_run:
            log.info("[dry run] discord <- %r%s", content[:120], " +file" if path else "")
            return PostResult(
                platform=NAME,
                status=status,
                url="https://discord.com/channels/dry-run/message",
                detail=detail or "dry run — nothing was published",
            )

        try:
            files = None
            handle = None
            if path is not None:
                handle = path.open("rb")
                files = {"files[0]": (path.name, handle, mime)}
            try:
                response = await self._client.post(
                    self._webhook,
                    params={"wait": "true"},
                    data={"payload_json": json.dumps({"content": content})},
                    files=files,
                    timeout=180,
                )
            finally:
                if handle is not None:
                    handle.close()

            if response.status_code == 413:
                return PostResult.failed(NAME, "Discord rejected the file as too large (413)")
            response.raise_for_status()
            message_id = response.json().get("id")
        except httpx.HTTPStatusError as exc:
            return PostResult.failed(
                NAME, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            )
        except Exception as exc:  # noqa: BLE001 — a platform failure is a result, not a crash
            log.exception("discord publish failed")
            return PostResult.failed(NAME, f"{type(exc).__name__}: {exc}")

        base = await self._resolve_permalink_base()
        url = f"{base}/{message_id}" if base and message_id else None
        return PostResult(platform=NAME, status=status, url=url, detail=detail)
