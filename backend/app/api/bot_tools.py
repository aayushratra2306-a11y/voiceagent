"""Task 3.1 — managing a bot's tools as configuration.

Every route here hangs off a bot the caller already owns: `get_owned_bot`
(task 2.6) resolves the bot from the path and refuses if it belongs to
someone else, so ownership is checked once by the dependency rather than
repeated in each handler.

The credential is write-only across this API. It arrives in plain text on
create and update, is encrypted before it touches the database, and is
never sent back — reads return a masked form so someone can recognise
which key they configured without the API ever being a way to retrieve it.
"""

from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.core.deps import get_owned_bot
from app.models.bot import Bot
from app.models.bot_tool import BotTool, ToolAuth, ToolParameter
from app.services.tool_registry import test_tool

router = APIRouter(prefix="/bots/{bot_id}/tools", tags=["tools"])


class AuthIn(BaseModel):
    kind: str = "none"
    name: str = ""
    # Plain text on the way in only. Omit it on an update to keep the
    # stored one — which is what lets the UI show a masked value and still
    # let someone edit the URL without re-typing their key.
    secret: str | None = None


class ToolIn(BaseModel):
    name: str
    description: str
    enabled: bool = True
    long_running: bool = False
    kind: str = "http"
    builtin: str = ""
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    parameters: list[ToolParameter] = Field(default_factory=list)
    auth: AuthIn = Field(default_factory=AuthIn)


def _out(tool: BotTool) -> dict:
    """The API's view of a tool — everything except the actual credential."""
    return {
        "id": str(tool.id),
        "name": tool.name,
        "description": tool.description,
        "enabled": tool.enabled,
        "long_running": tool.long_running,
        "kind": tool.kind,
        "builtin": tool.builtin,
        "method": tool.method,
        "url": tool.url,
        "headers": tool.headers,
        "query": tool.query,
        "body": tool.body,
        "parameters": [p.model_dump() for p in tool.parameters],
        "auth": {
            "kind": tool.auth.kind,
            "name": tool.auth.name,
            # Decrypted only to mask it. The plain value never leaves here.
            "secret_masked": mask_secret(decrypt_secret(tool.auth.secret_encrypted)),
            "has_secret": bool(tool.auth.secret_encrypted),
        },
    }


async def _owned_tool(bot_id: str, tool_id: str) -> BotTool:
    """Load a tool and confirm it belongs to the bot in the path.

    The bot itself is already checked by get_owned_bot; this stops a valid
    tool id from one bot being used against another bot's URL.
    """
    try:
        tool = await BotTool.get(PydanticObjectId(tool_id))
    except Exception:
        tool = None
    if tool is None or tool.bot_id != bot_id:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


@router.get("/")
async def list_tools(bot: Bot = Depends(get_owned_bot)):
    tools = await BotTool.find(BotTool.bot_id == str(bot.id)).to_list()
    return [_out(t) for t in tools]


@router.post("/", status_code=201)
async def create_tool(body: ToolIn, bot: Bot = Depends(get_owned_bot)):
    # The model rejects a name that is not an identifier, which matters
    # because it becomes a function name in the schema sent to the AI.
    if await BotTool.find_one(BotTool.bot_id == str(bot.id), BotTool.name == body.name):
        raise HTTPException(status_code=409, detail=f"This bot already has a tool called {body.name}")

    data = body.model_dump(exclude={"auth"})
    tool = BotTool(
        bot_id=str(bot.id),
        auth=ToolAuth(
            kind=body.auth.kind,
            name=body.auth.name,
            secret_encrypted=encrypt_secret(body.auth.secret or ""),
        ),
        **data,
    )
    await tool.insert()
    return _out(tool)


@router.patch("/{tool_id}")
async def update_tool(body: ToolIn, tool_id: str, bot: Bot = Depends(get_owned_bot)):
    tool = await _owned_tool(str(bot.id), tool_id)

    for field, value in body.model_dump(exclude={"auth"}).items():
        setattr(tool, field, value)

    tool.auth.kind = body.auth.kind
    tool.auth.name = body.auth.name
    # A secret of None means "leave it alone" so the form can be saved
    # without re-entering the key; "" means "clear it".
    if body.auth.secret is not None:
        tool.auth.secret_encrypted = encrypt_secret(body.auth.secret)

    await tool.save()
    return _out(tool)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(tool_id: str, bot: Bot = Depends(get_owned_bot)):
    tool = await _owned_tool(str(bot.id), tool_id)
    await tool.delete()


@router.post("/{tool_id}/test")
async def run_tool_test(
    tool_id: str,
    arguments: dict[str, Any] = Body(default_factory=dict),
    bot: Bot = Depends(get_owned_bot),
):
    """Run the tool once, now, with arguments supplied by hand.

    The manual asks for this explicitly and the reason is practical: without
    it, the only way to discover that a URL or an API key is wrong is to
    place a phone call and listen to the bot fail mid-conversation.
    """
    tool = await _owned_tool(str(bot.id), tool_id)
    return await test_tool(tool, arguments)
