import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import grpc
from diskcache import Cache
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from xai_sdk import AsyncClient
from xai_sdk.chat import assistant, system, user


load_dotenv()

HOST = os.getenv("GROK_TOOL_HOST", "127.0.0.1")
PORT = int(os.getenv("GROK_TOOL_PORT", "8765"))
DEFAULT_MODEL = os.getenv("GROK_MODEL", "grok-4.6")
CACHE_DIR = Path(
    os.getenv("GROK_CACHE_DIR", Path(__file__).parent / ".cache" / "grok")
)

mcp = MCPServer(
    "Local Grok",
    instructions="Use ask_grok to send one independent prompt to Grok.",
)

cache = Cache(CACHE_DIR)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


def request_cache_key(model: str, messages: list[dict[str, str]]) -> str:
    request = {"model": model, "messages": messages}
    return hashlib.sha256(
        json.dumps(
            request, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@mcp.tool()
async def ask_grok(
    messages: list[ChatMessage], model: str = DEFAULT_MODEL
) -> str:
    """Send one stateless message list to Grok and return its response."""
    if not messages:
        raise ValueError("messages must not be empty")
    if any(not message.content.strip() for message in messages):
        raise ValueError("message content must not be empty")
    if not os.getenv("XAI_API_KEY"):
        raise RuntimeError("XAI_API_KEY is not set")

    request_messages = [message.model_dump() for message in messages]
    cache_key = request_cache_key(model, request_messages)
    cached = cache.get(cache_key)
    if isinstance(cached, str):
        return cached

    client = AsyncClient()
    chat = client.chat.create(
        model=model,
        messages=[
            system(message.content)
            if message.role == "system"
            else assistant(message.content)
            if message.role == "assistant"
            else user(message.content)
            for message in messages
        ],
    )
    try:
        response = await chat.sample()
    except grpc.aio.AioRpcError as error:
        raise ToolError(
            f"xAI request failed: {error.code().name}: {error.details()}"
        ) from error
    cache.set(cache_key, response.content)
    return response.content


def main() -> None:
    mcp.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
