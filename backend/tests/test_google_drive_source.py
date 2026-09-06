"""Task 7.5 — the Google Drive connector's own transformation logic.

Cannot be tested against a real Drive folder without a real service
account key, which only the account holder can create — see
google_drive.py's own module docstring. What IS tested, thoroughly: the
file-listing pagination, which mime types get extracted and how, which get
correctly skipped, and that one bad file never stops the rest — all
against fixture payloads shaped like Drive API v3's own documented
responses, with only the HTTP layer and the token exchange mocked. The
token exchange itself is Google's own library's responsibility, not
this project's code, and is not what these tests are for.
"""

import httpx
import pytest

from app.services.knowledge_sources.google_drive import fetch_drive_files

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Resp:
    def __init__(self, status_code: int = 200, json_body: dict | None = None, text: str = "", content: bytes = b""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("GET", "https://googleapis.com"),
                response=httpx.Response(self.status_code),
            )


class _FakeDriveClient:
    def __init__(self, file_pages: list[dict], exports: dict, media: dict):
        # file_pages: list of {"files": [...], "nextPageToken": ...} responses,
        # served in order — one per call to the file-list endpoint.
        self._file_pages = list(file_pages)
        self._exports = exports  # file_id -> exported plain text
        self._media = media  # file_id -> (content_bytes, as_text)
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, params: dict | None = None):
        self.requests.append((url, params or {}))
        if url.endswith("/files") and "export" not in url:
            page = self._file_pages.pop(0)
            return _Resp(200, page)
        if url.endswith("/export"):
            file_id = url.split("/files/")[1].split("/export")[0]
            return _Resp(200, text=self._exports.get(file_id, ""))
        if (params or {}).get("alt") == "media":
            file_id = url.split("/files/")[1]
            content, as_text = self._media.get(file_id, (b"", ""))
            return _Resp(200, text=as_text, content=content)
        return _Resp(404)


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    """Bypasses the real Google OAuth token exchange entirely — testing
    that this project's code correctly USES a token, not that Google's own
    library correctly produces one."""
    async def _fake(_service_account_json):
        return "fake-access-token"

    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive._get_access_token", _fake
    )


def _file(file_id: str, name: str, mime_type: str, link: str = "") -> dict:
    return {"id": file_id, "name": name, "mimeType": mime_type,
            "webViewLink": link or f"https://drive.google.com/{file_id}"}


