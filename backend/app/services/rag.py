import asyncio
import io
import re
import time

import pypdf
from loguru import logger
from openai import AsyncOpenAI
from pinecone import Pinecone, RetryConfig

from app.core import breaker
from app.core.config import settings

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_pc: Pinecone | None = None
_index = None
_rerank_pc: Pinecone | None = None


def _get_rerank_client() -> Pinecone:
    """A Pinecone client used only for rerank, with the SDK's retries off.

    Found 2026-09-13: pinecone 9.1 retries a 429 three times by default,
    sleeping between attempts inside the worker thread, where a budget
    cannot cancel it. A refused MONTHLY allowance is still refused a second
    later, so those retries only spent the caller's 3.5s. Rerank already
    falls back to raw similarity on any failure, so a retry buys nothing
    here. Every other Pinecone call keeps the SDK defaults on _pc.

    The request timeout is short for a related reason (independent review,
    same day): a rerank the search stops waiting for still runs in its
    thread, and with the SDK's 30s default it could hold one of the few
    default thread-pool threads that embed, the index queries and breaker
    reads also queue for. See RERANK_REQUEST_TIMEOUT_SECONDS.
    """
    global _rerank_pc
    if _rerank_pc is None:
        _rerank_pc = Pinecone(
            api_key=settings.pinecone_api_key,
            retry_config=RetryConfig(max_retries=0),
            timeout=RERANK_REQUEST_TIMEOUT_SECONDS,
        )
    return _rerank_pc

_groq: AsyncOpenAI | None = None


def _get_groq() -> AsyncOpenAI | None:
    """Lazily built — returns None when no Groq key is configured, so
    rewrite_query() can fall back to the raw query untouched rather than
    crash. Groq's API is OpenAI-compatible, so the same client class works
    against its base URL."""
    global _groq
    if not settings.groq_api_key:
        return None
    if _groq is None:
        _groq = AsyncOpenAI(api_key=settings.groq_api_key, base_url="https://api.groq.com/openai/v1")
    return _groq


def _get_index():
    global _pc, _index
    if _index is None:
        _pc = Pinecone(api_key=settings.pinecone_api_key)
        _index = _pc.Index(settings.pinecone_index_name)
    return _index


_sparse_index = None
SPARSE_EMBED_MODEL = "pinecone-sparse-english-v0"


def _get_sparse_index():
    """Task 1.8 — keyword-matching side of hybrid search.

    Pinecone serverless indexes are single-type (confirmed 2026-08-31 via
    describe_index(): the existing index is vector_type='dense', and
    Pinecone doesn't let a serverless index mix dense + sparse vectors), so
    this is a genuinely separate index, created lazily on first use with
    metric='dotproduct' (required for sparse) rather than the dense index's
    'cosine'. Same ids and namespace convention as the dense index, so
    results from both sides can be correlated/deduped by id.
    """
    global _sparse_index
    if _sparse_index is None:
        _get_index()  # ensures _pc is initialized
        existing = {idx["name"] for idx in _pc.list_indexes()}
        if settings.pinecone_sparse_index_name not in existing:
            logger.info(f"[RAG] Creating sparse index '{settings.pinecone_sparse_index_name}' (first run)")
            _pc.create_index(
                name=settings.pinecone_sparse_index_name,
                vector_type="sparse",
                metric="dotproduct",
                spec={"serverless": {"cloud": "aws", "region": "us-east-1"}},
            )
        _sparse_index = _pc.Index(settings.pinecone_sparse_index_name)
    return _sparse_index


# ── PDF parsing ────────────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes) -> list[tuple[int, str]]:
    """Returns list of (page_number, text) tuples, 1-indexed."""
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(pages: list[tuple[int, str]], chunk_size: int = 400, overlap: int = 80) -> list[dict]:
    """Returns list of {text, page} dicts. Text is prefixed with [Page N] so embeddings capture page context."""
    chunks = []
    for page_num, text in pages:
        words = text.split()
        start = 0
        while start < len(words):
            chunk = " ".join(words[start : start + chunk_size])
            if len(chunk.strip()) > 50:
                chunks.append({
                    "text": f"[Page {page_num}] {chunk}",
                    "page": page_num,
                })
            start += chunk_size - overlap
    return chunks


# ── Embeddings ─────────────────────────────────────────────────────────────────

