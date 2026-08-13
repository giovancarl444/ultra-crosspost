"""Google Drive v3 access with a service account.

The app only ever lists, downloads and moves. It never creates or deletes folders, and
never deletes a file — archiving is a re-parent, so nothing is ever lost.

google-api-python-client is synchronous, so every call is pushed through
`asyncio.to_thread` to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
"""Full Drive scope is required to re-parent a file. The real boundary is sharing: a
service account can only see folders that have been explicitly shared with it."""

MEDIA_QUERY = (
    "'{folder_id}' in parents and trashed = false "
    "and (mimeType contains 'image/' or mimeType contains 'video/')"
)
LIST_FIELDS = "nextPageToken, files(id, name, mimeType, size)"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime: str
    size_bytes: int


class DriveError(RuntimeError):
    pass


class DriveClient:
    def __init__(self, service_account_file: Path) -> None:
        self._path = service_account_file
        self._service = None

    @property
    def account_email(self) -> str:
        """The address the operator must share their folders with."""
        try:
            return json.loads(self._path.read_text()).get("client_email", "<unknown>")
        except (OSError, json.JSONDecodeError) as exc:
            raise DriveError(f"cannot read {self._path}: {exc}") from exc

    def _client(self):
        if self._service is None:
            credentials = service_account.Credentials.from_service_account_file(
                str(self._path), scopes=SCOPES
            )
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    # -- sync bodies, each run in a worker thread ---------------------------------------

    def _list_media(self, folder_id: str) -> list[DriveFile]:
        files, page_token = [], None
        while True:
            response = (
                self._client()
                .files()
                .list(
                    q=MEDIA_QUERY.format(folder_id=folder_id),
                    fields=LIST_FIELDS,
                    pageToken=page_token,
                    pageSize=100,
                    orderBy="createdTime",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(
                DriveFile(
                    id=entry["id"],
                    name=entry.get("name", entry["id"]),
                    mime=entry.get("mimeType", "application/octet-stream"),
                    size_bytes=int(entry.get("size") or 0),
                )
                for entry in response.get("files", [])
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    def _download(self, file_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = self._client().files().get_media(fileId=file_id, supportsAllDrives=True)
        # Download to a temp name first so a crash mid-transfer cannot leave a truncated
        # file that later looks complete.
        partial = destination.with_suffix(destination.suffix + ".part")
        with partial.open("wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        partial.replace(destination)

    def _move(self, file_id: str, *, from_folder: str, to_folder: str) -> None:
        self._client().files().update(
            fileId=file_id,
            addParents=to_folder,
            removeParents=from_folder,
            fields="id, parents",
            supportsAllDrives=True,
        ).execute()

    def _folder_name(self, folder_id: str) -> str:
        entry = (
            self._client()
            .files()
            .get(fileId=folder_id, fields="id, name, mimeType", supportsAllDrives=True)
            .execute()
        )
        if entry.get("mimeType") != "application/vnd.google-apps.folder":
            raise DriveError(f"{folder_id} is not a folder ({entry.get('mimeType')})")
        return entry.get("name", folder_id)

    # -- async surface -------------------------------------------------------------------

    async def list_media(self, folder_id: str) -> list[DriveFile]:
        return await asyncio.to_thread(self._list_media, folder_id)

    async def download(self, file_id: str, destination: Path) -> None:
        await asyncio.to_thread(self._download, file_id, destination)

    async def move(self, file_id: str, *, from_folder: str, to_folder: str) -> None:
        await asyncio.to_thread(self._move, file_id, from_folder=from_folder, to_folder=to_folder)

    async def folder_name(self, folder_id: str) -> str:
        return await asyncio.to_thread(self._folder_name, folder_id)

    async def check_access(self, folder_ids: dict[str, str]) -> dict[str, str]:
        """Resolve each configured folder id to its name, so a misconfigured or unshared
        folder is caught at startup rather than on the first poll."""
        resolved = {}
        for label, folder_id in folder_ids.items():
            try:
                resolved[label] = await self.folder_name(folder_id)
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator as text
                resolved[label] = f"UNREACHABLE ({type(exc).__name__})"
        return resolved
