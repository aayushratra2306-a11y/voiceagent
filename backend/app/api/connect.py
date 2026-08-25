import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.models.bot import Bot
from app.models.user import User
from app.pipeline.voice_pipeline import run_voice_pipeline

router = APIRouter(tags=["voice"])

_handler = SmallWebRTCRequestHandler()


class WebRTCOffer(BaseModel):
    bot_id: str
    sdp: str
    type: str
    pc_id: str | None = None


class IceCandidateBody(BaseModel):
    candidate: str
    sdp_mid: str
    sdp_mline_index: int


class IcePatchBody(BaseModel):
    pc_id: str
    candidates: List[IceCandidateBody]


@router.post("/connect")
async def connect(body: WebRTCOffer, current_user: User = Depends(get_current_user)):
    bot = await Bot.get(body.bot_id)
    if not bot or bot.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Bot not found")

    request = SmallWebRTCRequest(sdp=body.sdp, type=body.type, pc_id=body.pc_id)

    async def start_pipeline(webrtc_connection: SmallWebRTCConnection):
        async def _run():
            try:
                await run_voice_pipeline(
                    webrtc_connection=webrtc_connection,
                    bot_name=bot.name,
                    system_prompt=bot.system_prompt,
                    voice_id=bot.voice_id,
                    llm_model=bot.llm_model,
                    language=bot.language,
                )
            except Exception as e:
                import traceback
                print(f"\n[PIPELINE ERROR] {e}\n{traceback.format_exc()}")
        asyncio.create_task(_run())

    answer = await _handler.handle_web_request(request, start_pipeline)
    return {"sdp": answer["sdp"], "type": answer["type"], "pc_id": answer["pc_id"]}


@router.post("/connect/ice")
async def ice_candidate(body: IcePatchBody, current_user: User = Depends(get_current_user)):
    candidates = [
        IceCandidate(
            candidate=c.candidate,
            sdp_mid=c.sdp_mid,
            sdp_mline_index=c.sdp_mline_index,
        )
        for c in body.candidates
    ]
    patch = SmallWebRTCPatchRequest(pc_id=body.pc_id, candidates=candidates)
    await _handler.handle_patch_request(patch)
    return {"status": "ok"}
