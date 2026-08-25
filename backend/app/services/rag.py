import asyncio
import io

import pypdf
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

def parse_pdf(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ── Chunking ───────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        chunk = " ".join(words[start : start + chunk_size])
        if len(chunk.strip()) > 50:
            chunks.append(chunk)
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

async def upsert_document(bot_id: str, doc_id: str, chunks: list[str]) -> int:
    if not chunks:
        return 0

    embeddings = await _embed(chunks)
    vectors = [
        {
            "id": f"{doc_id}_{i}",
            "values": emb,
            "metadata": {"text": chunk, "doc_id": doc_id},
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

async def query_context(bot_id: str, query: str, top_k: int = 3) -> str:
    embeddings = await _embed([query])
    index = _get_index()
    loop = asyncio.get_event_loop()

    results = await loop.run_in_executor(
        None,
        lambda: index.query(
            vector=embeddings[0],
            top_k=top_k,
            namespace=bot_id,
            include_metadata=True,
        ),
    )

    matches = [
        m.metadata["text"]
        for m in results.matches
        if m.score > 0.65 and "text" in m.metadata
    ]
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
