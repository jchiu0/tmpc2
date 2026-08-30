import asyncio

from dotenv import load_dotenv
from xai_sdk import AsyncClient
from xai_sdk.chat import system, user


async def main() -> None:
    load_dotenv()
    client = AsyncClient()
    chat = client.chat.create(
        model="grok-4.6",
        messages=[system("You are a helpful assistant.")],
    )

    while True:
        prompt = input("You: ")
        if prompt.lower() == "exit":
            break

        chat.append(user(prompt))
        response = await chat.sample()
        print(f"Grok: {response.content}")
        chat.append(response)


if __name__ == "__main__":
    asyncio.run(main())
