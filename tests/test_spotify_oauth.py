import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.common.config import Settings
from rest_service.spotify_oauth import (
    RefreshTokenStore,
    SpotifyOAuthError,
    build_authorization_url,
    code_challenge,
)
from scripts.authorize_spotify import finish, pending_path, receive_callback, start


def test_pkce_authorization_url_contains_state_and_s256_challenge() -> None:
    url = build_authorization_url(
        "client-id",
        "http://127.0.0.1:8888/callback",
        "expected-state",
        "verifier",
    )
    query = parse_qs(urlparse(url).query)

    assert query["state"] == ["expected-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [code_challenge("verifier")]
    assert "user-top-read" in query["scope"][0]
    assert "user-library-read" in query["scope"][0]
    assert "playlist-read-private" in query["scope"][0]
    assert "playlist-read-collaborative" in query["scope"][0]


def test_refresh_token_store_uses_private_file_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / "spotify" / "refresh-token.json"
    store = RefreshTokenStore(token_path)

    store.write("refresh-token-value")

    assert store.read() == "refresh-token-value"
    assert token_path.stat().st_mode & 0o077 == 0


def test_refresh_token_store_rejects_invalid_payload(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    token_path.write_text("{}", encoding="utf-8")
    os.chmod(token_path, 0o600)

    with pytest.raises(SpotifyOAuthError, match="no refresh token"):
        RefreshTokenStore(token_path).read()


def test_refresh_token_store_rejects_insecure_existing_file(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("refresh-token-value")
    os.chmod(token_path, 0o644)

    with pytest.raises(SpotifyOAuthError, match="permissions are insecure"):
        RefreshTokenStore(token_path).read()


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://127.0.0.1:8888/callback",
        "http://127.0.0.1:8888/not-callback",
        "http://127.0.0.1:8888/callback?unexpected=value",
    ],
)
def test_callback_rejects_noncanonical_redirect_uri(redirect_uri: str) -> None:
    with pytest.raises(SpotifyOAuthError, match="ending in /callback"):
        receive_callback(redirect_uri)


def make_spotify_config(tmp_path: Path) -> Settings:
    return Settings(
        spotify_client_id="client-id",
        spotify_token_file=tmp_path / "spotify" / "refresh-token.json",
        spotify_accounts_base_url="https://accounts.spotify.test",
    )


def test_two_step_authorization_saves_refresh_token(tmp_path: Path) -> None:
    config = make_spotify_config(tmp_path)
    started = start(config)
    state = parse_qs(urlparse(started["url"]).query)["state"][0]
    pending = pending_path(config)
    assert started["status"] == "pending"
    assert pending.stat().st_mode & 0o077 == 0

    response = httpx.Response(
        200,
        json={"refresh_token": "new-refresh-token"},
        request=httpx.Request("POST", "https://accounts.spotify.test/api/token"),
    )
    with patch("scripts.authorize_spotify.httpx.post", return_value=response) as post:
        result = finish(config, f"http://127.0.0.1:8888/callback?code=the-code&state={state}\n")

    assert result == {"status": "authorized"}
    assert post.call_args.kwargs["data"]["code"] == "the-code"
    assert RefreshTokenStore(config.spotify_token_file).read() == "new-refresh-token"
    assert not pending.exists()
    assert start(config) == {"status": "authorized"}
    assert start(config, force=True)["status"] == "pending"


def test_two_step_authorization_rejects_foreign_state(tmp_path: Path) -> None:
    config = make_spotify_config(tmp_path)
    start(config)

    with pytest.raises(SpotifyOAuthError, match="state did not match"):
        finish(config, "?code=the-code&state=other-state")


def test_finish_requires_pending_authorization(tmp_path: Path) -> None:
    with pytest.raises(SpotifyOAuthError, match="run start first"):
        finish(make_spotify_config(tmp_path), "?code=the-code&state=state")