async def _embed(texts: list[str]) -> list[list[float]]:
    response = await _openai.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    return [item.embedding for item in response.data]


_SPARSE_EMBED_BATCH = 96  # Pinecone's own hard limit for this model — hit
# live 2026-08-31 backfilling a real 183-chunk document ("Input length '183'
# exceeded inputs limit of 96"). Batching here protects every caller,
# including future large document uploads, not just the one-off backfill.


async def _embed_sparse(texts: list[str], input_type: str) -> list[dict]:
    """input_type is 'passage' for documents being stored, 'query' for a
    search query — Pinecone's sparse model weights terms differently for
    each (confirmed via its own docs; verified live 2026-08-31 that both
    values are accepted). Returns Pinecone's native sparse-vector shape
    ({"indices": [...], "values": [...]}) ready to pass straight into an
    upsert or query call."""
    _get_index()  # ensures _pc is initialized (return value unused here)
    loop = asyncio.get_event_loop()
    out: list[dict] = []
    for i in range(0, len(texts), _SPARSE_EMBED_BATCH):
        batch = texts[i : i + _SPARSE_EMBED_BATCH]
        result = await loop.run_in_executor(
            None,
            lambda b=batch: _pc.inference.embed(
                model=SPARSE_EMBED_MODEL,
                inputs=b,
                parameters={"input_type": input_type, "truncate": "END"},
            ),
        )
        out.extend({"indices": e.sparse_indices, "values": e.sparse_values} for e in result.data)
    return out


# ── Pinecone upsert ────────────────────────────────────────────────────────────

async def upsert_document(bot_id: str, doc_id: str, chunks: list[dict]) -> int:
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]
    embeddings, sparse_embeddings = await asyncio.gather(
        _embed(texts),
        _embed_sparse(texts, input_type="passage"),
    )

    vectors = [
        {
            "id": f"{doc_id}_{i}",
            "values": emb,
            "metadata": {"text": chunk["text"], "doc_id": doc_id, "page": chunk["page"]},
        }
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings, strict=True))
    ]
    # Same ids/metadata as the dense side (Task 1.8) — lets query_context
    # correlate/dedupe hits from both indexes and lets delete_document_vectors
    # remove both sides with the one id list it already builds.
    sparse_vectors = [
        {
            "id": f"{doc_id}_{i}",
            "sparse_values": sparse_emb,
            "metadata": {"text": chunk["text"], "doc_id": doc_id, "page": chunk["page"]},
        }
        for i, (chunk, sparse_emb) in enumerate(zip(chunks, sparse_embeddings, strict=True))
    ]

    index = _get_index()
    sparse_index = _get_sparse_index()
    loop = asyncio.get_event_loop()
    for i in range(0, len(vectors), 100):
        batch, sparse_batch = vectors[i : i + 100], sparse_vectors[i : i + 100]
        await asyncio.gather(
            loop.run_in_executor(None, lambda b=batch: index.upsert(vectors=b, namespace=bot_id)),
            loop.run_in_executor(None, lambda b=sparse_batch: sparse_index.upsert(vectors=b, namespace=bot_id)),
        )
    return len(vectors)


# ── Query rewriting (Task 1.6) ──────────────────────────────────────────────────

_REWRITE_SYSTEM_PROMPT = (
    "Rewrite the following spoken question into a clean, standalone search "
    "query for a document search engine. Fix filler words, false starts, "
    "and spoken number words (e.g. 'page fifty' -> 'page 50'). Do NOT "
    "answer the question, add information, or change its meaning. Output "
    "ONLY the rewritten query text, nothing else."
)


async def rewrite_query(raw_query: str) -> str:
    """Cleans up a messy spoken transcript into a proper search query before
    it's embedded and searched — sits directly in the latency path of every
    RAG lookup, so it deliberately uses Groq's small/fast model
    (`groq_rewrite_model`), not the conversational one.

    This also replaces the old hand-written word-number parsing below:
    the rewriter turns "page fifty" into "page 50" naturally, so
    _extract_page_num only needs to handle the digit form now.

    Falls back to the raw query untouched on any failure (no Groq key,
    API error, empty/junk response) — a slightly messier search beats
    breaking the pipeline over a cleanup step.
    """
    client = _get_groq()
    if client is None:
        return raw_query
    try:
        response = await client.chat.completions.create(
            model=settings.groq_rewrite_model,
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": raw_query},
            ],
            temperature=0,
            max_tokens=100,
            extra_body={"reasoning_effort": "low"},
        )
        rewritten = (response.choices[0].message.content or "").strip()
        return rewritten or raw_query
    except Exception as e:
        logger.warning(f"[RAG] Query rewrite failed, using raw query: {e}")
        return raw_query


