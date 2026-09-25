"""AWS Builder ID sign-in for the AWS Events API.

Sign-in is OAuth 2.0 authorization code + PKCE against oauth.awsevents.com with a
public, shared client ID. The only accepted redirect URI is a localhost loopback,
so the interactive sign-in must run on the attendee's machine. After that:

* access tokens (JWT) live 60 minutes;
* refresh tokens are opaque, live 30 days, and may rotate on every refresh:
  when the token response carries a new refresh token, the old one may stop working.

That is what makes the unattended Oct 8 reservation run possible: sign in locally
once, push the refresh token to a shared ``TokenStore`` (Secrets Manager), and let
the cloud side refresh -- always writing a rotated refresh token back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://oauth.awsevents.com/oauth2/authorize"
TOKEN_URL = "https://oauth.awsevents.com/oauth2/token"
CLIENT_ID = "7vmom55m1qstvq8i71ph127bfq"
REDIRECT_PORT = 8484
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "openid email events/access"
IDENTITY_PROVIDER = "AWSBuilderID"

# Refresh a little before the hour is up so a request never races expiry.
EXPIRY_SKEW_SECONDS = 120


class AuthError(RuntimeError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at - EXPIRY_SKEW_SECONDS

    @classmethod
    def from_token_response(
        cls, body: dict, previous_refresh_token: str | None = None, now: float | None = None
    ) -> Tokens:
        refresh = body.get("refresh_token") or previous_refresh_token
        if not refresh:
            raise AuthError("token response carried no refresh_token")
        return cls(
            access_token=body["access_token"],
            refresh_token=refresh,
            expires_at=(now if now is not None else time.time())
            + int(body.get("expires_in", 3600)),
        )


# --- PKCE -------------------------------------------------------------------


def new_code_verifier() -> str:
    # 64 random bytes -> 86 base64url chars, inside the 43..128 range.
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "identity_provider": IDENTITY_PROVIDER,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --- token stores -----------------------------------------------------------


class TokenStore(Protocol):
    def load(self) -> Tokens | None: ...

    def save(self, tokens: Tokens) -> None: ...

    def clear(self) -> None: ...


class FileTokenStore:
    """Local store, readable only by the current user."""

    def __init__(self, path: Path | None = None):
        default = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = path or default / "reinvent-agent" / "tokens.json"

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        return Tokens(**json.loads(self.path.read_text()))

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(tokens), f)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class SecretsManagerTokenStore:
    """Shared store for the cloud side (reservation Lambda, agent runtime)."""

    def __init__(self, secret_id: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("secretsmanager")
        self.secret_id = secret_id
        self.client = client

    def load(self) -> Tokens | None:
        try:
            value = self.client.get_secret_value(SecretId=self.secret_id)["SecretString"]
        except self.client.exceptions.ResourceNotFoundException:
            return None
        data = json.loads(value)
        return Tokens(**data) if data else None

    def save(self, tokens: Tokens) -> None:
        self.client.put_secret_value(
            SecretId=self.secret_id, SecretString=json.dumps(asdict(tokens))
        )

    def clear(self) -> None:
        self.client.put_secret_value(SecretId=self.secret_id, SecretString="{}")


# --- token provider ---------------------------------------------------------


class TokenProvider:
    """Hands out a valid access token, refreshing (and persisting rotation) as needed."""

    def __init__(self, store: TokenStore, http: httpx.Client | None = None):
        self.store = store
        self.http = http or httpx.Client(timeout=30)
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            tokens = self.store.load()
            if tokens is None:
                raise AuthError("not signed in; run `reinvent-agent auth login`")
            if tokens.is_expired():
                tokens = self._refresh(tokens)
            return tokens.access_token

    def force_refresh(self) -> str:
        with self._lock:
            tokens = self.store.load()
            if tokens is None:
                raise AuthError("not signed in; run `reinvent-agent auth login`")
            return self._refresh(tokens).access_token

    def _refresh(self, tokens: Tokens) -> Tokens:
        resp = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": tokens.refresh_token,
            },
        )
        if resp.status_code != 200:
            raise AuthError(
                f"refresh failed ({resp.status_code}); sign in again. {resp.text[:200]}"
            )
        new = Tokens.from_token_response(resp.json(), previous_refresh_token=tokens.refresh_token)
        # Persist before use: a rotated refresh token is the only one that works now.
        self.store.save(new)
        return new


# --- interactive login ------------------------------------------------------


def exchange_code(code: str, verifier: str, http: httpx.Client | None = None) -> Tokens:
    http = http or httpx.Client(timeout=30)
    resp = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        },
    )
    if resp.status_code != 200:
        raise AuthError(f"code exchange failed ({resp.status_code}): {resp.text[:200]}")
    return Tokens.from_token_response(resp.json())


def _wait_for_callback(expected_state: str, timeout: float) -> str:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            result.update(qs)
            ok = "code" in qs and qs.get("state") == expected_state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if ok else "Sign-in failed."
            self.wfile.write(msg.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", REDIRECT_PORT), Handler)
    server.timeout = 1
    deadline = time.time() + timeout
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise AuthError(f"sign-in error: {result['error']} {result.get('error_description', '')}")
    if "code" not in result:
        raise AuthError("timed out waiting for sign-in")
    if result.get("state") != expected_state:
        raise AuthError("state mismatch on callback; possible CSRF, aborting")
    return result["code"]


def interactive_login(store: TokenStore, timeout: float = 300, open_browser: bool = True) -> Tokens:
    verifier = new_code_verifier()
    state = secrets.token_urlsafe(24)
    url = authorization_url(code_challenge(verifier), state)
    print(f"Opening browser for AWS Builder ID sign-in:\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    code = _wait_for_callback(state, timeout)
    tokens = exchange_code(code, verifier)
    store.save(tokens)
    return tokens
