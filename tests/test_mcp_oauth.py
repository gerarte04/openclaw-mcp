import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from app.common.config import Settings
from mcp_server import oauth
from mcp_server.oauth import add_login_route, oauth_options

PUBLIC_URL = "http://localhost"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "correct horse battery staple"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "mcp_public_url": PUBLIC_URL,
        "mcp_oauth_password": PASSWORD,
        "mcp_oauth_state_file": tmp_path / "oauth" / "state.json",
    }
    return Settings(**(values | overrides))


def make_client(config: Settings) -> TestClient:
    kwargs, provider = oauth_options(config)
    server = FastMCP("test", host="0.0.0.0", streamable_http_path="/mcp", **kwargs)
    add_login_route(server, provider)

    @server.tool()
    def ping() -> str:
        return "pong"

    return TestClient(server.streamable_http_app(), follow_redirects=False)


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "LOGIN_FAILURE_DELAY_SECONDS", 0)


def register(client: TestClient, redirect_uri: str = CALLBACK):
    return client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


def start_login(client: TestClient, client_id: str, **params: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": f"{PUBLIC_URL}/mcp",
        }
        | params,
    )
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    return parse_qs(location.query)["request"][0]


def log_in(client: TestClient, request_id: str, password: str = PASSWORD):
    return client.post("/login", data={"request": request_id, "password": password})


def obtain_tokens(client: TestClient) -> tuple[str, dict]:
    client_id = register(client).json()["client_id"]
    response = log_in(client, start_login(client, client_id))
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.headers["location"].startswith(CALLBACK)
    assert query["state"] == ["client-state"]
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "code_verifier": VERIFIER,
            "client_id": client_id,
            "redirect_uri": CALLBACK,
        },
    )
    assert tokens.status_code == 200
    return client_id, tokens.json() | {"code": query["code"][0]}


def call_mcp(client: TestClient, access_token: str | None):
    headers = MCP_HEADERS | ({"Authorization": f"Bearer {access_token}"} if access_token else {})
    return client.post("/mcp", json=INITIALIZE, headers=headers)


def test_metadata_points_clients_to_this_authorization_server(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
        server = client.get("/.well-known/oauth-authorization-server").json()
        unauthorized = call_mcp(client, None)

    assert resource["resource"] == f"{PUBLIC_URL}/mcp"
    assert resource["authorization_servers"] == [f"{PUBLIC_URL}/"]
    assert server["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert "S256" in server["code_challenge_methods_supported"]
    assert unauthorized.status_code == 401
    assert "oauth-protected-resource/mcp" in unauthorized.headers["www-authenticate"]


def test_full_login_issues_tokens_that_open_mcp(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id, tokens = obtain_tokens(client)
        ok = call_mcp(client, tokens["access_token"])
        wrong = call_mcp(client, "not-a-token")
        replay = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": tokens["code"],
                "code_verifier": VERIFIER,
                "client_id": client_id,
                "redirect_uri": CALLBACK,
            },
        )

    assert ok.status_code == 200
    assert "protocolVersion" in ok.text
    assert wrong.status_code == 401
    assert replay.status_code == 400
    state = (tmp_path / "oauth" / "state.json").read_text()
    assert tokens["access_token"] not in state and tokens["refresh_token"] not in state
    assert (tmp_path / "oauth" / "state.json").stat().st_mode & 0o077 == 0


def test_refresh_rotates_tokens(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id, tokens = obtain_tokens(client)

        def refresh(refresh_token: str):
            return client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )

        rotated = refresh(tokens["refresh_token"])
        reused = refresh(tokens["refresh_token"])
        old_access = call_mcp(client, tokens["access_token"])
        new_access = call_mcp(client, rotated.json()["access_token"])

    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != tokens["refresh_token"]
    assert reused.status_code == 400
    assert old_access.status_code == 401
    assert new_access.status_code == 200


def test_tokens_survive_restart(tmp_path: Path) -> None:
    config = make_settings(tmp_path)
    with make_client(config) as client:
        _, tokens = obtain_tokens(client)
    with make_client(config) as client:
        assert call_mcp(client, tokens["access_token"]).status_code == 200


def test_revoked_token_stops_working(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id, tokens = obtain_tokens(client)
        # The SDK's revocation request model requires client_secret even for
        # public clients, so an empty one is sent.
        revoked = client.post(
            "/revoke",
            data={"token": tokens["refresh_token"], "client_id": client_id, "client_secret": ""},
        )
        after = call_mcp(client, tokens["access_token"])

    assert revoked.status_code == 200
    assert after.status_code == 401


def test_registration_accepts_only_allowed_redirect_uris(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        response = register(client, "https://attacker.example/callback")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_login_page_shows_client_and_blocks_framing(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id = register(client).json()["client_id"]
        page = client.get("/login", params={"request": start_login(client, client_id)})
        expired = client.get("/login", params={"request": "unknown"})

    assert page.status_code == 200
    assert "Claude" in page.text and "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert (
        "form-action 'self' https://claude.ai https://claude.com"
        in (page.headers["content-security-policy"])
    )
    assert expired.status_code == 400


def test_wrong_passwords_lock_login(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id = register(client).json()["client_id"]
        request_id = start_login(client, client_id)
        failures = [log_in(client, request_id, "wrong").status_code for _ in range(5)]
        locked = log_in(client, request_id)

    assert failures == [401] * 5
    assert locked.status_code == 429


def test_tokens_are_issued_only_for_this_server(tmp_path: Path) -> None:
    with make_client(make_settings(tmp_path)) as client:
        client_id = register(client).json()["client_id"]
        response = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "code_challenge": CHALLENGE,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": "https://other.example/mcp",
            },
        )

    assert response.status_code == 302
    assert "error=invalid_request" in response.headers["location"]


def test_oauth_is_off_without_public_url_and_requires_a_strong_password(tmp_path: Path) -> None:
    assert oauth_options(make_settings(tmp_path, mcp_public_url="")) == ({}, None)
    with pytest.raises(ValueError, match="at least 16"):
        oauth_options(make_settings(tmp_path, mcp_oauth_password="short"))


def test_pending_logins_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "MAX_LOGIN_REQUESTS", 2)
    with make_client(make_settings(tmp_path)) as client:
        client_id = register(client).json()["client_id"]
        first, second, third = (start_login(client, client_id) for _ in range(3))
        statuses = [
            client.get("/login", params={"request": request_id}).status_code
            for request_id in (first, second, third)
        ]

    assert statuses == [400, 200, 200]
