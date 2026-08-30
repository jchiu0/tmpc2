import asyncio
import os
from typing import Any

import grpc
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from xai_sdk import AsyncClient
from xai_sdk.chat import system, user


load_dotenv()

HOST = os.getenv("GROK_TOOL_HOST", "127.0.0.1")
PORT = int(os.getenv("GROK_TOOL_PORT", "8765"))
DEFAULT_MODEL = os.getenv("GROK_MODEL", "grok-4.6")
SYSTEM_PROMPT = os.getenv(
    "GROK_SYSTEM_PROMPT", "You are a concise, helpful assistant."
)

mcp = MCPServer(
    "Local Grok",
    instructions="Use ask_grok to talk with Grok in a persistent conversation.",
)

client: AsyncClient | None = None
chat: Any | None = None
chat_lock = asyncio.Lock()


@mcp.tool()
async def ask_grok(prompt: str) -> str:
    """Send a message to Grok and return its response.

    The local server retains conversation history between calls.
    """
    global client, chat

    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not os.getenv("XAI_API_KEY"):
        raise RuntimeError("XAI_API_KEY is not set")

    async with chat_lock:
        if client is None:
            client = AsyncClient()
        if chat is None:
            chat = client.chat.create(
                model=DEFAULT_MODEL,
                messages=[system(SYSTEM_PROMPT)],
            )

        chat.append(user(prompt))
        try:
            response = await chat.sample()
        except grpc.aio.AioRpcError as error:
            raise ToolError(
                f"xAI request failed: {error.code().name}: {error.details()}"
            ) from error
        chat.append(response)
        return response.content


def main() -> None:
    mcp.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
