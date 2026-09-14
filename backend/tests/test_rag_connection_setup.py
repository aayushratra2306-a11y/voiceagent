"""Live calls 2026-09-14: searches still overran the 3.5s budget after the
rerank fix, and every call sat silent for ~6s before it connected.

Measured inside the production container, not guessed:

1. Setting up the Pinecone clients made blocking network calls ON THE EVENT
   LOOP: `Index(name)` runs a describe-index lookup (0.5s to 17.3s measured)
   and `list_indexes()` another (0.6s to 2.2s). An event-loop heartbeat
   during a cold search showed the loop frozen for 13.45s. Every call runs
   this in its warm-up, and every call log shows a 4.5-5.9s gap between
   "[PIPELINE] Running" and "Connecting to Small WebRTC". Document uploads
   run the same setup in the API process.

2. httpx closes idle connections after 5s (keepalive_expiry default), and
   Pinecone's search hosts are in AWS us-east-1: 261ms to connect plus
   ~520ms of TLS from the Mumbai server. Measured: a query after 3s idle took
   0.30s, after 7s idle 1.05s (a new connection). With the pool kept open,
   queries after 7s, 25s and 55s idle all took 0.27-0.30s, so Pinecone keeps
   idle connections open and only our 5s setting closed them. The warm-up
   finished 6-9s before each question, so it opened connections that were
   already gone when they were needed.

No real Pinecone, OpenAI or Groq request is made in this file.
"""

import asyncio
import time
import types
from pathlib import Path

import httpx
import pytest
from loguru import logger

from app.core import breaker
from app.core.config import settings
from app.services import rag

pytestmark = pytest.mark.asyncio(loop_scope="session")

DENSE_HOST = "https://voiceagent-test.svc.aped-0000.pinecone.io"
SPARSE_HOST = "https://voiceagent-sparse-test.svc.aped-0000.pinecone.io"


def _match(i):
    return types.SimpleNamespace(
        id=f"doc_{i}", score=0.3, metadata={"text": f"[Page 50] chunk {i}", "page": 50.0, "doc_id": "doc"}
    )


class _FakeIndex:
    def query(self, **kwargs):
        return types.SimpleNamespace(matches=[_match(i) for i in range(2)])

    def upsert(self, **kwargs):
        return None


class _FakePinecone:
    """Blocks like the real control-plane lookups do, and counts them."""

    LOOKUP_SECONDS = 0.4
    describes = 0
    lists = 0

    def __init__(self, **kwargs):
        self.inference = types.SimpleNamespace(embed=self._embed)

    def Index(self, name="", host=""):  # noqa: N802 - mirrors the SDK
        if not host:
            type(self).describes += 1
            time.sleep(self.LOOKUP_SECONDS)
        return _FakeIndex()

    def list_indexes(self):
        type(self).lists += 1
        time.sleep(self.LOOKUP_SECONDS)
        return [{"name": settings.pinecone_index_name}, {"name": settings.pinecone_sparse_index_name}]

    @staticmethod
    def _embed(**kwargs):
        return types.SimpleNamespace(
            data=[types.SimpleNamespace(sparse_indices=[1], sparse_values=[1.0]) for _ in kwargs["inputs"]]
        )


@pytest.fixture
def cold_process(monkeypatch, tmp_path: Path):
    """A fresh call process: no Pinecone clients built yet."""
    breaker.use_database(tmp_path / "breakers.db")
    _FakePinecone.describes = 0
    _FakePinecone.lists = 0
    monkeypatch.setattr(rag, "Pinecone", _FakePinecone)
    monkeypatch.setattr(rag, "_pc", None)
    monkeypatch.setattr(rag, "_index", None)
    monkeypatch.setattr(rag, "_sparse_index", None)
    monkeypatch.setattr(settings, "pinecone_index_host", "", raising=False)
    monkeypatch.setattr(settings, "pinecone_sparse_index_host", "", raising=False)

    async def fake_embed(texts):
        return [[0.0] * 3 for _ in texts]

    monkeypatch.setattr(rag, "_embed", fake_embed)


async def _longest_freeze_while(coro) -> float:
    """Runs coro while a 20ms heartbeat measures how long the loop stalls."""
    gaps: list[float] = []
    done = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last - 0.02)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        await coro
    finally:
        done.set()
        await beat
    return max(gaps)


# --- 1. setup must not freeze the call ------------------------------------------

async def test_a_cold_search_does_not_freeze_the_event_loop(cold_process):
    freeze = await _longest_freeze_while(rag.query_context("bot-1", "page 50", rerank=False))

    assert freeze < 0.2, (
        f"the event loop froze for {freeze:.2f}s while Pinecone lookups ran; "
        "nothing else in the call (audio, greeting, the caller's turn) could run"
    )


async def test_a_document_upload_does_not_freeze_the_api_server(cold_process, monkeypatch):
    async def fake_embed_sparse(texts, input_type):
        return [{"indices": [1], "values": [1.0]} for _ in texts]

    monkeypatch.setattr(rag, "_embed_sparse", fake_embed_sparse)
    freeze = await _longest_freeze_while(
        rag.upsert_document("bot-1", "doc", [{"text": "[Page 1] hello world", "page": 1}])
    )

    assert freeze < 0.2, f"an upload froze the API server's event loop for {freeze:.2f}s"


