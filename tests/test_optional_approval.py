import time
from uuid import uuid4

import httpx

from src.database import Database
from src.models import PlaceOrderInput
from src.orders import Orders


def instruction(rig, **changes):
    rig.settings.require_trade_approval = False
    return {"request_id": str(uuid4()), "ticker": "TEST-MARKET", "side": "yes", "action": "buy",
            "contracts": 10, "limit_price_cents": 47, **changes}


async def test_no_approval_dry_run(rig):
    result = await rig.place(instruction(rig))
    assert result["status"] == "simulated"
    assert result["approval_id"] is None and result["approval_required"] is False
    assert not rig.remote.posts


async def test_no_approval_does_not_enable_live_trading(rig):
    args = instruction(rig)
    rig.settings.dry_run = False
    assert (await rig.place(args))["reason"] == "trading_disabled"
    assert not rig.remote.posts


async def test_no_approval_idempotency_after_restart(rig):
    rig.live()
    args = instruction(rig)
    first = await rig.place(args)
    db = Database(rig.settings.database_path, "production")
    result = await Orders(rig.settings, rig.client, db).place(PlaceOrderInput.model_validate(args))
    assert result["order_id"] == first["order_id"]
    assert len(rig.remote.posts) == 1


async def test_approval_required_when_enabled(rig):
    args = instruction(rig)
    rig.settings.require_trade_approval = True
    assert (await rig.place(args))["reason"] == "missing_expired_or_mismatched_owner_approval"


async def test_supplied_approval_still_checked_in_optional_mode(rig):
    result = await rig.place(instruction(rig, approval_id=str(uuid4())))
    assert result["reason"] == "missing_expired_or_mismatched_owner_approval"


async def test_daily_ceiling_blocks_repeated_orders(rig):
    rig.live()
    rig.settings.max_daily_order_cost_cents = 500
    assert (await rig.place(instruction(rig)))["status"] == "resting"
    assert (await rig.place(instruction(rig, limit_price_cents=48)))["reason"] == "owner_daily_order_cost_limit"
    assert len(rig.remote.posts) == 1
    budget = rig.db.daily_budget(500)
    assert budget["committed_cents"] == 476 and budget["remaining_cents"] == 24


async def test_unknown_submission_keeps_daily_commitment(rig):
    rig.live()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = httpx.ReadTimeout("unknown")
    assert (await rig.place(instruction(rig)))["status"] == "unknown"
    assert rig.db.daily_budget(10000)["committed_cents"] == 476


async def test_dry_run_does_not_spend_daily_budget(rig):
    await rig.place(instruction(rig))
    assert rig.db.daily_budget(10000)["committed_cents"] == 0


async def test_cancel_does_not_refund_daily_budget(rig):
    rig.live()
    placed = await rig.place(instruction(rig))
    await rig.tools.call("kalshi_cancel_order", {"request_id": str(uuid4()), "order_id": placed["order_id"]}, ["read", "trade"])
    assert rig.db.daily_budget(10000)["committed_cents"] == 476


async def test_definitive_rejection_releases_daily_budget(rig):
    rig.live()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = 400
    assert (await rig.place(instruction(rig)))["status"] == "rejected"
    assert rig.db.daily_budget(10000)["committed_cents"] == 0


async def test_daily_budget_survives_restart_and_resets_in_utc(rig):
    rig.live()
    await rig.place(instruction(rig))
    db = Database(rig.settings.database_path, "production")
    assert db.daily_budget(10000)["committed_cents"] == 476
    assert db.daily_budget(10000, now=time.time() + 86400)["committed_cents"] == 0


async def test_reader_still_cannot_trade_without_approval_mode(rig):
    args = instruction(rig)
    assert (await rig.tools.call("kalshi_place_order", args, ["read"]))["reason"] == "director_permission_required"


async def test_bot_cannot_change_approval_or_budget_policy(rig):
    args = instruction(rig, require_trade_approval=False, max_daily_order_cost_cents=999999)
    assert (await rig.place(args))["reason"] == "invalid_arguments"
