import asyncio
import io
import re

import pypdf
from loguru import logger
from openai import AsyncOpenAI
from pinecone import Pinecone

from app.core.config import settings

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_pc: Pinecone | None = None
_index = None

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
    index = _get_index()  # ensures _pc is initialized
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
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
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
        for i, (chunk, sparse_emb) in enumerate(zip(chunks, sparse_embeddings))
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


async def query_context(bot_id: str, query: str, top_k: int = RETRIEVE_TOP_K) -> str:
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

    for m in dense_results.matches:
        logger.debug(f"[RAG] dense candidate score={m.score:.3f} page={m.metadata.get('page')} text={m.metadata.get('text','')[:80]}")
    for m in sparse_results.matches:
        logger.debug(f"[RAG] sparse candidate score={m.score:.3f} page={m.metadata.get('page')} text={m.metadata.get('text','')[:80]}")

    # Dedupe by id — the same chunk very often surfaces on both sides.
    seen_ids: set[str] = set()
    candidates: list[str] = []
    for m in list(dense_results.matches) + list(sparse_results.matches):
        if m.id in seen_ids or "text" not in m.metadata:
            continue
        seen_ids.add(m.id)
        candidates.append(m.metadata["text"])

    if not candidates:
        logger.info(f"[RAG] 0 candidates retrieved (page_filter={page_num})")
        return ""

    try:
        reranked = await loop.run_in_executor(
            None,
            lambda: _pc.inference.rerank(
                model=RERANK_MODEL,
                query=query,
                documents=candidates,
                top_n=RERANK_TOP_N,
                return_documents=True,
            ),
        )
        for r in reranked.data:
            logger.debug(f"[RAG] reranked score={r.score:.4f} text={r.document['text'][:80]}")
        matches = [r.document["text"] for r in reranked.data if r.score > RERANK_THRESHOLD]
        logger.info(f"[RAG] {len(matches)}/{len(reranked.data)} reranked matches passed threshold (from {len(candidates)} candidates, page_filter={page_num})")
    except Exception as e:
        # Fail open: a broken reranker call should degrade to the old
        # raw-similarity behavior, never break the whole RAG lookup.
        logger.warning(f"[RAG] Rerank failed, falling back to raw similarity: {e}")
        matches = candidates[:RERANK_TOP_N]

    return "\n\n".join(matches)


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