async def test_the_warm_up_and_the_first_question_share_one_setup(cold_process):
    """The warm-up and a quick first question can both arrive before the
    clients exist. Setting up twice would double the lookups."""
    await asyncio.gather(
        rag.query_context("bot-1", "warm up", rerank=False),
        rag.query_context("bot-1", "page 50", rerank=False),
    )

    assert _FakePinecone.describes == 2, f"{_FakePinecone.describes} index lookups, expected 2 (dense + sparse)"
    assert _FakePinecone.lists == 1


async def test_configured_index_hosts_skip_every_lookup(cold_process, monkeypatch):
    monkeypatch.setattr(settings, "pinecone_index_host", DENSE_HOST, raising=False)
    monkeypatch.setattr(settings, "pinecone_sparse_index_host", SPARSE_HOST, raising=False)

    await rag.query_context("bot-1", "page 50", rerank=False)

    assert _FakePinecone.describes == 0 and _FakePinecone.lists == 0, (
        "with the hosts configured, a call should not ask Pinecone where its indexes are"
    )


def test_the_host_settings_exist_and_default_to_looking_up_by_name():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["pinecone_index_host"].default == ""
    assert fields["pinecone_sparse_index_host"].default == ""


# --- 2. connections must survive the gap between questions ----------------------

def _pools(obj, seen=None, depth=0):
    seen = seen if seen is not None else set()
    if id(obj) in seen or depth > 6:
        return []
    seen.add(id(obj))
    if isinstance(obj, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)):
        return [obj._pool]
    found = []
    for value in getattr(obj, "__dict__", {}).values():
        found += _pools(value, seen, depth + 1)
    return found


def test_pinecone_search_connections_stay_open_between_questions(monkeypatch):
    """Uses the REAL pinecone 9.1 client, built by host so no request is made.
    If an SDK upgrade moves the connection pool, this fails rather than the
    fix silently doing nothing."""
    from pinecone import Pinecone

    monkeypatch.setattr(rag, "Pinecone", Pinecone)
    monkeypatch.setattr(rag, "_pc", None)
    monkeypatch.setattr(rag, "_index", None)
    monkeypatch.setattr(rag, "_sparse_index", None)
    monkeypatch.setattr(settings, "pinecone_api_key", "test-key")
    monkeypatch.setattr(settings, "pinecone_index_host", DENSE_HOST, raising=False)
    monkeypatch.setattr(settings, "pinecone_sparse_index_host", SPARSE_HOST, raising=False)

    dense, sparse = rag._get_index(), rag._get_sparse_index()

    for name, client in (("dense index", dense), ("sparse index", sparse), ("sparse embedding", rag._pc.inference)):
        pools = _pools(client)
        assert pools, f"no connection pool found on the {name} client"
        assert all(p._keepalive_expiry == rag.KEEPALIVE_SECONDS for p in pools), (
            f"the {name} connection still closes after {pools[0]._keepalive_expiry}s idle"
        )


def test_openai_and_groq_connections_stay_open_between_questions(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(rag, "_groq", None)

    for name, client in (("OpenAI embeddings", rag._openai), ("Groq rewrite", rag._get_groq())):
        pools = _pools(client._client)
        assert pools, f"no connection pool found on the {name} client"
        assert all(p._keepalive_expiry == rag.KEEPALIVE_SECONDS for p in pools), (
            f"the {name} connection still closes after {pools[0]._keepalive_expiry}s idle"
        )


def test_keep_alive_is_inside_what_pinecone_was_measured_to_allow():
    """Measured 2026-09-14: Pinecone kept an idle connection open for 55s. A
    client that holds on longer than the server risks reusing a dead socket."""
    assert 10 <= rag.KEEPALIVE_SECONDS < 55


# --- 3. the timing line names the slow sub-step --------------------------------

async def test_a_stalled_search_says_which_request_it_was_waiting_on(cold_process, monkeypatch):
    """Live call 2: "embed=- (stopped during embed)" after 3.17s, and nothing
    said whether OpenAI or Pinecone was the one that stalled."""
    monkeypatch.setattr(settings, "pinecone_index_host", DENSE_HOST, raising=False)
    monkeypatch.setattr(settings, "pinecone_sparse_index_host", SPARSE_HOST, raising=False)

    async def stalled_sparse(texts, input_type):
        await asyncio.sleep(5)

    monkeypatch.setattr(rag, "_embed_sparse", stalled_sparse)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(rag.query_context("bot-1", "page 50", rerank=False), timeout=0.3)
    finally:
        logger.remove(sink)

    import re

    timing = [line for line in lines if "search timing" in line][-1]
    assert "stopped during embed" in timing
    assert re.search(r"sparse_embed=\d+\.\d\ds\+unfinished", timing), timing
    assert re.search(r"openai=\d+\.\d\ds(?!\+)", timing), timing
