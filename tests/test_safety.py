import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
import jsonschema
import pytest
from pydantic import SecretStr

from src.database import Database
from src.models import PlaceOrderInput
from src.validation import normalize_order


async def test_body_conforms_to_checked_official_v2_schema(rig):
    schema = json.loads((Path(__file__).parent / "fixtures" / "create_order_v2.json").read_text())
    for side in ("yes", "no"):
        for action in ("buy", "sell"):
            trade = PlaceOrderInput.model_validate(rig.trade(side=side, action=action))
            jsonschema.validate(rig.orders.body(trade), schema)


async def test_expired_approval(rig):
    args = rig.trade()
    with rig.db.connection() as c:
        c.execute("UPDATE approvals SET expires=0")
    assert (await rig.place(args))["reason"] == "missing_expired_or_mismatched_owner_approval"


async def test_stale_preflight(rig, monkeypatch):
    values = iter([0.0, 6.0])
    monkeypatch.setattr("src.orders.monotonic", lambda: next(values))
    # Use a separate monotonic replacement only inside the module, rather than asyncio's clock.
    result = await rig.place()
    assert result["reason"] == "account_snapshot_stale"
    assert not rig.remote.posts


async def test_unknown_create_response_is_never_retried(rig):
    rig.live()
    original = rig.remote.handle

    async def malformed_response(request):
        response = await original(request)
        return httpx.Response(201, json={"not_an_order": True}) if request.method == "POST" else response

    await rig.client.close()
    rig.client.http = httpx.AsyncClient(base_url=rig.settings.api_origin, transport=httpx.MockTransport(malformed_response))
    args = rig.trade()
    result = await rig.place(args)
    assert result["status"] == "unknown"
    assert (await rig.place(args))["status"] == "resting"
    assert len(rig.remote.posts) == 1


async def test_new_request_duplicate_detected_during_projection_lag(rig):
    rig.live()
    first = await rig.place()
    assert first["status"] == "resting"
    rig.remote.orders.clear()  # simulate a temporarily stale exchange list
    assert (await rig.place())["reason"] == "duplicate_existing_local_order"
    assert len(rig.remote.posts) == 1


async def test_order_status_read_and_database_update(rig):
    rig.live()
    args = rig.trade()
    result = await rig.place(args)
    oid = result["order_id"]
    rig.remote.orders[oid].update(status="executed", fill_count_fp="10.00", remaining_count_fp="0.00")
    result = await rig.tools.call("kalshi_get_order", {"order_id": oid}, ["read"])
    assert result["status"] == "filled"
    assert rig.db.get(args["request_id"])["status"] == "filled"


async def test_cancel_timeout_is_unknown_and_no_automatic_reissue(rig):
    rig.settings.dry_run = False
    order = rig.remote.order()
    oid = order["order_id"]
    rig.remote.orders[oid] = order
    rig.remote.fail[("DELETE", "/portfolio/events/orders/" + oid)] = httpx.ReadTimeout("ambiguous cancel")
    args = {"request_id": str(uuid4()), "order_id": oid}
    result = await rig.tools.call("kalshi_cancel_order", args, ["read", "trade"])
    assert result["status"] == "unknown"
    result = await rig.tools.call("kalshi_cancel_order", args, ["read", "trade"])
    assert result["status"] == "unknown" and result["observed_status"] == "resting"
    assert len([key for key, _ in rig.remote.calls if key[0] == "DELETE"]) == 1


async def test_cancel_filled_race_preserves_executed_quantity(rig):
    rig.settings.dry_run = False
    order = rig.remote.order(status="executed", fill_count_fp="10.00", remaining_count_fp="0.00")
    rig.remote.orders[order["order_id"]] = order
    result = await rig.tools.call("kalshi_cancel_order", {"request_id": str(uuid4()), "order_id": order["order_id"]}, ["read", "trade"])
    assert result["status"] == "filled" and result["filled_contracts"] == "10.00"
    assert not rig.remote.deletes


async def test_reservation_not_released_for_balance_before_terminal_fill(rig):
    rig.live()
    rig.remote.fill_count, rig.remote.place_status = "10.00", "executed"
    args = rig.trade()
    await rig.place(args)
    with rig.db.connection() as c:
        c.execute("UPDATE requests SET submitted_at=?, terminal_observed_at=? WHERE request_id=?",
                  (time.time() - 3600, time.time(), args["request_id"]))
    assert await rig.orders.outstanding_reserve({"updated_ts": int(time.time()) - 5}) == 1470


async def test_reservation_released_after_confirmed_newer_balance(rig):
    rig.live()
    rig.remote.fill_count, rig.remote.place_status = "10.00", "executed"
    args = rig.trade()
    await rig.place(args)
    assert await rig.orders.outstanding_reserve({"updated_ts": int(time.time()) + 3}) == 0


def test_environment_cannot_change_under_existing_state(rig):
    with pytest.raises(ValueError, match="separate database"):
        Database(rig.settings.database_path, "demo")


def test_expired_order_state():
    assert normalize_order({"status": "expired", "fill_count_fp": "2.00", "remaining_count_fp": "0.00"})["status"] == "expired"


def test_zero_fill_executed_does_not_invent_fills():
    assert normalize_order({"status": "executed", "fill_count_fp": "0.00", "remaining_count_fp": "0.00"})["status"] == "unknown"


def test_invalid_key_startup_error_does_not_include_key(settings):
    from src.kalshi_client import KalshiClient
    bad_key = "not a key; highly-sensitive-value"
    settings.kalshi_private_key = SecretStr(bad_key)
    with pytest.raises(ValueError) as exc:
        KalshiClient(settings)
    assert bad_key not in str(exc.value)


def test_configuration_error_redacts_raw_values(monkeypatch):
    from src.config import load_settings
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "SENSITIVE-UNIQUE-PEM")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "SENSITIVE-UNIQUE-ID")
    monkeypatch.setenv("ADMIN_TOKEN", "bad")
    with pytest.raises(ValueError) as exc:
        load_settings()
    assert "SENSITIVE" not in str(exc.value)


async def test_sell_blocked_by_local_projection_commitment(rig):
    rig.live()
    rig.remote.positions = [{"ticker": "TEST-MARKET", "position_fp": "10.00"}]
    await rig.place(rig.trade(side="no", limit_price_cents=40))
    rig.remote.orders.clear()
    result = await rig.place(rig.trade(action="sell", limit_price_cents=48))
    assert result["reason"] == "unreconciled_local_commitment_blocks_sell"
    assert len(rig.remote.posts) == 1


async def test_unknown_record_blocks_even_if_gate_row_missing(rig):
    rig.live()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = httpx.ReadTimeout("unknown")
    await rig.place()
    with rig.db.connection() as c:
        c.execute("DELETE FROM placement_gate")
    assert (await rig.place())["reason"] == "unresolved_execution_blocks_placement"


async def test_acknowledgment_without_final_read_never_assumes_fill(rig):
    rig.live()
    original = rig.remote.handle

    async def delayed_read(request):
        if request.method == "GET" and "/portfolio/orders/" in request.url.path:
            raise httpx.ReadTimeout("follow-up read timed out")
        return await original(request)

    await rig.client.close()
    rig.client.http = httpx.AsyncClient(base_url=rig.settings.api_origin, transport=httpx.MockTransport(delayed_read))
    result = await rig.place()
    assert result["status"] == "submitted" and result["order_id"]
    assert result["reconciliation_required"] is True
    assert len(rig.remote.posts) == 1
