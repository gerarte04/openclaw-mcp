"""Single-user OAuth 2.1 authorization server for the public MCP endpoint.

Claude's connector registers itself (RFC 7591 dynamic client registration),
sends the owner's browser to /login, and receives tokens bound to this MCP
server. The MCP SDK implements the protocol endpoints, PKCE, and bearer-token
checks; this module stores clients and tokens and checks the owner's password.

Registered clients and tokens are persisted, as SHA-256 digests of the token
values, so a restart does not disconnect the connector. Login requests and
authorization codes live only in memory: they expire within minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.common.config import Settings

ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
LOGIN_REQUEST_TTL_SECONDS = 10 * 60
# Anyone can start a login, so pending ones are capped; the oldest go first.
MAX_LOGIN_REQUESTS = 100
# Claude registers a new client for each fresh connection; old ones without
# tokens are dropped beyond this limit.
MAX_CLIENTS = 50
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_DELAY_SECONDS = 1.0
LOGIN_PATH = "/login"
MCP_PATH = "/mcp"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class OAuthStore:
    """JSON file of registered clients and token digests, written atomically with 0600."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._data = {}
            for section in ("clients", "access_tokens", "refresh_tokens"):
                self._data.setdefault(section, {})
        return self._data

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                "w",
                encoding="utf-8",
            ) as state_file:
                json.dump(data, state_file)
            os.replace(temporary, self.path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @asynccontextmanager
    async def read(self):
        async with self._lock:
            yield self._load()

    @asynccontextmanager
    async def write(self):
        async with self._lock:
            data = self._load()
            yield data
            now = time.time()
            for section in ("access_tokens", "refresh_tokens"):
                data[section] = {
                    key: record
                    for key, record in data[section].items()
                    if record["expires_at"] > now
                }
            self._save(data)


@dataclass
class LoginRequest:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float


class SingleUserOAuthProvider:
    """OAuthAuthorizationServerProvider whose only user is the owner with MCP_OAUTH_PASSWORD."""

    def __init__(
        self,
        public_url: str,
        password: str,
        state_file: Path,
        allowed_redirect_uris: list[str],
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.resource = f"{self.public_url}{MCP_PATH}"
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self.allowed_redirect_uris = set(allowed_redirect_uris)
        self.store = OAuthStore(state_file)
        self._login_requests: dict[str, LoginRequest] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._login_failures: deque[float] = deque()

    # Client registration

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self.store.read() as data:
            record = data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(record) if record else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirect_uris = {str(uri) for uri in client_info.redirect_uris or []}
        if not redirect_uris or not redirect_uris <= self.allowed_redirect_uris:
            raise RegistrationError(
                "invalid_redirect_uri",
                "Only the redirect URIs in MCP_OAUTH_ALLOWED_REDIRECT_URIS may be registered",
            )
        async with self.store.write() as data:
            data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            self._prune_clients(data)

    @staticmethod
    def _prune_clients(data: dict[str, dict[str, Any]]) -> None:
        if len(data["clients"]) <= MAX_CLIENTS:
            return
        in_use = {record["client_id"] for record in data["refresh_tokens"].values()}
        idle = sorted(
            (record.get("client_id_issued_at") or 0, client_id)
            for client_id, record in data["clients"].items()
            if client_id not in in_use
        )
        for _, client_id in idle[: len(data["clients"]) - MAX_CLIENTS]:
            del data["clients"][client_id]

    # Authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_request", "Tokens are issued only for this MCP server")
        now = time.time()
        self._login_requests = {
            key: request
            for key, request in self._login_requests.items()
            if request.expires_at > now
        }
        while len(self._login_requests) >= MAX_LOGIN_REQUESTS:
            del self._login_requests[next(iter(self._login_requests))]
        request_id = secrets.token_urlsafe(32)
        self._login_requests[request_id] = LoginRequest(
            client, params, now + LOGIN_REQUEST_TTL_SECONDS
        )
        return f"{self.public_url}{LOGIN_PATH}?request={request_id}"

    def login_request(self, request_id: str) -> LoginRequest | None:
        request = self._login_requests.get(request_id)
        if request is None or request.expires_at < time.time():
            self._login_requests.pop(request_id, None)
            return None
        return request

    def login_locked(self) -> bool:
        cutoff = time.time() - LOGIN_FAILURE_WINDOW_SECONDS
        while self._login_failures and self._login_failures[0] < cutoff:
            self._login_failures.popleft()
        return len(self._login_failures) >= LOGIN_FAILURE_LIMIT

    def check_password(self, password: str) -> bool:
        candidate = hashlib.sha256(password.encode("utf-8")).digest()
        if hmac.compare_digest(candidate, self._password_digest):
            self._login_failures.clear()
            return True
        self._login_failures.append(time.time())
        return False

    def complete_login(self, request_id: str) -> str:
        """Issue an authorization code and return the client redirect carrying it."""
        request = self._login_requests.pop(request_id)
        now = time.time()
        self._codes = {key: code for key, code in self._codes.items() if code.expires_at > now}
        code = secrets.token_urlsafe(32)
        scopes = request.params.scopes or (request.client.scope or "").split()
        self._codes[digest(code)] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=now + AUTHORIZATION_CODE_TTL_SECONDS,
            client_id=request.client.client_id,
            code_challenge=request.params.code_challenge,
            redirect_uri=request.params.redirect_uri,
            redirect_uri_provided_explicitly=request.params.redirect_uri_provided_explicitly,
            resource=self.resource,
        )
        return construct_redirect_uri(
            str(request.params.redirect_uri), code=code, state=request.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(digest(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Codes are single-use: a replayed code finds nothing.
        if self._codes.pop(digest(authorization_code.code), None) is None:
            raise TokenError("invalid_grant", "Authorization code was already used")
        return await self._issue_tokens(client.client_id, authorization_code.scopes)

    # Tokens

    async def _issue_tokens(
        self, client_id: str, scopes: list[str], replaced_refresh_digest: str | None = None
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = int(time.time())
        access_digest, refresh_digest = digest(access_token), digest(refresh_token)
        async with self.store.write() as data:
            if replaced_refresh_digest is not None:
                self._remove_pair(data, refresh_digest=replaced_refresh_digest)
            data["access_tokens"][access_digest] = {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + ACCESS_TOKEN_TTL_SECONDS,
                "refresh_digest": refresh_digest,
            }
            data["refresh_tokens"][refresh_digest] = {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + REFRESH_TOKEN_TTL_SECONDS,
                "access_digest": access_digest,
            }
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(scopes) or None,
        )

    @staticmethod
    def _remove_pair(
        data: dict[str, dict[str, Any]],
        access_digest: str | None = None,
        refresh_digest: str | None = None,
    ) -> None:
        if access_digest is not None:
            record = data["access_tokens"].pop(access_digest, None)
            if record is not None:
                refresh_digest = refresh_digest or record["refresh_digest"]
        if refresh_digest is not None:
            record = data["refresh_tokens"].pop(refresh_digest, None)
            if record is not None:
                data["access_tokens"].pop(record["access_digest"], None)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with self.store.read() as data:
            record = data["refresh_tokens"].get(digest(refresh_token))
        if record is None or record["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotation: the old refresh token and its access token stop working.
        return await self._issue_tokens(
            client.client_id, scopes, replaced_refresh_digest=digest(refresh_token.token)
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self.store.read() as data:
            record = data["access_tokens"].get(digest(token))
        if record is None or record["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_digest = digest(token.token)
        async with self.store.write() as data:
            if isinstance(token, AccessToken):
                self._remove_pair(data, access_digest=token_digest)
            else:
                self._remove_pair(data, refresh_digest=token_digest)


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect to OpenClaw MCP</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 26rem; margin: 4rem auto; padding: 0 1rem; }}
input, button {{ font: inherit; width: 100%; box-sizing: border-box; padding: .6rem; margin-top: .5rem; }}
.error {{ color: #b00020; }}
</style>
</head>
<body>
<h1>Connect to OpenClaw MCP</h1>
{body}
</body>
</html>
"""


def login_response(provider: SingleUserOAuthProvider, body: str, status_code: int) -> HTMLResponse:
    form_targets = " ".join(
        sorted(
            {
                f"{urlparse(uri).scheme}://{urlparse(uri).netloc}"
                for uri in provider.allowed_redirect_uris
            }
        )
    )
    return HTMLResponse(
        LOGIN_PAGE.format(body=body),
        status_code=status_code,
        headers={
            # The form posts here, then redirects to the client's callback.
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"form-action 'self' {form_targets}; frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


def login_form(request_id: str, login: LoginRequest, error: str = "") -> str:
    client_name = login.client.client_name or "An MCP client"
    callback_host = urlparse(str(login.params.redirect_uri)).netloc
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<p><strong>{html.escape(client_name)}</strong> ({html.escape(callback_host)})
wants to use this server's Telegram and Spotify tools.</p>
{error_html}
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<label>Password<input type="password" name="password" autocomplete="current-password"
autofocus required></label>
<button type="submit">Allow access</button>
</form>"""


def add_login_route(mcp: Any, provider: SingleUserOAuthProvider) -> None:
    @mcp.custom_route(LOGIN_PATH, methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            password = None
        else:
            form = await request.form()
            request_id = str(form.get("request", ""))
            password = str(form.get("password", ""))
        login_request = provider.login_request(request_id)
        if login_request is None:
            return login_response(
                provider,
                "<p>This sign-in link has expired. Start connecting again from Claude.</p>",
                400,
            )
        if password is None:
            return login_response(provider, login_form(request_id, login_request), 200)
        if provider.login_locked():
            return login_response(
                provider,
                "<p>Too many failed attempts. Try again in 15 minutes.</p>",
                429,
            )
        if not provider.check_password(password):
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            return login_response(
                provider, login_form(request_id, login_request, "Wrong password."), 401
            )
        return RedirectResponse(provider.complete_login(request_id), status_code=302)


def oauth_options(config: Settings) -> tuple[dict[str, Any], SingleUserOAuthProvider | None]:
    """Return FastMCP keyword arguments enabling OAuth when MCP_PUBLIC_URL is set."""
    if not config.mcp_public_url:
        return {}, None
    if len(config.mcp_oauth_password) < 16:
        raise ValueError(
            "MCP_OAUTH_PASSWORD must be at least 16 characters when MCP_PUBLIC_URL is set"
        )
    provider = SingleUserOAuthProvider(
        config.mcp_public_url,
        config.mcp_oauth_password,
        config.mcp_oauth_state_file,
        parse_list(config.mcp_oauth_allowed_redirect_uris),
    )
    auth = AuthSettings(
        issuer_url=provider.public_url,
        resource_server_url=provider.resource,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    return {"auth": auth, "auth_server_provider": provider}, provider
