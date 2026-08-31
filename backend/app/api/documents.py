from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.core.auth import get_current_user
from app.core.deps import get_owned_bot, get_owned_document
from app.models.bot import Bot
from app.models.document import Document
from app.models.user import User
from app.services.rag import parse_pdf, chunk_text, upsert_document, delete_document_vectors

router = APIRouter(tags=["documents"])


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

    doc = Document(
        bot_id=str(bot.id),
        user_id=str(current_user.id),
        filename=file.filename,
        chunk_count=len(chunks),
    )
    await doc.insert()

    await upsert_document(str(bot.id), str(doc.id), chunks)

    return {"id": str(doc.id), "filename": doc.filename, "chunk_count": len(chunks)}


@router.get("/bots/{bot_id}/documents")
async def list_documents(bot: Bot = Depends(get_owned_bot)):
    docs = await Document.find(Document.bot_id == str(bot.id)).to_list()
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
        }
        for d in docs
    ]


@router.delete("/documents/{doc_id}")
async def delete_document(doc: Document = Depends(get_owned_document)):
    await delete_document_vectors(doc.bot_id, str(doc.id), doc.chunk_count)
    await doc.delete()
    return {"status": "deleted"}
