"""PKCE helpers and local refresh-token storage for Spotify authorization."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import urlencode

SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_SCOPES = (
    "user-top-read",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
)


class SpotifyOAuthError(RuntimeError):
    """Raised when OAuth state or the locally stored token is invalid."""


def generate_code_verifier() -> str:
    """Return an RFC 7636-compatible high-entropy code verifier."""
    return secrets.token_urlsafe(64)


def code_challenge(code_verifier: str) -> str:
    """Return the SHA-256 S256 code challenge for a verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    """Return an opaque CSRF state value."""
    return secrets.token_urlsafe(32)


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    verifier: str,
    scopes: tuple[str, ...] = SPOTIFY_SCOPES,
) -> str:
    """Build an Authorization Code with PKCE URL without exposing credentials."""
    if not client_id:
        raise SpotifyOAuthError("SPOTIFY_CLIENT_ID is required")
    return f"{SPOTIFY_AUTHORIZE_URL}?{
        urlencode(
            {
                'response_type': 'code',
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'state': state,
                'code_challenge_method': 'S256',
                'code_challenge': code_challenge(verifier),
                'scope': ' '.join(scopes),
            }
        )
    }"


class RefreshTokenStore:
    """Stores the refresh token in a user-owned file with restrictive permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str | None:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpotifyOAuthError("Spotify token file cannot be read") from error
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise SpotifyOAuthError("Spotify token file permissions are insecure")
            with os.fdopen(descriptor, "r", encoding="utf-8") as token_file:
                payload = json.load(token_file)
        except (OSError, json.JSONDecodeError) as error:
            raise SpotifyOAuthError("Spotify token file cannot be read") from error
        if not isinstance(payload, dict):
            raise SpotifyOAuthError("Spotify token file has no refresh token")
        token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            raise SpotifyOAuthError("Spotify token file has no refresh token")
        return token

    def write(self, refresh_token: str) -> None:
        if not refresh_token:
            raise SpotifyOAuthError("Spotify authorization response has no refresh token")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(16)}.tmp")
        try:
            with os.fdopen(
                os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                ),
                "w",
                encoding="utf-8",
            ) as token_file:
                json.dump({"refresh_token": refresh_token}, token_file)
                token_file.write("\n")
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise SpotifyOAuthError("Spotify refresh token cannot be saved") from error