# ── Pinecone query ─────────────────────────────────────────────────────────────

def _extract_page_num(query: str) -> int | None:
    # "page 50" — digit form. The word form ("page fifty") used to need a
    # hand-written number-word parser here; rewrite_query() now normalizes
    # that upstream, so this only ever sees digits.
    m = re.search(r'\bpage\s+(\d+)\b', query, re.IGNORECASE)
    return int(m.group(1)) if m else None


# Task 1.7 — reranking. Cast a wide net (RETRIEVE_TOP_K candidates from raw
# vector similarity), then have a cross-encoder actually read the query
# together with each candidate and score real relevance, keeping only
# RERANK_TOP_N. Confirmed live 2026-08-31 this fixes a genuine failure mode:
# for the query "what avoids mistakes when using claude code", the actually-
# correct chunk (page 40) didn't even make the raw top-5 (best raw score was
# an unrelated page at 0.654) — but reranking correctly placed it #1 at
# 0.9047, with a clear gap to the next result (0.7582). Raw cosine similarity
# clusters tightly (0.6-0.65 for both good and irrelevant matches) and can't
# discriminate; the reranker's scores spread out (0.0 for a genuinely
# unrelated query, 0.4-0.9 for real matches) which is what makes a real
# threshold possible — see RERANK_THRESHOLD below.
RETRIEVE_TOP_K = 20
RERANK_TOP_N = 4
# NOT the old 0.2 (that was calibrated for raw cosine similarity scores,
# which land in a totally different range). Confirmed live: an unrelated
# query reranks every candidate to 0.0000; real matches land 0.4-0.9+. 0.15
# sits well inside that gap.
RERANK_THRESHOLD = 0.15
RERANK_MODEL = "bge-reranker-v2-m3"

# Found live 2026-09-13: Pinecone refused every rerank with
# "429 RESOURCE_EXHAUSTED ... reached the rerank request limit (500) ... for
# the current month". The fallback below already handled the refusal, but
# only after paying the round trip for it, on every search, in every call:
# each call is its own process (task 2.4), so nothing in memory could
# remember that the allowance was gone. That wasted round trip is part of why
# searches overran the 3.5s budget and answers went out without the document.
#
# The shared breaker store is what every call process reads, so a used-up
# allowance is recorded there once and respected by all of them. One refusal
# opens it (a quota does not recover in seconds); after the cooldown exactly
# one trial rerank checks whether it came back. Only a QUOTA refusal counts:
# a one-off network error says nothing about the next request, and switching
# reranking off for an hour over one would cost answer quality for nothing.
RERANK_BREAKER = "provider:rerank:pinecone"
RERANK_QUOTA_COOLDOWN_SECONDS = 3600

# Rerank's own deadline, inside the caller's 3.5s retrieval budget
# (rag_processor.RETRIEVAL_BUDGET_SECONDS). Found on the 2026-09-13 retest:
# with no deadline of its own, a slow rerank did not just lose the ranking,
# the budget threw away the whole search, including results already in hand.
#
# Sized from that call's log: rewrite ~0.35s + embed 0.63s + query 0.57s
# = ~1.55s before rerank starts, so 1.2s here still finishes by ~2.75s.
#
# NOT yet measured against a healthy rerank: the monthly allowance ran out
# before per-step timing was logged. The only older number is a whole
# search cycle of ~2.0s (2026-09-03), which would put rerank near 0.45s,
# but that subtracts steps timed on a different day. So a rerank that
# answers after this deadline logs "answered late after Xs": if those show
# up once the allowance resets, this number is too small.
#
# The caller's remaining budget can make the wait shorter still: see the
# deadline parameter of query_context.
RERANK_TIMEOUT_SECONDS = 1.2

