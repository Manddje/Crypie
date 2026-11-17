"""OAuth for the MCP endpoint (app/auth/oauth.py, app/auth/oauth_routes.py)."""

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app.auth import oauth
from app.db import OAuthClient, OAuthCode, OAuthToken, SessionLocal, init_db
from tests.test_api import api  # noqa: F401  (fixture: login off)
from tests.test_auth import PASSWORD, _setup
from tests.test_mcp import rpc
from tests.test_security_auth import web  # noqa: F401  (fixture: login on)

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
REGISTRATION = {
    "client_name": "Claude",
    "redirect_uris": [CALLBACK],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
RESOURCE = "http://test/mcp"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _clear() -> None:
    with SessionLocal() as db:
        for model in (OAuthToken, OAuthCode, OAuthClient):
            db.query(model).delete()
        db.commit()


async def _register(client, **overrides) -> dict:
    r = await client.post("/oauth/register", json={**REGISTRATION, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def _params(client_id: str, challenge: str, **extra) -> dict:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "resource": RESOURCE,
        **extra,
    }
    return {k: v for k, v in params.items() if v is not None}


def _authorize_url(client_id: str, challenge: str, **extra) -> str:
    return "/oauth/authorize?" + urlencode(_params(client_id, challenge, **extra))


def _redirect_query(response) -> dict:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(CALLBACK + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _allow(client, client_id: str, challenge: str, **extra):
    data = {**_params(client_id, challenge, **extra), "decision": "allow", "password": PASSWORD}
    return _redirect_query(await client.post("/oauth/authorize", data=data))


async def _exchange(client, client_id: str, code: str, verifier: str, **extra):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
        **extra,
    }
    return await client.post("/oauth/token", data=data)


async def _grant(client, client_id: str) -> dict:
    """Consent -> code -> tokens, as Claude does after registering."""
    verifier, challenge = _pkce()
    page = await client.get(_authorize_url(client_id, challenge))
    assert page.status_code == 200 and "Claude wil Crypie gebruiken" in page.text
    # The page allows the way back to the app after the POST (Chrome checks form-action on
    # the redirect); everything else in the policy stays as it is.
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://claude.ai;" in csp and "script-src 'self';" in csp
    query = await _allow(client, client_id, challenge)
    assert query["state"] == "xyz" and "code" in query
    r = await _exchange(client, client_id, query["code"], verifier)
    assert r.status_code == 200, r.text
    return r.json()


async def _refresh(client, client_id: str, refresh: str):
    data = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
    return await client.post("/oauth/token", data=data)


async def _ping(client, token: str) -> int:
    return (await rpc(client, "ping", authorization=f"Bearer {token}")).status_code


# -- discovery and registration (no login needed) ---------------------------------------------


async def test_metadata_documents(api):  # noqa: F811
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        r = await api.get(path)
        assert r.status_code == 200
        assert r.json()["resource"] == RESOURCE
        assert r.json()["authorization_servers"] == ["http://test"]
    r = await api.get("/.well-known/oauth-authorization-server")
    meta = r.json()
    assert meta["issuer"] == "http://test"
    assert meta["authorization_endpoint"] == "http://test/oauth/authorize"
    assert meta["token_endpoint"] == "http://test/oauth/token"
    assert meta["registration_endpoint"] == "http://test/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert "refresh_token" in meta["grant_types_supported"]


async def test_registration(api):  # noqa: F811
    _clear()
    try:
        reg = await _register(api, token_endpoint_auth_method="client_secret_post")
        assert reg["client_name"] == "Claude" and reg["redirect_uris"] == [CALLBACK]
        assert reg["token_endpoint_auth_method"] == "none" and "client_secret" not in reg
        assert reg["client_id_issued_at"] > 0 and len(reg["client_id"]) >= 24

        for bad in (
            {"redirect_uris": []},
            {"redirect_uris": ["http://evil.example/cb"]},
            {"redirect_uris": ["https://user:pw@claude.ai/cb"]},
            {"redirect_uris": ["https://claude.ai/cb#frag"]},
            {"redirect_uris": [CALLBACK] * 6},
            {"grant_types": ["implicit"]},
            {"response_types": ["token"]},
        ):
            r = await api.post("/oauth/register", json={**REGISTRATION, **bad})
            assert r.status_code == 400, bad
            assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
        r = await api.post("/oauth/register", content=b"nope")
        assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"
        reg = await _register(api, redirect_uris=["http://localhost:3000/cb"], client_name=" X  Y ")
        assert reg["client_name"] == "X Y"
    finally:
        _clear()


async def test_registration_is_capped_and_pruned(api):  # noqa: F811
    _clear()
    try:
        with SessionLocal() as db:
            for i in range(oauth.MAX_CLIENTS):
                oauth.register_client(db, {**REGISTRATION, "client_name": f"app {i}"})
        r = await api.post("/oauth/register", json=REGISTRATION)
        assert r.status_code == 400 and "al 50 apps" in r.json()["error_description"]
        with SessionLocal() as db:  # old registrations without a grant are forgotten
            for client in db.query(OAuthClient).all():
                client.created_at = oauth.now() - timedelta(days=2)
            db.commit()
        reg = await _register(api)
        with SessionLocal() as db:
            assert db.query(OAuthClient).count() == 1
            assert db.get(OAuthClient, reg["client_id"]) is not None
    finally:
        _clear()


# -- the flow (login on) -----------------------------------------------------------------------


@pytest.mark.parametrize("web", [{"API_TOKEN": ""}], indirect=True)
async def test_mcp_points_an_unauthenticated_client_at_the_metadata(web):  # noqa: F811
    r = await rpc(web, "ping")
    assert r.status_code == 401
    expected = 'Bearer resource_metadata="http://test/.well-known/oauth-protected-resource/mcp"'
    assert r.headers["www-authenticate"] == expected
    r = await rpc(web, "ping", authorization="Bearer wrong")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith('Bearer error="invalid_token", resource_')


@pytest.mark.parametrize("web", [{"API_TOKEN": ""}], indirect=True)
async def test_authorize_needs_the_login(web):  # noqa: F811
    _clear()
    try:
        reg = await _register(web)
        _, challenge = _pkce()
        r = await web.get(_authorize_url(reg["client_id"], challenge))
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login?next=/oauth/authorize?response_type=code")
        r = await web.post("/oauth/authorize", data={"decision": "allow"})
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    finally:
        _clear()


@pytest.mark.parametrize("web", [{"API_TOKEN": ""}], indirect=True)
async def test_full_flow_tokens_refresh_and_revocation(web):  # noqa: F811
    _clear()
    await _setup(web)
    try:
        client_id = (await _register(web))["client_id"]
        tokens = await _grant(web, client_id)
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
        access, refresh = tokens["access_token"], tokens["refresh_token"]
        with SessionLocal() as db:  # only hashes are stored
            hashes = {t.token_hash for t in db.query(OAuthToken).all()}
            assert access not in hashes and refresh not in hashes and len(hashes) == 2

        # The access token opens /mcp and the API, never a page; a refresh token opens nothing.
        r = await rpc(web, "ping", authorization=f"Bearer {access}")
        assert r.status_code == 200 and r.json()["result"] == {}
        headers = {"authorization": f"Bearer {access}"}
        assert (await web.get("/api/status", headers=headers)).status_code == 200
        assert await _ping(web, refresh) == 401

        # The account page lists the app.
        page = await web.get("/account")
        assert "Gekoppeld via OAuth" in page.text and "Claude ontkoppelen" in page.text

        # Refresh rotates: the old refresh token and the old access token die.
        r = await _refresh(web, client_id, refresh)
        assert r.status_code == 200, r.text
        access2, refresh2 = r.json()["access_token"], r.json()["refresh_token"]
        assert access2 != access and refresh2 != refresh
        assert await _ping(web, access) == 401 and await _ping(web, access2) == 200
        r = await _refresh(web, client_id, refresh)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

        # An expired access token is refused.
        with SessionLocal() as db:
            for t in db.query(OAuthToken).filter(OAuthToken.kind == "access").all():
                t.expires_at = oauth.now() - timedelta(seconds=1)
            db.commit()
        assert await _ping(web, access2) == 401

        # Revocation (RFC 7009) always answers 200 and clears the whole grant.
        assert (await web.post("/oauth/revoke", data={"token": "unknown"})).status_code == 200
        assert (await web.post("/oauth/revoke", data={"token": refresh2})).status_code == 200
        assert (await _refresh(web, client_id, refresh2)).status_code == 400

        # Link again, then revoke the app on the account page (password needed).
        tokens = await _grant(web, client_id)
        assert await _ping(web, tokens["access_token"]) == 200
        r = await web.post("/account", data={"action": "oauth_revoke", "client_id": client_id})
        assert r.status_code == 401
        data = {"action": "oauth_revoke", "client_id": client_id, "password": PASSWORD}
        r = await web.post("/account", data=data)
        assert r.status_code == 200 and "Claude is ontkoppeld" in r.text
        assert await _ping(web, tokens["access_token"]) == 401
        with SessionLocal() as db:
            assert db.query(OAuthToken).count() == 0
            assert db.get(OAuthClient, client_id) is None
    finally:
        _clear()


@pytest.mark.parametrize("web", [{"API_TOKEN": ""}], indirect=True)
async def test_consent_refusals(web):  # noqa: F811
    _clear()
    await _setup(web)
    try:
        client_id = (await _register(web))["client_id"]
        verifier, challenge = _pkce()

        # Unknown client or a foreign redirect URI: an error page, never a redirect.
        r = await web.get(_authorize_url("nope", challenge))
        assert r.status_code == 400 and "Onbekende app" in r.text
        assert "form-action 'self';" in r.headers["content-security-policy"]  # the default
        r = await web.get(_authorize_url(client_id, challenge, redirect_uri="https://evil/cb"))
        assert r.status_code == 400 and "hoort niet bij deze app" in r.text

        # Other problems go back to the client with an error.
        r = await web.get(_authorize_url(client_id, challenge, response_type="token"))
        query = _redirect_query(r)
        assert query["error"] == "unsupported_response_type" and query["state"] == "xyz"
        r = await web.get(_authorize_url(client_id, challenge, code_challenge_method="plain"))
        assert _redirect_query(r)["error"] == "invalid_request"
        r = await web.get(_authorize_url(client_id, challenge, resource="http://other/mcp"))
        assert _redirect_query(r)["error"] == "invalid_target"

        # Deny: no password needed, the client hears access_denied.
        data = {**_params(client_id, challenge), "decision": "deny"}
        assert _redirect_query(await web.post("/oauth/authorize", data=data)) == {
            "error": "access_denied",
            "state": "xyz",
        }
        # Allow needs the password; a wrong one shows the form again (and counts).
        data = {**_params(client_id, challenge), "decision": "allow", "password": "fout"}
        r = await web.post("/oauth/authorize", data=data)
        assert r.status_code == 401 and "klopt niet" in r.text and "Toegang toestaan" in r.text
        del data["password"]
        assert (await web.post("/oauth/authorize", data=data)).status_code == 401

        # The token endpoint: a wrong verifier burns the code, the right one afterwards fails.
        code = (await _allow(web, client_id, challenge))["code"]
        r = await _exchange(web, client_id, code, "x" * 50)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        r = await _exchange(web, client_id, code, verifier)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

        # A fresh code with the wrong client, redirect_uri or grant type.
        code = (await _allow(web, client_id, challenge))["code"]
        r = await _exchange(web, "nope", code, verifier)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client"
        r = await _exchange(web, client_id, code, verifier, redirect_uri=CALLBACK + "x")
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        r = await web.post("/oauth/token", data={"grant_type": "password"})
        assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
        r = await web.post("/oauth/token", content=b"\xff", headers={"content-type": "x/y"})
        assert r.status_code == 400

        # An expired code is refused.
        code = (await _allow(web, client_id, challenge))["code"]
        with SessionLocal() as db:
            for c in db.query(OAuthCode).all():
                c.expires_at = oauth.now() - timedelta(seconds=1)
            db.commit()
        r = await _exchange(web, client_id, code, verifier)
        assert r.status_code == 400 and "verlopen" in r.json()["error_description"]
    finally:
        _clear()


async def test_authorize_without_the_login_explains(api):  # noqa: F811
    r = await api.get("/oauth/authorize?client_id=x", headers={"accept": "text/html"})
    assert r.status_code == 400 and "AUTH_ENABLED=false" in r.text


def test_pkce_and_redirect_checks():
    init_db()
    verifier, challenge = _pkce()
    assert oauth._pkce_ok(verifier, challenge)
    assert not oauth._pkce_ok(verifier + "x", challenge)
    assert not oauth._pkce_ok("short", challenge)
    assert oauth.valid_redirect_uri("https://claude.ai/api/mcp/auth_callback")
    assert oauth.valid_redirect_uri("http://127.0.0.1:8080/callback")
    assert not oauth.valid_redirect_uri("http://claude.ai/cb")
    assert not oauth.valid_redirect_uri("https://")
    assert not oauth.valid_redirect_uri("javascript:alert(1)")
