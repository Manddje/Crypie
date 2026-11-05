"""The MCP server at /mcp (app/mcp_server.py) and the API token (app/auth/api_token.py)."""

import json
import re

import pytest

from app import settings_store as cfg
from app.auth.api_token import (
    TOKEN_HASH_KEY,
    bearer_token,
    issue_token,
    revoke_token,
    token_matches,
    token_status,
    token_valid,
)
from app.db import SessionLocal, Setting, init_db
from app.main import app
from app.mcp_server import DEFAULT_PROTOCOL, TOOLS
from tests.test_api import api  # noqa: F401  (fixture: login off)
from tests.test_auth import PASSWORD, _setup
from tests.test_security_auth import web  # noqa: F401  (fixture: login on)

TOKEN = "mcp-test-token-" + "x" * 40
ACCEPT = {"accept": "application/json, text/event-stream"}
BEARER = {"authorization": f"Bearer {TOKEN}"}


def rpc(client, method, params=None, msg_id=1, **headers):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={**ACCEPT, **headers})


async def call(client, name, arguments=None, **headers):
    r = await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **headers)
    assert r.status_code == 200, r.text
    return r.json()


def _text(answer: dict) -> str:
    return answer["result"]["content"][0]["text"]


# -- units -----------------------------------------------------------------------------------


def test_token_helpers():
    assert token_matches(TOKEN, TOKEN)
    assert not token_matches(TOKEN + "x", TOKEN) and not token_matches("", TOKEN)
    assert not token_matches(TOKEN, "") and not token_matches(None, TOKEN)

    class R:
        def __init__(self, value):
            self.headers = {"authorization": value} if value is not None else {}

    assert bearer_token(R(f"Bearer {TOKEN}")) == TOKEN
    assert bearer_token(R(f"bearer  {TOKEN} ")) == TOKEN
    assert bearer_token(R("Basic abc")) is None and bearer_token(R(None)) is None


def test_every_tool_is_consistent():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names)) == 34
    for tool in TOOLS:
        assert tool.read_only == (tool.method == "GET"), tool.name
        assert tool.description and tool.title
        listing = tool.listing()
        assert listing["inputSchema"]["type"] == "object"
        assert listing["annotations"]["readOnlyHint"] is tool.read_only
        if tool.model is None:
            assert listing["inputSchema"]["properties"] == {}


# -- the protocol (login off) ------------------------------------------------------------------


async def test_initialize_negotiates_the_protocol_version(api):  # noqa: F811
    r = await rpc(api, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "crypie"
    assert "current_password" in result["instructions"]

    r = await rpc(api, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == DEFAULT_PROTOCOL
    r = await rpc(api, "initialize")
    assert r.json()["result"]["protocolVersion"] == DEFAULT_PROTOCOL


async def test_notifications_and_responses_get_no_answer(api):  # noqa: F811
    r = await api.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    r = await api.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "result": {}})
    assert r.status_code == 202


