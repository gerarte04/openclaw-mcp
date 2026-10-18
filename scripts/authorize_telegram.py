"""Manually create or refresh the Telethon session used by the REST service.

Without arguments the script prompts in the terminal. The subcommands split the
same login into non-interactive steps for automation (the Ansible
``authorize.yml`` playbook): each reads its secret from stdin and prints one
JSON status line.

    status     -> {"status": "authorized" | "unauthorized"}
    send-code  (stdin: phone)    -> {"status": "code_sent"}
    sign-in    (stdin: code)     -> {"status": "authorized" | "password_required"}
    password   (stdin: password) -> {"status": "authorized"}

The login uses a separate pending session, so a working session is replaced
only after the new one is authorized.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv
from telethon import TelegramClient, errors

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.common.config import Settings

# A Telegram login code expires within minutes; this only drops stale state.
PENDING_LOGIN_TTL_SECONDS = 15 * 60
# Telethon keeps retrying an unreachable server; fail the step instead.
CONNECT_TIMEOUT_SECONDS = 30


class TelegramAuthorizationError(RuntimeError):
    """Raised when a login step cannot continue."""


def client_for(session_file: Path, config: Settings) -> TelegramClient:
    if config.telegram_api_id <= 0 or not config.telegram_api_hash:
        raise TelegramAuthorizationError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    return TelegramClient(
        str(session_file.with_suffix("")),
        config.telegram_api_id,
        config.telegram_api_hash,
    )


async def connect(client: TelegramClient) -> None:
    try:
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
    except (TimeoutError, OSError) as error:
        raise TelegramAuthorizationError("Telegram servers cannot be reached") from error


class PendingLogin:
    """Files for a login in progress next to the configured session file."""

    def __init__(self, session_file: Path) -> None:
        self.target = session_file
        self.session = session_file.with_name(f"{session_file.stem}-login.session")
        self.state = session_file.with_name(f"{session_file.stem}-login.json")

    def clear(self) -> None:
        for path in (self.session, self.state, Path(f"{self.session}-journal")):
            path.unlink(missing_ok=True)

    def save_state(self, phone: str, phone_code_hash: str) -> None:
        temporary = self.state.with_name(f".{self.state.name}.tmp")
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
            encoding="utf-8",
        ) as state_file:
            json.dump(
                {"phone": phone, "phone_code_hash": phone_code_hash, "created_at": time.time()},
                state_file,
            )
        os.replace(temporary, self.state)

    def load_state(self) -> dict:
        try:
            state = json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TelegramAuthorizationError(
                "No Telegram login in progress; send a code first"
            ) from error
        if time.time() - state.get("created_at", 0) > PENDING_LOGIN_TTL_SECONDS:
            self.clear()
            raise TelegramAuthorizationError("The Telegram login expired; send a new code")
        return state

    def commit(self) -> None:
        os.chmod(self.session, 0o600)
        os.replace(self.session, self.target)
        self.clear()


async def status(config: Settings) -> dict:
    session_file = config.telegram_session_file
    if not session_file.is_file():
        return {"status": "unauthorized"}
    # Check a copy, as the REST service does, so the live session is never written.
    with TemporaryDirectory(prefix="telegram-session-") as directory:
        temporary_session = Path(directory) / "telegram.session"
        shutil.copy2(session_file, temporary_session)
        client = client_for(temporary_session, config)
        await connect(client)
        try:
            if await client.is_user_authorized():
                user = await client.get_me()
                return {"status": "authorized", "user_id": user.id}
            return {"status": "unauthorized"}
        finally:
            await client.disconnect()


async def send_code(config: Settings, phone: str) -> dict:
    if not phone:
        raise TelegramAuthorizationError("A phone number is required")
    pending = PendingLogin(config.telegram_session_file)
    pending.target.parent.mkdir(parents=True, exist_ok=True)
    pending.clear()
    client = client_for(pending.session, config)
    await connect(client)
    try:
        sent = await client.send_code_request(phone)
    finally:
        await client.disconnect()
    pending.save_state(phone, sent.phone_code_hash)
    return {"status": "code_sent"}


async def sign_in(config: Settings, code: str | None = None, password: str | None = None) -> dict:
    pending = PendingLogin(config.telegram_session_file)
    state = pending.load_state()
    client = client_for(pending.session, config)
    await connect(client)
    try:
        try:
            if password is not None:
                user = await client.sign_in(password=password)
            else:
                user = await client.sign_in(
                    phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"]
                )
        except errors.SessionPasswordNeededError:
            return {"status": "password_required"}
    finally:
        await client.disconnect()
    pending.commit()
    return {"status": "authorized", "user_id": user.id}


async def run_step(command: str, config: Settings) -> dict:
    if command == "status":
        return await status(config)
    secret = sys.stdin.readline().rstrip("\r\n")
    # A password may legitimately start or end with spaces; other values may not.
    if command != "password":
        secret = secret.strip()
    if command == "send-code":
        return await send_code(config, secret)
    if not secret:
        raise TelegramAuthorizationError(f"The {command} step needs a value on stdin")
    if command == "sign-in":
        return await sign_in(config, code=secret)
    return await sign_in(config, password=secret)


async def authorize() -> None:
    config = Settings()
    session_file = config.telegram_session_file
    session_file.parent.mkdir(parents=True, exist_ok=True)
    client = client_for(session_file, config)
    try:
        # Safely prompts locally for phone, verification code, and 2FA if required.
        await client.start()
        user = await client.get_me()
        print(f"Telegram session saved to {session_file} for account {user.id}.")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", nargs="?", choices=["status", "send-code", "sign-in", "password"]
    )
    args = parser.parse_args()
    load_dotenv()
    try:
        if args.command is None:
            asyncio.run(authorize())
            return
        result = asyncio.run(run_step(args.command, Settings()))
    except (TelegramAuthorizationError, errors.RPCError, OSError, ValueError) as error:
        print(f"Telegram authorization failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result))


if __name__ == "__main__":
    main()
