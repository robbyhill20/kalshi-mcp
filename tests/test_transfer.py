import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from starlette.testclient import TestClient

from src.errors import Rejection
from src.kalshi_client import KalshiClient
from src.models import TransferInput
from src.server import create_app
from src.transfers import move_exchange_cash

from .conftest import Exchange


OWNER = {"Authorization": "Bearer " + "a" * 48}
BOTS = ("r" * 48, "d" * 48, "x" * 48)


@pytest.fixture
def web(settings):
    remote = Exchange()
    client = KalshiClient(settings, httpx.MockTransport(remote.handle))
    app = create_app(settings, client)
    with TestClient(app, base_url="http://localhost:8000") as browser:
        yield browser, app, remote


def move(client, body, token="a" * 48):
    return client.post("/owner/transfer", json=body, headers={"Authorization": "Bearer " + token})


def sample(amount=150, **overrides):
    body = {"request_id": str(uuid4()), "from_index": 0, "to_index": 3, "amount_cents": amount}
    body.update(overrides)
    return body


def expected_body(body):
    return {"source": "event_contract", "destination": "event_contract",
            "amount": body["amount_cents"] * 100, "client_transfer_id": body["request_id"],
            "source_exchange_shard": body["from_index"], "destination_exchange_shard": body["to_index"],
            "source_subaccount": 0, "destination_subaccount": 0}


def test_bot_tokens_cannot_transfer_or_read_owner_pools(web):
    client, _, remote = web
    body = sample()
    assert client.post("/owner/transfer", json=body).status_code == 401
    for token in BOTS:
        assert move(client, body, token).status_code == 401
        assert client.get("/owner/exchange-balances",
                          headers={"Authorization": "Bearer " + token}).status_code == 401
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Authorization": "Bearer " + "d" * 48,
                                  "Accept": "application/json, text/event-stream",
                                  "MCP-Protocol-Version": "2025-11-25"}).json()["result"]["tools"]
    names = {tool["name"] for tool in listed}
    assert "kalshi_get_exchange_balances" in names
    assert not any("transfer" in name for name in names)
    rejected = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "kalshi_transfer", "arguments": body}},
                           headers={"Authorization": "Bearer " + "x" * 48,
                                    "Accept": "application/json, text/event-stream",
                                    "MCP-Protocol-Version": "2025-11-25"})
    assert rejected.json()["result"]["structuredContent"]["reason"] == "unknown_tool"
    assert remote.transfer_posts == []


def test_owner_can_read_pool_balances(web):
    client, _, remote = web
    remote.pool_balances = [
        {"subaccount_number": 0, "exchange_index": 3, "balance": "12.3400", "updated_ts": 1},
        {"subaccount_number": 2, "exchange_index": 0, "balance": "5.0000", "updated_ts": 1},
        {"subaccount_number": 0, "exchange_index": 0, "balance": "900.0000", "updated_ts": 1},
    ]
    assert client.get("/owner/exchange-balances").status_code == 401
    response = client.get("/owner/exchange-balances", headers=OWNER)
    assert response.status_code == 200, response.text
    assert response.json()["balances"] == [
        {"exchange_index": 0, "balance_cents": 90000, "balance_dollars": "900.0000"},
        {"exchange_index": 3, "balance_cents": 1234, "balance_dollars": "12.3400"},
    ]
    assert remote.transfer_posts == []


