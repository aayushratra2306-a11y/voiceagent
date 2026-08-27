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


def _get_index():
    global _pc, _index
    if _index is None:
        _pc = Pinecone(api_key=settings.pinecone_api_key)
        _index = _pc.Index(settings.pinecone_index_name)
    return _index


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


# ── Pinecone upsert ────────────────────────────────────────────────────────────

async def upsert_document(bot_id: str, doc_id: str, chunks: list[dict]) -> int:
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]
    embeddings = await _embed(texts)
    vectors = [
        {
            "id": f"{doc_id}_{i}",
            "values": emb,
            "metadata": {"text": chunk["text"], "doc_id": doc_id, "page": chunk["page"]},
        }
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
    ]

    index = _get_index()
    loop = asyncio.get_event_loop()
    for i in range(0, len(vectors), 100):
        batch = vectors[i : i + 100]
        await loop.run_in_executor(
            None, lambda b=batch: index.upsert(vectors=b, namespace=bot_id)
        )
    return len(vectors)


# ── Pinecone query ─────────────────────────────────────────────────────────────

_WORD_NUMS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
    'twenty-one': 21, 'twenty-two': 22, 'twenty-three': 23, 'twenty-four': 24,
    'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
    'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100,
}


def _extract_page_num(query: str) -> int | None:
    # "page 50" — digit form
    m = re.search(r'\bpage\s+(\d+)\b', query, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # "page fifty" / "page twenty three" — word form
    m = re.search(r'\bpage\s+((?:[a-z]+-?[a-z]*\s*){1,3})', query, re.IGNORECASE)
    if m:
        words = m.group(1).lower().strip().split()
        total = 0
        for w in words:
            val = _WORD_NUMS.get(w.rstrip(',.:?!'))
            if val:
                total += val
        if total > 0:
            return total
    return None


async def query_context(bot_id: str, query: str, top_k: int = 5) -> str:
    embeddings = await _embed([query])
    index = _get_index()
    loop = asyncio.get_event_loop()

    page_num = _extract_page_num(query)
    filter_dict = {"page": {"$eq": page_num}} if page_num else None

    results = await loop.run_in_executor(
        None,
        lambda: index.query(
            vector=embeddings[0],
            top_k=top_k,
            namespace=bot_id,
            include_metadata=True,
            filter=filter_dict,
        ),
    )

    for m in results.matches:
        logger.debug(f"[RAG] match score={m.score:.3f} page={m.metadata.get('page')} text={m.metadata.get('text','')[:80]}")

    matches = [
        m.metadata["text"]
        for m in results.matches
        if m.score > 0.2 and "text" in m.metadata
    ]
    logger.info(f"[RAG] {len(matches)}/{len(results.matches)} matches passed threshold (page_filter={page_num})")
    return "\n\n".join(matches)


# ── Pinecone delete ────────────────────────────────────────────────────────────

async def delete_document_vectors(bot_id: str, doc_id: str, chunk_count: int):
    ids = [f"{doc_id}_{i}" for i in range(chunk_count)]
    if not ids:
        return
    index = _get_index()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: index.delete(ids=ids, namespace=bot_id)
    )
