"""Manually create or refresh the Telethon session used by the REST service."""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.common.config import Settings


async def authorize() -> None:
    load_dotenv()
    config = Settings()
    session_file = config.telegram_session_file
    session_file.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_file.with_suffix("")),
        config.telegram_api_id,
        config.telegram_api_hash,
    )
    try:
        # Safely prompts locally for phone, verification code, and 2FA if required.
        await client.start()
        user = await client.get_me()
        print(f"Telegram session saved to {session_file} for account {user.id}.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(authorize())
