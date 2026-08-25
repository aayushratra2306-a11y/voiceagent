from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.models.bot import Bot
from app.models.user import User

router = APIRouter(prefix="/bots", tags=["bots"])


class BotCreate(BaseModel):
    name: str
    system_prompt: str = "You are a helpful voice assistant."
    voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091"
    llm_model: str = "gpt-4o-mini"
    language: str = "en"


class BotUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    voice_id: str | None = None
    llm_model: str | None = None
    language: str | None = None


@router.post("/", status_code=201)
async def create_bot(body: BotCreate, current_user: User = Depends(get_current_user)):
    bot = Bot(user_id=str(current_user.id), **body.model_dump())
    await bot.insert()
    return {"id": str(bot.id), "name": bot.name}


@router.get("/")
async def list_bots(current_user: User = Depends(get_current_user)):
    bots = await Bot.find(Bot.user_id == str(current_user.id)).to_list()
    return [{"id": str(b.id), "name": b.name, "llm_model": b.llm_model, "voice_id": b.voice_id} for b in bots]


@router.patch("/{bot_id}")
async def update_bot(bot_id: str, body: BotUpdate, current_user: User = Depends(get_current_user)):
    bot = await Bot.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    await bot.set(update_data)
    return {"message": "Bot updated"}


@router.delete("/{bot_id}", status_code=204)
async def delete_bot(bot_id: str, current_user: User = Depends(get_current_user)):
    bot = await Bot.get(bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")
    await bot.delete()
