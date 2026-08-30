import asyncio

from mcp.client import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp") as client:
        result = await client.call_tool(
            "ask_grok",
            {"prompt": "Reply with exactly: hello"},
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