def test_dry_run_transfer_never_calls_kalshi_and_cannot_be_promoted(web, settings):
    client, _, remote = web
    body = sample()
    response = move(client, body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "simulated" and result["submitted"] is False
    assert result["would_submit"] == expected_body(body)
    assert remote.transfer_posts == []
    again = move(client, body)
    assert again.json()["replayed"] is True and again.json()["status"] == "simulated"
    assert remote.transfer_posts == []
    settings.dry_run = False
    conflict = move(client, body)
    assert conflict.status_code == 400 and conflict.json()["error"] == "request_id_conflict"
    assert remote.transfer_posts == []


def test_live_transfer_posts_once_and_replay_does_not(web, settings):
    client, app, remote = web
    settings.dry_run = False
    assert settings.trading_enabled is False
    client.post("/owner/halt", json={"halted": True}, headers=OWNER)
    body = sample()
    response = move(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert response.json()["transfer_id"] == "mock-transfer"
    assert remote.transfer_posts == [expected_body(body)]
    replay = move(client, body)
    assert replay.json()["replayed"] is True
    assert remote.transfer_posts == [expected_body(body)]
    changed = sample(amount=151, request_id=body["request_id"])
    conflict = move(client, changed)
    assert conflict.status_code == 400 and conflict.json()["error"] == "request_id_conflict"
    assert len(remote.transfer_posts) == 1
    with app.state.db.connection() as c:
        logs = "".join(row[0] for row in c.execute("SELECT record FROM audit"))
    assert "mock-transfer" in logs
    assert "test-key-id" not in logs
    for secret in (settings.admin_token, settings.mcp_read_token, settings.mcp_director_token,
                   settings.mcp_axe_token, settings.kalshi_private_key, settings.kalshi_api_key_id):
        assert secret.get_secret_value() not in logs


def test_unconfirmed_transfer_retries_the_same_kalshi_request(web, settings):
    client, _, remote = web
    settings.dry_run = False
    body = sample(amount=2500)
    remote.fail[("POST", "/portfolio/intra_exchange_instance_transfer")] = httpx.ReadTimeout("lost")
    first = move(client, body)
    assert first.status_code == 502 and first.json()["status"] == "unknown"
    assert first.json()["submitted"] is None
    assert remote.transfer_posts == [expected_body(body)]
    del remote.fail[("POST", "/portfolio/intra_exchange_instance_transfer")]
    second = move(client, body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "accepted" and second.json()["transfer_id"] == "mock-transfer"
    assert remote.transfer_posts == [expected_body(body), expected_body(body)]
    third = move(client, body)
    assert third.json()["replayed"] is True
    assert len(remote.transfer_posts) == 2


def test_duplicate_kalshi_request_id_does_not_create_another_transfer(web, settings):
    client, _, remote = web
    settings.dry_run = False
    body = sample()
    remote.fail[("POST", "/portfolio/intra_exchange_instance_transfer")] = 409
    response = move(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["already_accepted"] is True
    assert response.json()["created_another"] is False
    assert remote.transfer_posts == [expected_body(body)]
    replay = move(client, body)
    assert replay.json()["replayed"] is True
    assert len(remote.transfer_posts) == 1


def test_rejected_transfer_is_not_retried_with_a_new_post(web, settings):
    client, _, remote = web
    settings.dry_run = False
    body = sample()
    remote.fail[("POST", "/portfolio/intra_exchange_instance_transfer")] = 403
    response = move(client, body)
    assert response.status_code == 400
    assert response.json()["status"] == "rejected" and response.json()["reason"] == "permission_denied"
    assert "sensitive error body" not in response.text
    assert len(remote.transfer_posts) == 1
    assert move(client, body).json()["replayed"] is True
    assert len(remote.transfer_posts) == 1


@pytest.mark.parametrize("overrides", [
    {"from_index": 0, "to_index": 0},
    {"from_index": 4},
    {"to_index": -1},
    {"amount_cents": 0},
    {"amount_cents": -5},
    {"amount_cents": 1.5},
    {"amount_cents": 10_000_001},
    {"request_id": "not-a-uuid"},
    {"from_index": "0"},
    {"dry_run": False},
])
def test_invalid_transfer_is_rejected_before_kalshi(web, overrides):
    client, _, remote = web
    response = move(client, sample(**overrides))
    assert response.status_code == 400 and response.json()["error"] == "invalid_arguments"
    assert remote.transfer_posts == []


def test_amount_above_owner_cap_does_not_call_kalshi(web):
    client, app, remote = web
    body = sample(amount=1_000_001)
    response = move(client, body)
    assert response.status_code == 400, response.text
    assert response.json()["reason"] == "transfer_amount_limit"
    assert response.json()["max_transfer_cents"] == 1_000_000
    assert response.json()["submitted"] is False
    assert remote.transfer_posts == []
    with app.state.db.connection() as c:
        assert c.execute("SELECT count(*) FROM requests WHERE kind='transfer'").fetchone()[0] == 0
        logs = "".join(row[0] for row in c.execute("SELECT record FROM audit"))
    assert "test-key-id" not in logs and "1_000_001" not in logs
    assert "1000001" in logs


async def test_exchange_balances_are_read_only_for_every_desk(rig):
    rig.remote.pool_balances = [
        {"subaccount_number": 0, "exchange_index": 3, "balance": "12.3456", "updated_ts": 1},
        {"subaccount_number": 0, "exchange_index": 0, "balance": "900.0000", "updated_ts": 1},
        {"subaccount_number": 2, "exchange_index": 1, "balance": "5.0000", "updated_ts": 1},
    ]
    expected = [
        {"exchange_index": 0, "balance_cents": 90000, "balance_dollars": "900.0000"},
        {"exchange_index": 3, "balance_cents": 1234, "balance_dollars": "12.3456"},
    ]
    for scopes in (["read"], ["read", "trade"], ["read", "axe"]):
        assert (await rig.tools.call("kalshi_get_exchange_balances", {}, scopes))["balances"] == expected
    assert (await rig.tools.call("kalshi_transfer", sample(), ["read", "trade", "axe"]))["reason"] == "unknown_tool"
    assert rig.remote.transfer_posts == []
    assert not rig.remote.posts and not rig.remote.deletes


async def test_transfer_request_id_cannot_reuse_an_order(rig):
    args = rig.trade()
    await rig.place(args)
    transfer = TransferInput(request_id=args["request_id"], from_index=0, to_index=3, amount_cents=100)
    with pytest.raises(Rejection) as caught:
        await move_exchange_cash(rig.settings, rig.client, rig.db, transfer)
    assert caught.value.code == "request_id_conflict"
    assert rig.remote.transfer_posts == []


async def test_concurrent_transfer_posts_once(rig):
    rig.live()
    transfer = TransferInput(request_id=str(uuid4()), from_index=0, to_index=3, amount_cents=100)
    results = await asyncio.gather(*(move_exchange_cash(rig.settings, rig.client, rig.db, transfer) for _ in range(6)))
    assert len(rig.remote.transfer_posts) == 1
    assert rig.remote.transfer_posts[0]["client_transfer_id"] == str(transfer.request_id)
    assert any(row["status"] == "accepted" and row.get("transfer_id") == "mock-transfer" for row in results)


def test_owner_page_offers_the_transfer_form(web):
    client = web[0]
    page = client.get("/owner").text
    script = client.get("/assets/owner.js").text
    assert "Move cash" in page and "transfer-request-id" in page
    assert "exchange-balances" in script and 'call("transfer"' in script


def test_audit_record_is_json_without_request_body_secrets(web, settings):
    client, app, _ = web
    move(client, sample())
    with app.state.db.connection() as c:
        records = [json.loads(row[0]) for row in c.execute("SELECT record FROM audit")]
    simulated = [row for row in records if row["event"] == "owner_exchange_transfer_simulated"]
    assert simulated and simulated[0]["submitted"] is False
    assert "would_submit" not in simulated[0]
    assert settings.kalshi_api_key_id.get_secret_value() not in json.dumps(records)
