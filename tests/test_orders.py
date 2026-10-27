from uuid import uuid4

import httpx
import pytest


@pytest.mark.parametrize("field,value", [
    ("side", "YES"), ("side", "maybe"), ("limit_price_cents", 0), ("limit_price_cents", 100),
    ("limit_price_cents", 47.5), ("limit_price_cents", "47"), ("contracts", 0), ("contracts", -1),
    ("contracts", 1.5), ("contracts", True), ("allow_taker", "false"), ("action", "market"),
])
async def test_invalid_order_schema(rig, field, value):
    args = rig.trade()
    args[field] = value
    result = await rig.place(args)
    assert result["reason"] == "invalid_arguments"
    assert not rig.remote.posts and not rig.remote.calls


async def test_missing_request_id(rig):
    args = rig.trade()
    del args["request_id"]
    assert (await rig.place(args))["reason"] == "invalid_arguments"


async def test_missing_owner_approval(rig):
    args = rig.trade()
    args["approval_id"] = str(uuid4())
    assert (await rig.place(args))["reason"] == "missing_expired_or_mismatched_owner_approval"


async def test_changed_terms_rejected(rig):
    args = rig.trade()
    args["contracts"] += 1
    assert (await rig.place(args))["reason"] == "missing_expired_or_mismatched_owner_approval"


@pytest.mark.parametrize("change,reason", [
    ({"status": "closed"}, "market_not_open"),
    ({"status": "inactive"}, "market_not_open"),
    ({"close_time": "2020-01-01T00:00:00Z"}, "market_not_open"),
    ({"market_type": "scalar"}, "only_one_dollar_binary_markets_supported"),
    ({"price_ranges": []}, "missing_market_price_grid"),
    ({"price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.02"}]}, "price_not_on_market_grid"),
    ({"exchange_index": 3}, "exchange_trading_paused_or_unknown"),
])
async def test_market_validation(rig, change, reason):
    rig.remote.market.update(change)
    result = await rig.place()
    assert result["reason"] == reason
    assert not rig.remote.posts


async def test_invalid_market(rig):
    result = await rig.place(rig.trade(ticker="MISSING-MARKET"))
    assert result["status"] == "rejected" and result["reason"] == "not_found"


async def test_insufficient_balance_includes_fee_reserve(rig):
    rig.remote.balance = 470
    assert (await rig.place())["reason"] == "insufficient_available_balance"


async def test_more_than_100_contracts_allowed_within_dollar_limits(rig):
    result = await rig.place(rig.trade(contracts=101, limit_price_cents=1))
    assert result["status"] == "simulated"
    assert result["submitted"] is False


async def test_dollar_cap_still_blocks_large_quantity_without_schema_cap(rig):
    result = await rig.place(rig.trade(contracts=10001, limit_price_cents=1))
    assert result["reason"] == "owner_order_cost_limit"


async def test_duplicate_existing_order(rig):
    order = rig.remote.order()
    rig.remote.orders[order["order_id"]] = order
    assert (await rig.place())["reason"] == "duplicate_existing_order"


async def test_duplicate_on_later_page(rig):
    rig.remote.pages = {"": {"orders": [], "cursor": "next"},
                        "next": {"orders": [rig.remote.order()], "cursor": ""}}
    assert (await rig.place())["reason"] == "duplicate_existing_order"


async def test_partial_fill(rig):
    rig.live()
    rig.remote.fill_count = "3.25"
    result = await rig.place()
    assert result["status"] == "partially_filled"
    assert result["filled_contracts"] == "3.25"
    assert result["remaining_contracts"] == "6.75"
    assert len(rig.remote.posts) == 1


async def test_filled(rig):
    rig.live()
    rig.remote.fill_count, rig.remote.place_status = "10.00", "executed"
    assert (await rig.place())["status"] == "filled"


async def test_cancellation_while_disabled_and_halted(rig):
    rig.settings.dry_run = False
    rig.db.halt(True)
    order = rig.remote.order(fill_count_fp="2.00", remaining_count_fp="8.00")
    rig.remote.orders[order["order_id"]] = order
    args = {"request_id": str(uuid4()), "order_id": order["order_id"]}
    result = await rig.tools.call("kalshi_cancel_order", args, ["read", "trade"])
    assert result["status"] == "cancelled" and result["filled_contracts"] == "2.00"
    assert len(rig.remote.deletes) == 1
    await rig.tools.call("kalshi_cancel_order", args, ["read", "trade"])
    assert len(rig.remote.deletes) == 1
    delete_calls = [(key, params) for key, params in rig.remote.calls if key[0] == "DELETE"]
    assert delete_calls[0][1]["market_ticker"] == "TEST-MARKET"


async def test_post_timeout_never_retries(rig):
    rig.live()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = httpx.ReadTimeout("ambiguous")
    args = rig.trade()
    assert (await rig.place(args))["status"] == "unknown"
    assert (await rig.place(args))["status"] == "unknown"
    posts = [key for key, _ in rig.remote.calls if key[0] == "POST"]
    assert len(posts) == 1
    assert (await rig.place())["reason"] == "unresolved_execution_blocks_placement"


async def test_live_market_recheck(rig):
    rig.live()
    rig.remote.close_on_second_read = True
    assert (await rig.place())["reason"] == "market_not_open"
    assert not rig.remote.posts


@pytest.mark.parametrize("action,side,direction,price", [
    ("buy", "yes", "bid", "0.4700"), ("buy", "no", "ask", "0.5300"),
    ("sell", "yes", "ask", "0.4700"), ("sell", "no", "bid", "0.5300"),
])
async def test_exact_v2_translation(rig, action, side, direction, price):
    rig.remote.positions = [{"ticker": "TEST-MARKET", "position_fp": "10.00" if side == "yes" else "-10.00"}]
    result = await rig.place(rig.trade(action=action, side=side))
    assert result["status"] == "simulated"
    body = result["would_submit"]
    assert (body["side"], body["price"], body["count"]) == (direction, price, "10.00")
    assert "action" not in body and "yes_price" not in body


async def test_no_short_selling(rig):
    assert (await rig.place(rig.trade(action="sell")))["reason"] == "insufficient_uncommitted_contracts_to_sell"


async def test_readers_cannot_place_or_cancel(rig):
    for name in ("kalshi_place_order", "kalshi_cancel_order"):
        result = await rig.tools.call(name, {}, ["read"])
        assert result["reason"] == "director_permission_required"


async def test_explicit_taker_permission(rig):
    body = (await rig.place(rig.trade(allow_taker=True)))["would_submit"]
    assert body["post_only"] is False
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["exchange_index"] == rig.remote.market["exchange_index"]


async def test_maker_order_uses_market_exchange_and_remains_gtc(rig):
    body = (await rig.place(rig.trade()))["would_submit"]
    assert body["post_only"] is True
    assert body["time_in_force"] == "good_till_canceled"
    assert body["exchange_index"] == rig.remote.market["exchange_index"]


async def test_persisted_halt(rig):
    from src.database import Database
    rig.db.halt(True)
    assert Database(rig.settings.database_path, "production").halted()
    assert (await rig.place())["reason"] == "owner_kill_switch_active"


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500, 503])
async def test_mutation_errors_do_not_retry(rig, status):
    rig.live()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = status
    args = rig.trade()
    result = await rig.place(args)
    assert result["status"] in {"unknown", "rejected"}
    await rig.place(args)
    assert len([key for key, _ in rig.remote.calls if key[0] == "POST"]) == 1