async def test_a_google_doc_is_exported_as_plain_text(monkeypatch):
    client = _FakeDriveClient(
        file_pages=[{"files": [_file("f1", "Notes.gdoc", "application/vnd.google-apps.document")]}],
        exports={"f1": "This is the exported document content."},
        media={},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert len(items) == 1
    assert items[0].external_id == "f1"
    assert items[0].title == "Notes.gdoc"
    assert "exported document content" in items[0].text


async def test_a_plain_text_file_is_downloaded_directly(monkeypatch):
    client = _FakeDriveClient(
        file_pages=[{"files": [_file("f2", "readme.txt", "text/plain")]}],
        exports={},
        media={"f2": (b"Plain text file contents.", "Plain text file contents.")},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items
    assert "Plain text file contents." in items[0].text


async def test_a_pdf_is_extracted_via_the_same_parser_task_2_10_uses(monkeypatch):
    """Reuses rag.parse_pdf rather than a second PDF parser — mocked here
    specifically to isolate THIS module's responsibility (downloading the
    bytes and calling the parser correctly), not PDF parsing itself.

    google_drive.py imports app.services.rag LAZILY, inside the PDF branch
    of _extract_text — and app.services.rag itself imports openai at ITS
    module level, which subclasses httpx.AsyncClient at OPENAI'S import
    time. Found by this exact test: if that first import of app.services.
    rag happens while httpx.AsyncClient is already monkeypatched to a fake
    below, openai tries to subclass a lambda instead of the real class and
    fails with a bizarre metaclass error that has nothing to do with this
    module's own logic. Importing app.services.rag here, BEFORE the
    monkeypatch, makes this test self-sufficient regardless of what other
    test files have or haven't already imported by the time it runs.
    """
    import app.services.rag  # noqa: F401 — see the note above; forces this import first

    client = _FakeDriveClient(
        file_pages=[{"files": [_file("f3", "manual.pdf", "application/pdf")]}],
        exports={},
        media={"f3": (b"%PDF-fake-bytes", "")},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )
    monkeypatch.setattr(
        "app.services.rag.parse_pdf", lambda content: [(1, "Page one text"), (2, "Page two text")]
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert "Page one text" in items[0].text
    assert "Page two text" in items[0].text


async def test_a_subfolder_is_never_recursed_into(monkeypatch):
    """Stated scope limit in the module docstring, pinned by a test: only
    files directly in the configured folder are synced."""
    client = _FakeDriveClient(
        file_pages=[{"files": [
            _file("sub1", "Subfolder", "application/vnd.google-apps.folder"),
            _file("f4", "top-level.txt", "text/plain"),
        ]}],
        exports={},
        media={"f4": (b"Top level content", "Top level content")},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert len(items) == 1
    assert items[0].external_id == "f4"


async def test_an_unsupported_mime_type_is_skipped_not_an_error(monkeypatch):
    client = _FakeDriveClient(
        file_pages=[{"files": [
            _file("img1", "photo.jpg", "image/jpeg"),
            _file("f5", "notes.txt", "text/plain"),
        ]}],
        exports={},
        media={"f5": (b"Real notes", "Real notes")},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert len(items) == 1
    assert items[0].external_id == "f5"


async def test_file_listing_pagination_is_followed_to_the_end(monkeypatch):
    """A folder with more files than one Drive API page — nextPageToken
    must trigger a follow-up request, not silently stop at the first page."""
    client = _FakeDriveClient(
        file_pages=[
            {"files": [_file("f1", "one.txt", "text/plain")], "nextPageToken": "page2"},
            {"files": [_file("f2", "two.txt", "text/plain")]},
        ],
        exports={},
        media={
            "f1": (b"", "First page file"),
            "f2": (b"", "Second page file"),
        },
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert {item.external_id for item in items} == {"f1", "f2"}


async def test_a_file_that_fails_to_download_does_not_stop_the_others(monkeypatch):
    class _PartiallyFailingClient(_FakeDriveClient):
        async def get(self, url, params=None):
            if "/files/broken" in url and params and params.get("alt") == "media":
                return _Resp(403)
            return await super().get(url, params)

    client = _PartiallyFailingClient(
        file_pages=[{"files": [
            _file("broken", "broken.txt", "text/plain"),
            _file("good", "good.txt", "text/plain"),
        ]}],
        exports={},
        media={"good": (b"", "Readable content")},
    )
    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: client
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items

    assert len(items) == 1
    assert items[0].external_id == "good"


async def test_a_file_with_no_extractable_text_is_skipped():
    """An empty text file contributes nothing to a text-based knowledge
    base — must not appear as a zero-content item."""
    client = _FakeDriveClient(
        file_pages=[{"files": [_file("empty", "empty.txt", "text/plain")]}],
        exports={}, media={"empty": (b"", "   ")},
    )
    import app.services.knowledge_sources.google_drive as drive_module
    original = drive_module.httpx.AsyncClient
    drive_module.httpx.AsyncClient = lambda **k: client
    try:
        items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items
    finally:
        drive_module.httpx.AsyncClient = original

    assert items == []


async def test_no_folder_id_configured_returns_nothing():
    items = (await fetch_drive_files("{}", {})).items
    assert items == []


async def test_an_authentication_failure_returns_an_empty_list_not_an_exception(monkeypatch):
    async def _boom(_service_account_json):
        raise ValueError("invalid service account key")

    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive._get_access_token", _boom
    )

    items = (await fetch_drive_files("not valid json", {"folder_id": "folder-1"})).items
    assert items == []


async def test_a_total_api_failure_returns_an_empty_list_not_an_exception(monkeypatch):
    class _ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **k):
            raise httpx.ConnectError("Drive is unreachable")

    monkeypatch.setattr(
        "app.services.knowledge_sources.google_drive.httpx.AsyncClient", lambda **k: _ExplodingClient()
    )

    items = (await fetch_drive_files("{}", {"folder_id": "folder-1"})).items
    assert items == []