# Bounds how long an ABANDONED rerank request keeps its thread (see
# _get_rerank_client). Longer than RERANK_TIMEOUT_SECONDS, so the request is
# never killed while the search is still waiting for it. The SDK passes it to
# httpx, which applies it to each phase (connect, write, read) separately, so
# the real worst case is a few multiples of this, not this: ~5-7.5s rather
# than the SDK's 30s default.
RERANK_REQUEST_TIMEOUT_SECONDS = 2.5

# Below this much time left, a rerank is not sent at all: the answer could
# not come back in time, and sending it would still spend one of the
# month's requests.
RERANK_MIN_WINDOW_SECONDS = 0.3


def _is_quota_refusal(error: Exception) -> bool:
    text = str(error)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


def _rerank_and_record(query: str, candidates: list[str]):
    """Runs in a worker thread: the rerank request AND recording its outcome.

    Recording lives here, not after the await, on purpose. The search may
    stop waiting (its own deadline, or the caller's budget cancelling it),
    but a thread cannot be cancelled: the request still finishes. Found on
    the 2026-09-13 retest, when the budget's CancelledError skipped the
    except block that recorded the refusal, so the breaker could never open
    on the slow refusal it exists for. Recording in the thread means the
    outcome is kept whether or not anyone is still waiting for it.
    """
    try:
        result = _get_rerank_client().inference.rerank(
            model=RERANK_MODEL,
            query=query,
            documents=candidates,
            top_n=RERANK_TOP_N,
            return_documents=True,
        )
    except Exception as e:
        if _is_quota_refusal(e):
            breaker.record_failure(RERANK_BREAKER, "rerank allowance used up")
            logger.warning(
                f"[RAG] Rerank refused (allowance used up) — skipping rerank for "
                f"{RERANK_QUOTA_COOLDOWN_SECONDS // 60} min across all calls: {e}"
            )
        raise
    breaker.record_success(RERANK_BREAKER)
    return result


def _consume_outcome(future: asyncio.Future) -> None:
    # A rerank nobody waits for any more still finishes; reading its
    # exception here stops asyncio reporting "exception was never retrieved"
    # as an error in the middle of a call. Its outcome is already recorded.
    if not future.cancelled():
        future.exception()