async def test_tools_list(api):  # noqa: F811
    r = await rpc(api, "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {t.name for t in TOOLS}
    journal = tools["journal"]["inputSchema"]["properties"]
    assert journal["status"]["anyOf"][0]["enum"] == ["open", "closed"]
    assert tools["set_strategy"]["inputSchema"]["$defs"]  # the strategy schema travels along
    assert tools["place_order"]["annotations"]["destructiveHint"] is True
    assert tools["status"]["annotations"]["readOnlyHint"] is True


async def test_protocol_errors(api):  # noqa: F811
    r = await api.post("/mcp", content=b"{not json", headers=ACCEPT)
    assert r.status_code == 200 and r.json()["error"]["code"] == -32700
    assert r.json()["id"] is None
    r = await api.post("/mcp", json=[], headers=ACCEPT)
    assert r.json()["error"]["code"] == -32600
    r = await api.post("/mcp", json={"id": 1, "method": "ping"}, headers=ACCEPT)
    assert r.json()["error"]["code"] == -32600  # no "jsonrpc": "2.0"
    r = await rpc(api, "resources/list")
    assert r.json()["error"]["code"] == -32601
    r = await rpc(api, "tools/call", params=[1, 2])
    assert r.json()["error"]["code"] == -32602
    r = await rpc(api, "ping", **{"mcp-protocol-version": "2024-11-05"})
    assert r.status_code == 400
    r = await rpc(api, "ping", **{"mcp-protocol-version": "2025-06-18"})
    assert r.status_code == 200 and r.json()["result"] == {}
    for method in (api.get, api.delete):
        r = await method("/mcp")
        assert r.status_code == 405 and r.headers["allow"] == "POST"
        assert r.headers["cache-control"] == "no-store"


async def test_batch(api):  # noqa: F811
    body = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    r = await api.post("/mcp", json=body, headers=ACCEPT)
    assert [a["id"] for a in r.json()] == [1, 2]


async def test_tools_call_validates_arguments(api):  # noqa: F811
    r = await rpc(api, "tools/call", {"name": "candles", "arguments": {"market": "x", "limit": 0}})
    error = r.json()["error"]
    assert error["code"] == -32602 and "candles" in error["message"]
    assert {e["field"] for e in error["data"]} == {"market", "limit"}
    r = await rpc(api, "tools/call", {"name": "nope"})
    assert r.json()["error"]["code"] == -32602
    r = await rpc(api, "tools/call", {"name": "status", "arguments": {"x": 1}})
    assert "neemt geen argumenten" in r.json()["error"]["message"]
    r = await rpc(api, "tools/call", {"name": "positions", "arguments": {"mode": "paper", "x": 1}})
    assert r.json()["error"]["data"][0]["field"] == "x"  # extra fields are refused
    r = await rpc(api, "tools/call", {"name": "pause", "arguments": "yes"})
    assert r.json()["error"]["code"] == -32602


# -- the tools call the API (login off) --------------------------------------------------------


async def test_read_tools(api):  # noqa: F811
    answer = await call(api, "status")
    assert "isError" not in answer["result"]
    assert answer["result"]["structuredContent"]["mode"] == "paper"
    assert json.loads(_text(answer))["health"]["label"]

    answer = await call(api, "strategy")
    assert answer["result"]["structuredContent"]["name"]

    answer = await call(api, "positions", {"mode": "paper"})
    assert answer["result"]["structuredContent"] == {"mode": "paper", "open": [], "closed": []}

    answer = await call(api, "decisions", {"limit": 5})
    assert json.loads(_text(answer)) == [] and "structuredContent" not in answer["result"]

    answer = await call(api, "journal", {"status": "open", "days": 7})
    assert answer["result"]["structuredContent"]["entries"] == []

    for name in ("trading_status", "orders", "evaluations", "plan", "claude_usage"):
        answer = await call(api, name)
        assert "isError" not in answer["result"], name


async def test_settings_come_back_compact(api):  # noqa: F811
    answer = await call(api, "settings")
    data = answer["result"]["structuredContent"]
    assert data["mode"] == "paper" and "values" not in data
    assert len(_text(answer)) < 30_000  # the form payload of /api/settings is over 100 KB
    group = data["groups"][0]
    assert group["title"] == "Handelen" and group["needs_password"] and "sections" not in group
    field = next(f for f in group["fields"] if f["key"] == "trading_mode")
    assert field["value"] == "paper" and field["sensitive"] is True
    assert field["choices"] == [["paper", "Oefenmodus"], ["live", "Live"]]
    assert "placeholder" not in field and "default_form" not in field


async def test_acting_tools_go_through_the_api(api):  # noqa: F811
    try:
        answer = await call(api, "pause", {"paused": True})
        assert answer["result"]["structuredContent"] == {"paused": True}
        answer = await call(api, "trading_status")
        assert answer["result"]["structuredContent"]["paused"] is True
        await call(api, "pause", {"paused": False})

        answer = await call(api, "update_settings", {"changes": {"trade_interval_min": 30}})
        assert "isError" not in answer["result"]
        assert app.state.settings.trade_interval_min == 30
        field = next(
            f
            for g in answer["result"]["structuredContent"]["groups"]
            for f in g["fields"]
            if f["key"] == "trade_interval_min"
        )
        assert field["value"] == 30 and field["overridden"] is True

        answer = await call(api, "reset_settings", {"key": "trade_interval_min"})
        assert "isError" not in answer["result"]
        assert app.state.settings.trade_interval_min == 60

        answer = await call(api, "set_plan", {"goal": "Rustig groeien", "rules": "Geen FOMO"})
        assert answer["result"]["structuredContent"]["goal"] == "Rustig groeien"
        answer = await call(api, "plan")
        assert answer["result"]["structuredContent"]["rules"] == "Geen FOMO"
    finally:
        with SessionLocal() as db:
            cfg.reset_overrides(db)


async def test_api_refusals_are_tool_errors(api):  # noqa: F811
    answer = await call(api, "journal_note", {"position_id": 999, "note": "x"})
    assert answer["result"]["isError"] is True
    assert _text(answer).startswith("Crypie weigerde dit (404 [position_not_found])")

    strategy = (await call(api, "strategy"))["result"]["structuredContent"]
    strategy["market"] = "XRP-EUR"  # not in MARKETS
    answer = await call(api, "set_strategy", {"strategy": strategy})
    assert answer["result"]["isError"] is True and "(422 [market_not_tracked])" in _text(answer)

    answer = await call(api, "reset_settings", {"key": "no_such_setting"})
    assert answer["result"]["isError"] is True and "(404 [unknown_setting])" in _text(answer)


def _fake_candles(n=60, start=30000.0):
    rows = []
    price = start
    for i in range(n):
        price *= 1.01 if i % 7 else 0.97
        rows.append(
            [1_700_000_000_000 + i * 3_600_000, price, price * 1.01, price * 0.99, price, 5.0]
        )
    return rows


async def test_backtest_drops_the_curve(api, monkeypatch):  # noqa: F811
    async def candles(market, interval="1h", limit=200):
        return _fake_candles(limit)

    monkeypatch.setattr(app.state.client, "candles", candles)
    answer = await call(api, "backtest", {"limit": 50})
    assert "isError" not in answer["result"], _text(answer)
    data = answer["result"]["structuredContent"]
    assert "curve" not in data and "trade_log" in data and data["market"]
    answer = await call(api, "candles", {"market": "btc-eur", "limit": 3})
    assert len(json.loads(_text(answer))) == 3


async def test_a_crash_in_a_tool_does_not_leak(api, monkeypatch):  # noqa: F811
    async def candles(market, interval="1h", limit=200):
        raise RuntimeError("boom-secret")

    monkeypatch.setattr(app.state.client, "candles", candles)
    answer = await call(api, "candles", {"market": "BTC-EUR"})
    assert answer["result"]["isError"] is True and "boom-secret" not in _text(answer)


# -- the API token (login on) ------------------------------------------------------------------


@pytest.mark.parametrize("web", [{"API_TOKEN": TOKEN}], indirect=True)
async def test_mcp_needs_the_api_token(web):  # noqa: F811
    r = await rpc(web, "ping")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert "API-token" in r.json()["detail"]
    r = await rpc(web, "ping", authorization=f"Bearer {TOKEN}x")
    assert r.status_code == 401 and r.json()["detail"] == "API-token klopt niet"
    r = await rpc(web, "ping", authorization="Basic abc")
    assert r.status_code == 401
    r = await rpc(web, "initialize", {"protocolVersion": "2025-06-18"}, **BEARER)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "crypie"

    # The token opens the JSON API too, but never a page.
    r = await web.get("/api/status")
    assert r.status_code == 401
    r = await web.get("/api/status", headers=BEARER)
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    r = await web.get("/api/status", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = await web.get("/settings", headers=BEARER)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")

    # The tools reach the API with the same token.
    answer = await call(web, "status", **BEARER)
    assert answer["result"]["structuredContent"]["mode"] == "paper"


@pytest.mark.parametrize("web", [{"API_TOKEN": TOKEN}], indirect=True)
async def test_mcp_ignores_the_browser_session(web):  # noqa: F811
    await _setup(web)
    assert (await web.get("/settings")).status_code == 200  # logged in
    r = await rpc(web, "ping")
    assert r.status_code == 401  # the cookie does not count on /mcp


@pytest.mark.parametrize("web", [{"API_TOKEN": "short"}], indirect=True)
async def test_a_short_token_is_ignored(web):  # noqa: F811
    assert app.state.env.api_token == ""
    r = await rpc(web, "ping", authorization="Bearer short")
    assert r.status_code == 401


@pytest.mark.parametrize("web", [{"API_TOKEN": TOKEN}], indirect=True)
async def test_step_up_through_the_mcp(web):  # noqa: F811
    await _setup(web)
    try:
        answer = await call(web, "update_settings", {"changes": {"max_trade_eur": 40}}, **BEARER)
        assert answer["result"]["isError"] is True
        assert "(403 [password_required])" in _text(answer) and "wachtwoord" in _text(answer)
        assert app.state.settings.max_trade_eur == 100.0

        answer = await call(
            web,
            "update_settings",
            {"changes": {"max_trade_eur": 40}, "current_password": "fout"},
            **BEARER,
        )
        assert answer["result"]["isError"] is True and "klopt niet" in _text(answer)

        answer = await call(
            web,
            "update_settings",
            {"changes": {"max_trade_eur": 40}, "current_password": PASSWORD},
            **BEARER,
        )
        assert "isError" not in answer["result"], _text(answer)
        assert app.state.settings.max_trade_eur == 40.0

        answer = await call(web, "reset_settings", {"key": "max_trade_eur"}, **BEARER)
        assert answer["result"]["isError"] is True and "(403 [password_required])" in _text(answer)
        answer = await call(
            web, "reset_settings", {"key": "max_trade_eur", "current_password": PASSWORD}, **BEARER
        )
        assert "isError" not in answer["result"], _text(answer)
        assert app.state.settings.max_trade_eur == 100.0
    finally:
        with SessionLocal() as db:
            cfg.reset_overrides(db)


# -- a token from the portal ---------------------------------------------------------------------

TOKEN_RE = re.compile(r'<code id="api-token"[^>]*>([A-Za-z0-9_-]+)</code>')


def test_portal_token_storage():
    init_db()
    env = "e" * 40
    with SessionLocal() as db:
        revoke_token(db)
        assert token_status(db, "").source is None
        assert token_status(db, env).source == "env"
        assert token_valid(db, env, env) and not token_valid(db, "", env)

        token = issue_token(db)
        assert len(token) >= 32
        assert db.get(Setting, TOKEN_HASH_KEY).value != token  # only the hash is stored
        assert token_status(db, env).source == "portal" and token_status(db, env).since
        assert token_valid(db, token, env)
        assert not token_valid(db, token + "x", env)
        assert not token_valid(db, env, env)  # the environment does not count meanwhile

        second = issue_token(db)
        assert second != token and token_valid(db, second, env) and not token_valid(db, token, env)
        assert revoke_token(db) is True and revoke_token(db) is False
        assert token_valid(db, env, env) and not token_valid(db, second, env)


async def _issue(client, password=PASSWORD):
    return await client.post("/account", data={"action": "api_token", "password": password})


@pytest.mark.parametrize("web", [{"API_TOKEN": TOKEN}], indirect=True)
async def test_portal_token_replaces_the_environment_token(web):  # noqa: F811
    await _setup(web)
    try:
        page = await web.get("/account")
        assert "API-token voor Claude" in page.text and "uit de omgeving" in page.text
        assert "Token intrekken" not in page.text  # nothing to revoke yet
        # The guide: both clients, with the placeholder until a token is made.
        assert "Zo gebruik je het" in page.text and "mcp-remote" in page.text
        assert 'crypie http://test/mcp --header "Authorization: Bearer &lt;token&gt;"' in page.text

        r = await _issue(web, "fout")
        assert r.status_code == 401 and "klopt niet" in r.text and 'id="api-token"' not in r.text
        r = await _issue(web)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        token = TOKEN_RE.search(r.text).group(1)
        assert len(token) >= 32
        assert (
            f'claude mcp add --transport http crypie http://test/mcp --header "Authorization: '
            f'Bearer {token}"'
        ) in r.text
        assert f'"CRYPIE_AUTH": "Bearer {token}"' in r.text  # the guide fills in the token too

        # The new token opens /mcp and the API; the environment token no longer does.
        assert (await rpc(web, "ping", authorization=f"Bearer {token}")).status_code == 200
        assert (
            await web.get("/api/status", headers={"authorization": f"Bearer {token}"})
        ).status_code == 200
        assert (await rpc(web, "ping", **BEARER)).status_code == 401
        page = await web.get("/account")
        assert "gemaakt in het portaal" in page.text and "Token intrekken" in page.text
        assert token not in page.text  # shown once

        # A second token replaces the first.
        second = TOKEN_RE.search((await _issue(web)).text).group(1)
        assert second != token
        assert (await rpc(web, "ping", authorization=f"Bearer {token}")).status_code == 401
        assert (await rpc(web, "ping", authorization=f"Bearer {second}")).status_code == 200

        # Revoking needs the password too; afterwards the environment token applies again.
        r = await web.post("/account", data={"action": "api_token_revoke", "password": "fout"})
        assert r.status_code == 401
        assert (await rpc(web, "ping", authorization=f"Bearer {second}")).status_code == 200
        r = await web.post("/account", data={"action": "api_token_revoke", "password": PASSWORD})
        assert (
            r.status_code == 200 and "ingetrokken" in r.text and "API_TOKEN) geldt weer" in r.text
        )
        assert (await rpc(web, "ping", authorization=f"Bearer {second}")).status_code == 401
        assert (await rpc(web, "ping", **BEARER)).status_code == 200
    finally:
        with SessionLocal() as db:
            revoke_token(db)


@pytest.mark.parametrize("web", [{"API_TOKEN": ""}], indirect=True)
async def test_without_any_token_the_page_says_so(web):  # noqa: F811
    await _setup(web)
    try:
        page = await web.get("/account")
        assert "genereer er hieronder een" in page.text
        assert (await rpc(web, "ping")).status_code == 401
        token = TOKEN_RE.search((await _issue(web)).text).group(1)
        assert (await rpc(web, "ping", authorization=f"Bearer {token}")).status_code == 200
        r = await web.post("/account", data={"action": "api_token_revoke", "password": PASSWORD})
        assert r.status_code == 200 and "nieuw token genereert." in r.text
        assert (await rpc(web, "ping", authorization=f"Bearer {token}")).status_code == 401
    finally:
        with SessionLocal() as db:
            revoke_token(db)
