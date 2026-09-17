"""Interactive alternative to SETUP_MODE; run only on your own computer."""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import env_int

load_dotenv()


async def main():
    api_id = env_int("API_ID", 0, maximum=2**31-1)
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise ValueError("Сначала заполни API_ID и API_HASH в .env")
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start()
        print("\nSESSION_STRING:\n" + client.session.save())
        print("\nСкопируй строку в Railway Variables. Не запускай одну сессию в двух местах.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
