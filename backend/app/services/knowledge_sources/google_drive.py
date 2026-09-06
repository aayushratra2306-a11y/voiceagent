"""Task 7.5 — pull files out of a Google Drive folder via the Drive API.

Uses a **service account**, not an OAuth consent flow. The manual's own
tools list ("Drive API") does not specify which auth model, and a service
account is the one that fits this project without adding a registered
OAuth app, a hosted redirect URI, or a consent screen this project would
have to operate: a customer creates a service account in their own Google
Cloud project (free), shares the specific Drive folder with that service
account's email address the same way they would share it with a person,
and pastes the service account's JSON key into this bot's knowledge
source config. That key is what `KnowledgeSource.credential_encrypted`
stores — encrypted with the same Fernet scheme as every other credential
in this project (app/core/crypto.py).

Token exchange (`google-auth`) is the one genuinely security-sensitive
piece — signing a JWT assertion with the service account's private key —
and is done with Google's own official library rather than hand-rolled,
deliberately: getting RS256 JWT claims wrong is exactly the kind of subtle,
hard-to-notice bug this project has spent this session finding and fixing
elsewhere, and there is no reason to accept that risk for a well-solved,
already-correct problem. `google-auth`'s own refresh call is synchronous by
design (it is not an asyncio library), so it runs in a thread via
`asyncio.to_thread` — the one blocking call in an otherwise async module.
Every actual Drive API call after that uses httpx, consistent with the
rest of this project.

**Scope, stated rather than silently assumed**: only files directly inside
the configured folder are synced — subfolders are not recursed into. Only
Google Docs (exported as plain text), plain text files, and PDFs (reusing
this project's own `rag.parse_pdf`, the same extraction task 2.10 already
relies on) are extracted; Sheets, Slides, images and anything else are
skipped with a log line rather than guessed at.

**Honest limit**: like notion.py, nothing here can be exercised against a
real Drive folder without a real service account key, which only the
account holder can create. The transformation logic (turning Drive's
documented JSON shapes into FetchedItems) is what's tested
(tests/test_google_drive_source.py), with only the HTTP calls mocked.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from loguru import logger

from app.services.knowledge_sources import FetchedItem, FetchResult

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
REQUEST_TIMEOUT_SECONDS = 20.0
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PLAIN_TEXT_MIME_TYPE = "text/plain"
PDF_MIME_TYPE = "application/pdf"


def _get_access_token_sync(service_account_json: str) -> str:
    """The one blocking call in this module — see the module docstring for
    why it stays synchronous rather than being reimplemented."""
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    info = json.loads(service_account_json)
    credentials = Credentials.from_service_account_info(info, scopes=[DRIVE_READONLY_SCOPE])
    credentials.refresh(Request())
    return credentials.token


async def _get_access_token(service_account_json: str) -> str:
    return await asyncio.to_thread(_get_access_token_sync, service_account_json)


async def _list_files_in_folder(client: httpx.AsyncClient, folder_id: str) -> list[dict]:
    files: list[dict] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        params = {
            "q": query,
            "fields": "nextPageToken, files(id, name, mimeType, webViewLink)",
            "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        response = await client.get(f"{DRIVE_API_BASE}/files", params=params)
        response.raise_for_status()
        body = response.json()
        files.extend(body.get("files", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            return files


async def _extract_text(client: httpx.AsyncClient, file: dict) -> str | None:
    """None means "not a type this can extract text from" — distinct from
    an empty string, which means "extracted successfully, but there was no
    text" (an empty Google Doc, say)."""
    file_id = file["id"]
    mime_type = file.get("mimeType", "")

    if mime_type == FOLDER_MIME_TYPE:
        return None  # subfolders are not recursed into — see the module docstring

    if mime_type == GOOGLE_DOC_MIME_TYPE:
        response = await client.get(
            f"{DRIVE_API_BASE}/files/{file_id}/export", params={"mimeType": PLAIN_TEXT_MIME_TYPE},
        )
        response.raise_for_status()
        return response.text

    if mime_type == PLAIN_TEXT_MIME_TYPE:
        response = await client.get(f"{DRIVE_API_BASE}/files/{file_id}", params={"alt": "media"})
        response.raise_for_status()
        return response.text

    if mime_type == PDF_MIME_TYPE:
        response = await client.get(f"{DRIVE_API_BASE}/files/{file_id}", params={"alt": "media"})
        response.raise_for_status()
        # Reuses task 2.10's own extraction rather than a second PDF parser
        # — one place that knows how to turn PDF bytes into page text.
        from app.services.rag import parse_pdf

        pages = parse_pdf(response.content)
        return "\n".join(text for _page_num, text in pages)

    logger.info(f"[DRIVE] skipping {file.get('name')!r} — unsupported type {mime_type!r}")
    return None


async def fetch_drive_files(service_account_json: str, config: dict) -> FetchResult:
    """`config` carries `folder_id`. New files added to the folder are
    picked up on the next scheduled sync automatically — the actual
    "auto re-syncing" value task 7.5 asks for on the Drive side.

    Never raises: an expired/revoked key, a folder the service account
    lost access to, or a Drive outage should mean this source's sync
    reports an error and every OTHER configured source still runs.

    Every one of those failures comes back as `FetchResult.complete=False`.
    A revoked key returns no files, and so does a customer emptying the
    folder — telling those apart is not optional, because one of them
    means "delete everything you have stored." See FetchResult's own
    comment.
    """
    folder_id = config.get("folder_id")
    if not folder_id:
        logger.warning("[DRIVE] knowledge source has no folder_id configured")
        return FetchResult(items=[], complete=False, error="no folder_id configured")

    try:
        token = await _get_access_token(service_account_json)
    except Exception as e:
        logger.warning(f"[DRIVE] could not authenticate: {type(e).__name__}: {e}")
        return FetchResult(
            items=[], complete=False,
            error=f"could not authenticate with the service account key: {type(e).__name__}",
        )

    headers = {"Authorization": f"Bearer {token}"}
    items: list[FetchedItem] = []
    failures: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
            files = await _list_files_in_folder(client, folder_id)
            for file in files:
                try:
                    text = await _extract_text(client, file)
                except httpx.HTTPStatusError as e:
                    logger.warning(f"[DRIVE] could not read {file.get('name')!r}: {e}")
                    failures.append(f"{file.get('name')!r}: {e.response.status_code}")
                    continue
                if text is None or not text.strip():
                    continue
                items.append(FetchedItem(
                    external_id=file["id"],
                    url=file.get("webViewLink", ""),
                    title=file.get("name", "Untitled"),
                    text=text,
                ))
    except httpx.HTTPStatusError as e:
        logger.warning(f"[DRIVE] sync failed listing folder {folder_id}: {e}")
        return FetchResult(
            items=[], complete=False,
            error=f"could not list folder {folder_id}: {e.response.status_code}",
        )
    except Exception as e:
        logger.warning(f"[DRIVE] unexpected error during sync: {type(e).__name__}: {e}")
        return FetchResult(items=[], complete=False, error=f"{type(e).__name__}: {e}")

    if failures:
        return FetchResult(items=items, complete=False, error="; ".join(failures[:5]))
    return FetchResult(items=items, complete=True)
