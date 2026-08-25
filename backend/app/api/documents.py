from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.core.auth import get_current_user
from app.models.bot import Bot
from app.models.document import Document
from app.models.user import User
from app.services.rag import parse_pdf, chunk_text, upsert_document, delete_document_vectors

router = APIRouter(tags=["documents"])


@router.post("/bots/{bot_id}/documents")
async def upload_document(
    bot_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    bot = await Bot.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    text = parse_pdf(content)
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from PDF")

    chunks = chunk_text(text)

    doc = Document(
        bot_id=bot_id,
        user_id=str(current_user.id),
        filename=file.filename,
        chunk_count=len(chunks),
    )
    await doc.insert()

    await upsert_document(bot_id, str(doc.id), chunks)

    return {"id": str(doc.id), "filename": doc.filename, "chunk_count": len(chunks)}


@router.get("/bots/{bot_id}/documents")
async def list_documents(
    bot_id: str,
    current_user: User = Depends(get_current_user),
):
    bot = await Bot.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    docs = await Document.find(Document.bot_id == bot_id).to_list()
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
async def delete_document(
    doc_id: str,
    current_user: User = Depends(get_current_user),
):
    doc = await Document.get(doc_id)
    if not doc or doc.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Document not found")

    await delete_document_vectors(doc.bot_id, str(doc.id), doc.chunk_count)
    await doc.delete()
    return {"status": "deleted"}
