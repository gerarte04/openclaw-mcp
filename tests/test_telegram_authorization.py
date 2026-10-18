import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from telethon import errors

from app.common.config import Settings
from scripts import authorize_telegram
from scripts.authorize_telegram import (
    PendingLogin,
    TelegramAuthorizationError,
    send_code,
    sign_in,
    status,
)


class FakeTelegramClient:
    """Mimics the Telethon calls the login steps make, one instance per process."""

    password_required = False
    sign_in_calls: ClassVar[list[dict]] = []

    def __init__(self, session: str, api_id: int, api_hash: str) -> None:
        self.session_file = Path(f"{session}.session")

    async def connect(self) -> None:
        self.session_file.touch()

    async def disconnect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return self.session_file.read_text() == "authorized"

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=42)

    async def send_code_request(self, phone: str) -> SimpleNamespace:
        return SimpleNamespace(phone_code_hash="code-hash")

    async def sign_in(self, **kwargs) -> SimpleNamespace:
        type(self).sign_in_calls.append(kwargs)
        if "code" in kwargs and type(self).password_required:
            raise errors.SessionPasswordNeededError(request=None)
        self.session_file.write_text("authorized")
        return SimpleNamespace(id=42)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(authorize_telegram, "TelegramClient", FakeTelegramClient)
    FakeTelegramClient.password_required = False
    FakeTelegramClient.sign_in_calls = []
    return Settings(
        telegram_api_id=1,
        telegram_api_hash="test-hash",
        telegram_session_file=tmp_path / "data" / "telegram.session",
    )


async def test_code_login_replaces_session_only_when_authorized(config: Settings) -> None:
    assert await status(config) == {"status": "unauthorized"}

    assert await send_code(config, "+10000000000") == {"status": "code_sent"}
    pending = PendingLogin(config.telegram_session_file)
    assert json.loads(pending.state.read_text())["phone_code_hash"] == "code-hash"
    assert pending.state.stat().st_mode & 0o077 == 0
    assert not config.telegram_session_file.exists()

    assert await sign_in(config, code="12345") == {"status": "authorized", "user_id": 42}
    assert FakeTelegramClient.sign_in_calls == [
        {"phone": "+10000000000", "code": "12345", "phone_code_hash": "code-hash"}
    ]
    assert config.telegram_session_file.stat().st_mode & 0o077 == 0
    assert not pending.session.exists() and not pending.state.exists()
    assert await status(config) == {"status": "authorized", "user_id": 42}


async def test_two_factor_login_asks_for_password(config: Settings) -> None:
    FakeTelegramClient.password_required = True
    await send_code(config, "+10000000000")

    assert await sign_in(config, code="12345") == {"status": "password_required"}
    assert not config.telegram_session_file.exists()
    assert await sign_in(config, password="secret") == {"status": "authorized", "user_id": 42}
    assert FakeTelegramClient.sign_in_calls[-1] == {"password": "secret"}
    assert config.telegram_session_file.exists()


async def test_sign_in_requires_sent_code(config: Settings) -> None:
    with pytest.raises(TelegramAuthorizationError, match="send a code first"):
        await sign_in(config, code="12345")


async def test_expired_login_is_discarded(config: Settings, monkeypatch) -> None:
    await send_code(config, "+10000000000")
    monkeypatch.setattr(authorize_telegram, "PENDING_LOGIN_TTL_SECONDS", -1)

    with pytest.raises(TelegramAuthorizationError, match="expired"):
        await sign_in(config, code="12345")
    assert not PendingLogin(config.telegram_session_file).state.exists()
