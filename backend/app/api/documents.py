from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from app.core.auth import get_current_user
from app.core.deps import get_owned_bot, get_owned_document
from app.db.mongo import database
from app.models.bot import Bot
from app.models.document import Document
from app.models.user import User
from app.services.rag import chunk_text, delete_document_vectors, parse_pdf, upsert_document

router = APIRouter(tags=["documents"])


def _bucket() -> AsyncIOMotorGridFSBucket:
    """GridFS handle for original uploaded PDFs (Task 2.10).

    GridFS rather than a plain document field because MongoDB caps a single
    document at 16 MB, and a PDF knowledge base can exceed that. GridFS
    rather than the local filesystem because the backend runs one process
    per call and is deployed in a container — a file written to local disk
    would be invisible to other workers and gone on the next deploy.
    """
    return AsyncIOMotorGridFSBucket(database)


@router.post("/bots/{bot_id}/documents")
async def upload_document(
    file: UploadFile = File(...),
    bot: Bot = Depends(get_owned_bot),
    current_user: User = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    pages = parse_pdf(content)
    if not any(text.strip() for _, text in pages):
        raise HTTPException(status_code=400, detail="Could not extract text from PDF")

    chunks = chunk_text(pages)

    # Task 2.10 — keep the original bytes. Stored before the Document is
    # inserted so a failure here can't leave a record pointing at a file
    # that was never written.
    file_id = await _bucket().upload_from_stream(
        file.filename,
        content,
        metadata={"bot_id": str(bot.id), "user_id": str(current_user.id)},
    )

    doc = Document(
        bot_id=str(bot.id),
        user_id=str(current_user.id),
        filename=file.filename,
        chunk_count=len(chunks),
        file_id=str(file_id),
    )
    await doc.insert()

    await upsert_document(str(bot.id), str(doc.id), chunks)

    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "chunk_count": len(chunks),
        "has_file": True,
    }


@router.get("/bots/{bot_id}/documents")
async def list_documents(bot: Bot = Depends(get_owned_bot)):
    docs = await Document.find(Document.bot_id == str(bot.id)).to_list()
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
            "has_file": d.file_id is not None,
        }
        for d in docs
    ]


@router.get("/documents/{doc_id}/file")
async def get_document_file(doc: Document = Depends(get_owned_document)):
    """Stream back the original PDF so a citation can open its source page.

    Ownership is enforced by get_owned_document, exactly like every other
    document route — this returns the raw contents of a customer's uploaded
    knowledge base, so it is not a route to leave unauthenticated.
    """
    if not doc.file_id:
        raise HTTPException(
            status_code=404,
            detail="The original file was not stored for this document. "
            "Re-upload it to enable opening the source.",
        )

    try:
        stream = await _bucket().open_download_stream(ObjectId(doc.file_id))
    except Exception as e:
        raise HTTPException(status_code=404, detail="Stored file is missing") from e

    async def chunks():
        while chunk := await stream.readchunk():
            yield chunk

    # inline, not attachment: the point is to open in the browser's PDF
    # viewer at a specific page, not to download a copy. Quotes are stripped
    # from the filename because an unescaped one would break the header.
    safe_name = doc.filename.replace('"', "").replace("\n", "")
    return StreamingResponse(
        chunks(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


@router.delete("/documents/{doc_id}")
async def delete_document(doc: Document = Depends(get_owned_document)):
    await delete_document_vectors(doc.bot_id, str(doc.id), doc.chunk_count)
    if doc.file_id:
        # Best-effort: an orphaned GridFS blob is wasted space, but failing
        # to delete it must not stop the document itself from being removed.
        try:
            await _bucket().delete(ObjectId(doc.file_id))
        except Exception:
            pass
    await doc.delete()
    return {"status": "deleted"}
