import asyncio
from typing import List

from fastapi import APIRouter, Depends
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.deps import fetch_owned_bot
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
    # Task 2.6: bot_id here comes from the request body, not the URL path,
    # so it uses fetch_owned_bot directly rather than the get_owned_bot
    # FastAPI dependency (which resolves bot_id from a path parameter).
    bot = await fetch_owned_bot(body.bot_id, current_user)

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
                    bot_id=str(bot.id),
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