def _log_late_answer(started: float):
    """The evidence for whether RERANK_TIMEOUT_SECONDS is too small."""
    def log(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        took = time.perf_counter() - started
        outcome = "refused/failed" if future.exception() is not None else "succeeded"
        logger.info(f"[RAG] Rerank answered late after {took:.2f}s ({outcome}); that turn used raw similarity")
    return log


def _rerank_wait(loop, deadline: float | None) -> float:
    if deadline is None:
        return RERANK_TIMEOUT_SECONDS
    return min(RERANK_TIMEOUT_SECONDS, deadline - loop.time())


async def _rerank_allowed(loop) -> bool:
    # Registered here rather than at import: breaker config can be cleared at
    # runtime (tests do), and a forgotten config would silently fall back to
    # the default 30-second cooldown.
    breaker.configure(RERANK_BREAKER, breaker.BreakerConfig(
        failure_threshold=1,
        window_seconds=RERANK_QUOTA_COOLDOWN_SECONDS,
        cooldown_seconds=RERANK_QUOTA_COOLDOWN_SECONDS,
    ))
    # A local SQLite read, but still off the event loop: this runs inside a
    # live call's reply path (see review finding #8 on breaker reads).
    return await loop.run_in_executor(None, breaker.allows, RERANK_BREAKER)


async def query_context(
    bot_id: str,
    query: str,
    top_k: int = RETRIEVE_TOP_K,
    *,
    rerank: bool = True,
    deadline: float | None = None,
) -> tuple[str, list[dict]]:
    """Returns (context_text, sources).

    Task 2.10 added the second element: one entry per cited (document, page)
    as {"doc_id", "page", "score"}, ordered best-first. Empty list when
    nothing passed the rerank threshold — which is the signal the frontend
    uses to say the answer came from general knowledge rather than from the
    customer's documents.

    rerank=False runs everything except the rerank request. The per-call
    connection warm-up uses it: it exists to open connections, and a full
    search there spent one of Pinecone's monthly rerank requests on the words
    "warm up", on every single call (found 2026-09-13).

    deadline is the caller's own cut-off, on the event loop's clock
    (loop.time()). Rerank waits no longer than the time left before it, and
    is not sent at all when too little is left: a fixed wait alone would let
    slow earlier steps plus a slow rerank lose results already in hand.

    Always logs one "search timing" line, INCLUDING when the search is
    cancelled by the caller's budget, naming the step it stopped in. The live
    log for 2026-09-13 said only "exceeded 3.5s budget", which left the slow
    step to guesswork.
    """
    started = time.perf_counter()
    timings: dict[str, str] = {}
    stage = "embed"
    mark = started

    def finish_step(name: str) -> None:
        nonlocal mark
        now = time.perf_counter()
        timings[name] = f"{now - mark:.2f}s"
        mark = now

    try:
        index = _get_index()
        sparse_index = _get_sparse_index()
        loop = asyncio.get_event_loop()

        page_num = _extract_page_num(query)
        filter_dict = {"page": {"$eq": page_num}} if page_num else None

        # Task 1.8 — hybrid retrieval. Run meaning-based (dense) and keyword-based
        # (sparse) search in parallel, then union the two candidate pools before
        # reranking. This deliberately skips the hand-tuned alpha-weighted score
        # blend the manual describes as the default approach — with Task 1.7's
        # cross-encoder reranker already in place, it's a strictly better
        # combiner than a fixed weight: it actually reads each candidate against
        # the query rather than trusting two differently-scaled raw scores to
        # blend meaningfully. Widening the candidate pool is exactly what dense
        # alone was missing for exact identifiers (order IDs, part numbers) that
        # embeddings represent poorly but keyword search finds directly.
        dense_embeddings, sparse_embeddings = await asyncio.gather(
            _embed([query]), _embed_sparse([query], input_type="query"),
        )
        dense_vector = dense_embeddings[0]
        sparse_vector = sparse_embeddings[0]
        finish_step("embed")

        stage = "query"
        dense_results, sparse_results = await asyncio.gather(
            loop.run_in_executor(
                None,
                lambda: index.query(
                    vector=dense_vector,
                    top_k=top_k,
                    namespace=bot_id,
                    include_metadata=True,
                    filter=filter_dict,
                ),
            ),
            loop.run_in_executor(
                None,
                lambda: sparse_index.query(
                    sparse_vector=sparse_vector,
                    top_k=top_k,
                    namespace=bot_id,
                    include_metadata=True,
                    filter=filter_dict,
                ),
            ),
        )
        finish_step("query")

        for m in dense_results.matches:
            logger.debug(f"[RAG] dense candidate score={m.score:.3f} page={m.metadata.get('page')} text={m.metadata.get('text','')[:80]}")
        for m in sparse_results.matches:
            logger.debug(f"[RAG] sparse candidate score={m.score:.3f} page={m.metadata.get('page')} text={m.metadata.get('text','')[:80]}")

        # Dedupe by id — the same chunk very often surfaces on both sides.
        #
        # Task 2.10 — each chunk's metadata is kept alongside its text so an
        # answer can be attributed back to a document and page. Keyed by TEXT
        # rather than id on purpose: the reranker returns documents by their
        # text content and drops the ids we sent, so text is the only thing
        # that survives the round trip.
        seen_ids: set[str] = set()
        candidates: list[str] = []
        meta_by_text: dict[str, dict] = {}
        for m in list(dense_results.matches) + list(sparse_results.matches):
            if m.id in seen_ids or "text" not in m.metadata:
                continue
            seen_ids.add(m.id)
            text = m.metadata["text"]
            candidates.append(text)
            raw_page = m.metadata.get("page")
            meta_by_text[text] = {
                "doc_id": m.metadata.get("doc_id"),
                # Pinecone returns numeric metadata as float — 7.0 reads badly
                # as a page number.
                "page": int(raw_page) if raw_page is not None else None,
            }

        if not candidates:
            logger.info(f"[RAG] 0 candidates retrieved (page_filter={page_num})")
            timings["rerank"] = "not needed"
            stage = "done"
            return "", []

        stage = "rerank"
        unranked = [(t, None) for t in candidates[:RERANK_TOP_N]]
        # Score is None for unranked results rather than the raw cosine value —
        # those sit on a completely different scale (see RERANK_THRESHOLD) and
        # showing one to a user beside a reranked score would be actively wrong.
        if not rerank:
            scored = unranked
            timings["rerank"] = "off"
        # The time check comes BEFORE the breaker: after a cooldown,
        # breaker.allows() hands one caller the trial and records it, so a
        # search that asked and then declined to send would strand the trial
        # and keep reranking off for another full hour.
        elif deadline is not None and deadline - loop.time() < RERANK_MIN_WINDOW_SECONDS:
            logger.info(
                f"[RAG] Rerank not sent: {max(deadline - loop.time(), 0):.2f}s left of the "
                f"retrieval budget (page_filter={page_num}); using raw similarity"
            )
            scored = unranked
            timings["rerank"] = "skipped (no time left)"
        elif not await _rerank_allowed(loop):
            logger.info(
                "[RAG] Rerank skipped: Pinecone's rerank allowance is used up (breaker open); "
                "using raw similarity until the cooldown ends"
            )
            scored = unranked
            timings["rerank"] = "skipped (allowance used up)"
        else:
            # Re-read after the breaker check, which took a little time. Once
            # allowed, the request is sent even if that dipped the window
            # below the minimum, so a claimed trial always reports back.
            wait = max(_rerank_wait(loop, deadline), 0.0)
            rerank_started = time.perf_counter()
            pending = loop.run_in_executor(None, _rerank_and_record, query, candidates)
            pending.add_done_callback(_consume_outcome)
            # asyncio.wait, not wait_for: on timeout it leaves the request
            # running, so _rerank_and_record can still record what Pinecone
            # says. The thread could not be stopped anyway.
            done, _ = await asyncio.wait({pending}, timeout=wait)
            if not done:
                pending.add_done_callback(_log_late_answer(rerank_started))
                logger.warning(
                    f"[RAG] Rerank did not answer within {wait:.2f}s — using raw "
                    f"similarity for this turn (page_filter={page_num}); its outcome is still "
                    f"recorded when it arrives"
                )
                scored = unranked
                timings["rerank"] = "timed out"
            else:
                try:
                    reranked = pending.result()
                    for r in reranked.data:
                        logger.debug(f"[RAG] reranked score={r.score:.4f} text={r.document['text'][:80]}")
                    scored = [(r.document["text"], r.score) for r in reranked.data if r.score > RERANK_THRESHOLD]
                    logger.info(f"[RAG] {len(scored)}/{len(reranked.data)} reranked matches passed threshold (from {len(candidates)} candidates, page_filter={page_num})")
                    finish_step("rerank")
                except Exception as e:
                    # Fail open: a broken reranker call should degrade to the old
                    # raw-similarity behavior, never break the whole RAG lookup.
                    # A quota refusal was already recorded and logged in the thread.
                    if not _is_quota_refusal(e):
                        logger.warning(f"[RAG] Rerank failed, falling back to raw similarity: {e}")
                    scored = unranked
                    finish_step("rerank")
                    timings["rerank"] += " (failed)"

        # One citation per (document, page). Several retrieved chunks landing on
        # the same page is the common case, and listing it three times reads as
        # a bug rather than as thoroughness.
        sources: list[dict] = []
        seen_pages: set[tuple] = set()
        for text, score in scored:
            meta = meta_by_text.get(text)
            if meta is None:
                continue
            key = (meta["doc_id"], meta["page"])
            if key in seen_pages:
                continue
            seen_pages.add(key)
            sources.append({**meta, "score": score})

        stage = "done"
        return "\n\n".join(t for t, _ in scored), sources
    finally:
        steps = " ".join(f"{name}={timings.get(name, '-')}" for name in ("embed", "query", "rerank"))
        outcome = "completed" if stage == "done" else f"stopped during {stage}"
        logger.info(
            f"[RAG] search timing: {steps} total={time.perf_counter() - started:.2f}s ({outcome})"
        )
# ── Pinecone delete ────────────────────────────────────────────────────────────

async def delete_document_vectors(bot_id: str, doc_id: str, chunk_count: int):
    ids = [f"{doc_id}_{i}" for i in range(chunk_count)]
    if not ids:
        return
    index = _get_index()
    sparse_index = _get_sparse_index()
    loop = asyncio.get_event_loop()
    await asyncio.gather(
        loop.run_in_executor(None, lambda: index.delete(ids=ids, namespace=bot_id)),
        loop.run_in_executor(None, lambda: sparse_index.delete(ids=ids, namespace=bot_id)),
    )
