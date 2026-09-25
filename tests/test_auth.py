import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from reinvent_agent.events_api import auth
from reinvent_agent.events_api.auth import (
    AuthError,
    FileTokenStore,
    SecretsManagerTokenStore,
    TokenProvider,
    Tokens,
)


def test_pkce_verifier_and_challenge():
    v = auth.new_code_verifier()
    assert 43 <= len(v) <= 128
    assert set(v) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert auth.code_challenge(v) == expected
    assert auth.new_code_verifier() != v


def test_authorization_url_has_required_params():
    q = parse_qs(urlparse(auth.authorization_url("chal", "st")).query)
    assert q["identity_provider"] == ["AWSBuilderID"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://localhost:8484/callback"]
    assert q["scope"] == ["openid email events/access"]
    assert q["state"] == ["st"]


def test_file_store_is_private(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(Tokens("a", "r", 1.0))
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load() == Tokens("a", "r", 1.0)
    store.clear()
    assert store.load() is None


def _token_http(response_body, seen):
    def handler(req: httpx.Request):
        seen.append(parse_qs(req.content.decode()))
        return httpx.Response(200, json=response_body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refresh_persists_rotated_refresh_token(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(Tokens("old", "refresh-old", time.time() - 10))
    seen = []
    http = _token_http(
        {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600}, seen
    )
    assert TokenProvider(store, http=http).access_token() == "new"
    assert seen[0]["grant_type"] == ["refresh_token"]
    assert seen[0]["refresh_token"] == ["refresh-old"]
    assert store.load().refresh_token == "refresh-new"


def test_refresh_keeps_refresh_token_when_not_rotated(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(Tokens("old", "refresh-keep", time.time() - 10))
    http = _token_http({"access_token": "new", "expires_in": 3600}, [])
    TokenProvider(store, http=http).access_token()
    assert store.load().refresh_token == "refresh-keep"


def test_valid_token_not_refreshed(token_store):
    def boom(req):
        raise AssertionError("should not call token endpoint")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    assert TokenProvider(token_store, http=http).access_token() == "access-1"


def test_not_signed_in(tmp_path):
    with pytest.raises(AuthError):
        TokenProvider(FileTokenStore(tmp_path / "none.json")).access_token()


def test_secrets_manager_store_roundtrip():
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="tok", SecretString="{}")
        store = SecretsManagerTokenStore("tok", client=sm)
        assert store.load() is None
        store.save(Tokens("a", "r", 5.0))
        assert store.load() == Tokens("a", "r", 5.0)
        assert (
            json.loads(sm.get_secret_value(SecretId="tok")["SecretString"])["refresh_token"] == "r"
        )


def test_login_binds_next_reserved_port_and_exchanges_with_same_uri(tmp_path, monkeypatch):
    import socket
    import threading

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        blocker.bind(("localhost", 8484))
        blocker.listen()
    except OSError:
        pytest.skip("port 8484 unavailable in this environment")

    captured = {}

    def fake_open(url):
        q = parse_qs(urlparse(url).query)
        captured["redirect_uri"] = q["redirect_uri"][0]

        def hit():
            cb = f"{q['redirect_uri'][0]}?code=abc&state={q['state'][0]}"
            httpx.get(cb.replace("localhost", "127.0.0.1"))

        threading.Timer(0.2, hit).start()
        return True

    def fake_exchange(code, verifier, port, http=None):
        captured["exchange"] = (code, auth.redirect_uri(port))
        return Tokens("a", "r", time.time() + 3600)

    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    monkeypatch.setattr(auth, "exchange_code", fake_exchange)
    store = FileTokenStore(tmp_path / "t.json")
    try:
        auth.interactive_login(store, timeout=10)
    finally:
        blocker.close()
    assert captured["redirect_uri"] == "http://localhost:8485/callback"
    assert captured["exchange"] == ("abc", "http://localhost:8485/callback")
    assert store.load().access_token == "a"


def test_revoke_posts_refresh_token():
    seen = []
    auth.revoke("refresh-x", http=_token_http({}, seen))
    assert seen[0] == {"client_id": [auth.CLIENT_ID], "token": ["refresh-x"]}
