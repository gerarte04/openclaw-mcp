import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from rest_service.spotify_oauth import (
    RefreshTokenStore,
    SpotifyOAuthError,
    build_authorization_url,
    code_challenge,
)
from scripts.authorize_spotify import receive_callback


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
