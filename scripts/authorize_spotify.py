"""Manually authorize Spotify with Authorization Code Flow and PKCE.

Without arguments the script opens a browser and waits for the loopback
redirect. The subcommands split the flow for a remote server (the Ansible
``authorize.yml`` playbook), where the redirect cannot reach the script:

    start  -> {"status": "authorized"} or {"status": "pending", "url": ...}
    finish (stdin: the redirected address) -> {"status": "authorized"}

After approving, the browser opens the redirect URI, which does not load; its
address carries the code and state that ``finish`` reads.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.common.config import Settings
from rest_service.spotify_oauth import (
    RefreshTokenStore,
    SpotifyOAuthError,
    build_authorization_url,
    generate_code_verifier,
    generate_state,
)


class CallbackHandler(BaseHTTPRequestHandler):
    """Capture exactly one loopback OAuth redirect without logging its query."""

    callback_query: dict[str, list[str]] | None = None

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/callback":
            self.send_error(404)
            return
        type(self).callback_query = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Spotify authorization completed. You may close this window.")

    def log_message(self, format: str, *args: object) -> None:
        """Avoid writing authorization codes to the terminal."""


def receive_callback(redirect_uri: str, bind_host: str = "127.0.0.1") -> dict[str, list[str]]:
    parsed = urlparse(redirect_uri)
    try:
        port = parsed.port
    except ValueError as error:
        raise SpotifyOAuthError("SPOTIFY_REDIRECT_URI must include a valid port") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or parsed.path != "/callback"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise SpotifyOAuthError(
            "SPOTIFY_REDIRECT_URI must be a loopback HTTP URL ending in /callback"
        )
    CallbackHandler.callback_query = None
    try:
        server = HTTPServer((bind_host, port), CallbackHandler)
    except OSError as error:
        raise SpotifyOAuthError("Spotify callback server could not be started") from error
    server.timeout = 300
    try:
        server.handle_request()
    finally:
        server.server_close()
    if CallbackHandler.callback_query is None:
        raise SpotifyOAuthError("Timed out waiting for the Spotify authorization callback")
    return CallbackHandler.callback_query


# Spotify authorization codes are short-lived; this only drops stale state.
PENDING_AUTHORIZATION_TTL_SECONDS = 15 * 60


def authorization_code(callback: dict[str, list[str]], state: str) -> str:
    if callback.get("state") != [state] or not secrets.compare_digest(callback["state"][0], state):
        raise SpotifyOAuthError("Spotify callback state did not match")
    if error := callback.get("error", [None])[0]:
        raise SpotifyOAuthError(f"Spotify authorization was denied: {error}")
    code_values = callback.get("code", [])
    if len(code_values) != 1 or not code_values[0]:
        raise SpotifyOAuthError("Spotify callback did not include an authorization code")
    return code_values[0]


def exchange_code(config: Settings, code: str, verifier: str) -> None:
    response = httpx.post(
        f"{config.spotify_accounts_base_url.rstrip('/')}/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.spotify_redirect_uri,
            "client_id": config.spotify_client_id,
            "code_verifier": verifier,
        },
        timeout=config.request_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    RefreshTokenStore(config.spotify_token_file).write(payload.get("refresh_token", ""))


def pending_path(config: Settings) -> Path:
    return config.spotify_token_file.with_name("pending-authorization.json")


def start(config: Settings, force: bool = False) -> dict[str, str]:
    store = RefreshTokenStore(config.spotify_token_file)
    if not force and store.path.is_file():
        store.read()
        return {"status": "authorized"}
    verifier = generate_code_verifier()
    state = generate_state()
    url = build_authorization_url(
        config.spotify_client_id, config.spotify_redirect_uri, state, verifier
    )
    path = pending_path(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w",
        encoding="utf-8",
    ) as pending_file:
        json.dump({"state": state, "verifier": verifier, "created_at": time.time()}, pending_file)
    os.replace(temporary, path)
    return {"status": "pending", "url": url}


def finish(config: Settings, redirected_address: str) -> dict[str, str]:
    path = pending_path(config)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SpotifyOAuthError("No Spotify authorization in progress; run start first") from error
    if time.time() - pending.get("created_at", 0) > PENDING_AUTHORIZATION_TTL_SECONDS:
        path.unlink(missing_ok=True)
        raise SpotifyOAuthError("The Spotify authorization expired; run start again")
    query = urlparse(redirected_address.strip()).query or redirected_address.strip().lstrip("?")
    code = authorization_code(parse_qs(query), pending["state"])
    exchange_code(config, code, pending["verifier"])
    path.unlink(missing_ok=True)
    return {"status": "authorized"}


def authorize() -> None:
    config = Settings()
    verifier = generate_code_verifier()
    state = generate_state()
    url = build_authorization_url(
        config.spotify_client_id, config.spotify_redirect_uri, state, verifier
    )
    print("Open this Spotify authorization URL in a browser:")
    print(url)
    webbrowser.open(url)
    callback = receive_callback(config.spotify_redirect_uri, config.spotify_auth_bind_host)
    exchange_code(config, authorization_code(callback, state), verifier)
    print(f"Spotify refresh token saved to {config.spotify_token_file}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", choices=["start", "finish"])
    parser.add_argument(
        "--force", action="store_true", help="start even if a refresh token is already saved"
    )
    args = parser.parse_args()
    load_dotenv()
    try:
        if args.command is None:
            authorize()
            return
        if args.command == "start":
            result = start(Settings(), force=args.force)
        else:
            result = finish(Settings(), sys.stdin.readline())
    except (SpotifyOAuthError, httpx.HTTPError, OSError) as error:
        print(f"Spotify authorization failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result))


if __name__ == "__main__":
    main()
