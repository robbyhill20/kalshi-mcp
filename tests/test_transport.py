import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from starlette.testclient import TestClient

from src.kalshi_client import KalshiClient
from src.server import create_app

from .conftest import Exchange


def rpc(client, method, params=None, token="r" * 48, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})


@pytest.fixture
def web(settings):
    remote = Exchange()
    client = KalshiClient(settings, httpx.MockTransport(remote.handle))
    app = create_app(settings, client)
    with TestClient(app, base_url="http://localhost:8000") as browser:
        yield browser, app, remote


def test_public_health(web):
    client, _, remote = web
    assert client.get("/health").json()["dry_run"] is True
    assert not remote.calls


def test_unauthenticated_mcp_rejected(web):
    response = web[0].post("/mcp", json={})
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["WWW-Authenticate"]


def test_protocol_initialize(web):
    response = rpc(web[0], "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "integration-test", "version": "1"}})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "kalshi-grok-mcp"


def test_tool_discovery_role_separation(web):
    client = web[0]
    readonly = rpc(client, "tools/list").json()["result"]["tools"]
    director = rpc(client, "tools/list", token="d" * 48).json()["result"]["tools"]
    assert len(readonly) == 7
    assert len(director) == 9
    assert all(t["inputSchema"]["additionalProperties"] is False for t in director)
    response = rpc(client, "tools/call", {"name": "kalshi_place_order", "arguments": {}})
    assert response.json()["result"]["structuredContent"]["reason"] == "director_permission_required"


def test_market_call_over_mcp(web):
    response = rpc(web[0], "tools/call", {"name": "kalshi_get_market", "arguments": {"ticker": "TEST-MARKET"}})
    result = response.json()["result"]
    assert result["structuredContent"]["market"]["ticker"] == "TEST-MARKET"
    assert not result["isError"]


def test_malformed_mcp_request(web):
    response = web[0].post("/mcp", content="{not json", headers={"Authorization": "Bearer " + "r" * 48,
                          "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    assert response.status_code == 400
    response = rpc(web[0], "tools/call", {"name": "kalshi_get_balance", "arguments": {"secret": "PRIVATE KEY"}})
    assert response.json()["result"]["isError"] is True
    assert "PRIVATE KEY" not in response.text


def test_oversized_body(web):
    response = web[0].post("/mcp", content="x" * 65537,
                          headers={"Authorization": "Bearer " + "r" * 48, "Content-Type": "application/json"})
    assert response.status_code == 413


def test_untrusted_host_rejected(web):
    response = web[0].post("/mcp", json={}, headers={"Host": "evil.test", "Authorization": "Bearer " + "r" * 48,
                           "Accept": "application/json, text/event-stream"})
    assert response.status_code == 421


def test_owner_controls_require_owner_secret(web):
    client = web[0]
    assert client.post("/owner/halt", json={"halted": True}).status_code == 401
    assert client.post("/owner/halt", json={"halted": True}, headers={"Authorization": "Bearer " + "d" * 48}).status_code == 401
    response = client.post("/owner/halt", json={"halted": True}, headers={"Authorization": "Bearer " + "a" * 48})
    assert response.json()["halted"] is True
    assert client.get("/health").json()["halted"] is True


def test_owner_only_equal_shard_allocation(web):
    client, _, remote = web
    endpoint = "/owner/allocation"
    owner = {"Authorization": "Bearer " + "a" * 48}
    director = {"Authorization": "Bearer " + "d" * 48}
    assert client.get(endpoint).status_code == 401
    assert client.post(endpoint, json={"target": "equal_default_sports"}, headers=director).status_code == 401
    assert client.post(endpoint, json={"target": "equal_default_sports", "exchange_index": 1},
                       headers=owner).status_code == 400
    assert remote.allocation_posts == []
    assert client.get(endpoint, headers=owner).json()["equal_default_sports_active"] is False
    response = client.post(endpoint, json={"target": "equal_default_sports"}, headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["equal_default_sports_active"] is True
    assert remote.allocation_posts == [{"allocations": [{"exchange_index": 0, "percent": 50},
                                                      {"exchange_index": 3, "percent": 50}],
                                        "resting_margin_reservation": "sum"}]
    assert client.get(endpoint, headers=owner).json()["equal_default_sports_active"] is True


def test_ambiguous_allocation_write_is_read_back_without_retry(web):
    client, _, remote = web
    remote.fail[("POST", "/portfolio/target_balance_allocation")] = httpx.ReadTimeout("ambiguous")
    response = client.post("/owner/allocation", json={"target": "equal_default_sports"},
                           headers={"Authorization": "Bearer " + "a" * 48})
    assert response.status_code == 502
    assert response.json()["error"] == "allocation_update_unconfirmed"
    assert len([key for key, _ in remote.calls if key ==
                ("POST", "/portfolio/target_balance_allocation")]) == 1


def test_allocation_scope_failure_is_reported_without_retry(web):
    client, _, remote = web
    remote.fail[("POST", "/portfolio/target_balance_allocation")] = 403
    response = client.post("/owner/allocation", json={"target": "equal_default_sports"},
                           headers={"Authorization": "Bearer " + "a" * 48})
    assert response.status_code == 502
    assert response.json()["error"] == "allocation_update_rejected"
    assert response.json()["reason"] == "permission_denied"
    assert len([key for key, _ in remote.calls if key ==
                ("POST", "/portfolio/target_balance_allocation")]) == 1


def test_owner_approves_exact_dry_run_over_http(web):
    client = web[0]
    terms = {"request_id": str(uuid4()), "ticker": "TEST-MARKET", "side": "yes", "action": "buy",
             "contracts": 10, "limit_price_cents": 47}
    approval = client.post("/owner/approvals", json=terms, headers={"Authorization": "Bearer " + "a" * 48}).json()
    result = rpc(client, "tools/call", {"name": "kalshi_place_order", "arguments": {
        **terms, "approval_id": approval["approval_id"]}}, token="d" * 48).json()["result"]
    assert result["structuredContent"]["status"] == "simulated"


@pytest.mark.parametrize("consent_origin", [None, "http://localhost:8000", "null"])
def test_oauth_pkce_flow_and_refresh(web, consent_origin):
    import base64
    client, app, _ = web
    registration = client.post("/register", json={"client_name": "Grok", "redirect_uris": ["https://grok.com/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "read"})
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": client_id, "redirect_uri": "https://grok.com/callback", "response_type": "code",
              "state": "test-state", "code_challenge": challenge, "code_challenge_method": "S256",
              "resource": "http://localhost:8000/mcp"}
    response = client.get("/authorize?" + urlencode(params), follow_redirects=False)
    assert response.status_code in {302, 303, 307}, response.text
    consent_url = response.headers["location"]
    nonce = parse_qs(urlsplit(consent_url).query)["nonce"][0]
    assert client.get(consent_url).status_code == 200
    response = client.post("/consent", data={"nonce": nonce, "login_token": "r" * 48}, follow_redirects=False,
                           headers={"Origin": consent_origin} if consent_origin else {})
    assert response.status_code == 200, response.text
    from html import unescape
    import re
    callback_url = unescape(re.search(r'href="([^"]+)"', response.text).group(1))
    callback = parse_qs(urlsplit(callback_url).query)
    assert callback["state"] == ["test-state"]
    token_params = {"grant_type": "authorization_code", "client_id": client_id, "code": callback["code"][0],
                    "redirect_uri": "https://grok.com/callback", "code_verifier": verifier,
                    "resource": "http://localhost:8000/mcp"}
    response = client.post("/token", data=token_params)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert rpc(client, "tools/list", token=tokens["access_token"]).status_code == 200
    assert len(rpc(client, "tools/list", token=tokens["access_token"]).json()["result"]["tools"]) == 7
    assert client.post("/token", data=token_params).status_code == 400
    refresh_params = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
    refreshed = client.post("/token", data=refresh_params)
    assert refreshed.status_code == 200, refreshed.text
    assert client.post("/token", data=refresh_params).status_code == 400
    assert rpc(client, "tools/list", token=tokens["access_token"]).status_code == 401
    new_access = refreshed.json()["access_token"]
    assert rpc(client, "tools/list", token=new_access).status_code == 200
    client.post("/owner/revoke-sessions", json={}, headers={"Authorization": "Bearer " + "a" * 48})
    assert rpc(client, "tools/list", token=new_access).status_code == 401
    # Database stores token hashes, never these access/refresh token values.
    with app.state.db.connection() as c:
        rows = json.dumps([dict(r) for r in c.execute("SELECT * FROM oauth")])
        assert new_access not in rows


def test_oauth_discovery(web):
    client = web[0]
    protected = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert protected["resource"] == "http://localhost:8000/mcp"
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]


def test_oauth_redirect_restricted(web):
    response = web[0].post("/register", json={"redirect_uris": ["https://evil.test/callback"]})
    assert response.status_code == 400


def test_documented_bot_callbacks_can_register_together(web):
    response = web[0].post("/register", json={"redirect_uris": [
        "https://www.cursor.com/agents/mcp/oauth/callback", "http://localhost:8787/callback"]})
    assert response.status_code == 201


@pytest.mark.parametrize("uri", ["https://www.cursor.com/other", "http://localhost:8788/callback",
    "http://localhost:8787/other", "http://127.0.0.1:8787/callback",
    "https://www.cursor.com.evil.test/agents/mcp/oauth/callback",
    "http://localhost:8787/callback?secret=do-not-log"])
def test_bot_callback_exception_is_exact(web, uri):
    assert web[0].post("/register", json={"redirect_uris": [uri]}).status_code == 400


def test_no_secrets_in_audit(web, settings):
    client, app, _ = web
    rpc(client, "tools/call", {"name": "kalshi_get_balance", "arguments": {}})
    with app.state.db.connection() as c:
        logs = "".join(row[0] for row in c.execute("SELECT record FROM audit"))
    for secret in [settings.kalshi_private_key, settings.admin_token, settings.mcp_read_token,
                   settings.mcp_director_token, settings.kalshi_api_key_id]:
        assert secret.get_secret_value() not in logs


def test_only_one_worker_per_database(web, settings):
    with pytest.raises(RuntimeError, match="one server worker"):
        create_app(settings)


@pytest.mark.parametrize("origin", ["https://evil.test", "https://grok.com"])
def test_consent_rejects_explicit_foreign_origin(web, origin):
    response = web[0].post("/consent", data={"nonce": "nonce", "login_token": "r" * 48},
                            headers={"Origin": origin})
    assert response.status_code == 403 and response.json()["error"] == "invalid_origin"


def test_opaque_origin_consent_requires_browser_nonce(web):
    response = web[0].post("/consent", data={"nonce": "nonce", "login_token": "r" * 48},
                            headers={"Origin": "null"})
    assert response.status_code == 403 and response.json()["error"] == "invalid_consent"


def test_opaque_origin_cannot_modify_owner_controls(web):
    response = web[0].post("/owner/halt", json={"halted": True},
                            headers={"Origin": "null", "Authorization": "Bearer " + "a" * 48})
    assert response.status_code == 401


async def test_official_mcp_client_round_trip(settings):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    remote = Exchange()
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(remote.handle)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    headers={"Authorization": "Bearer " + "r" * 48}) as http:
            async with streamable_http_client("http://localhost:8000/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "kalshi-grok-mcp"
                    assert len((await session.list_tools()).tools) == 7
                    result = await session.call_tool("kalshi_get_balance", {})
                    assert result.structuredContent["balance"] == 100000
