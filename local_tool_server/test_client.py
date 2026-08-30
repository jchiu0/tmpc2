import asyncio

from mcp.client import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8765/mcp") as client:
        result = await client.call_tool(
            "ask_grok",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "Reply with only the requested answer.",
                    },
                    {
                        "role": "user",
                        "content": "My project codename is Kumquat.",
                    },
                    {
                        "role": "assistant",
                        "content": "Understood.",
                    },
                    {
                        "role": "user",
                        "content": "What is my project codename?",
                    },
                ]
            },
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
