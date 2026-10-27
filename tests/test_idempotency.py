import asyncio
import json
from uuid import uuid4

from src.database import Database
from src.models import GetOrderInput
from src.orders import Orders


async def test_duplicate_request(rig):
    rig.live()
    args = rig.trade()
    first = await rig.place(args)
    second = await rig.place(args)
    assert first["order_id"] == second["order_id"]
    assert second["replayed"] is True
    assert len(rig.remote.posts) == 1


async def test_restart_preserves_idempotency(rig):
    rig.live()
    args = rig.trade()
    first = await rig.place(args)
    db = Database(rig.settings.database_path, "production")
    restarted = Orders(rig.settings, rig.client, db)
    from src.models import PlaceOrderInput
    second = await restarted.place(PlaceOrderInput.model_validate(args))
    assert first["order_id"] == second["order_id"]
    assert len(rig.remote.posts) == 1


async def test_changed_payload_for_same_request(rig):
    args = rig.trade()
    await rig.place(args)
    args["limit_price_cents"] = 48
    assert (await rig.place(args))["reason"] == "request_id_conflict"


async def test_approval_cannot_be_reused(rig):
    args = rig.trade()
    await rig.place(args)
    args["request_id"] = str(uuid4())
    assert (await rig.place(args))["status"] == "rejected"


async def test_concurrent_same_request(rig):
    rig.live()
    args = rig.trade()
    results = await asyncio.gather(*(rig.place(args) for _ in range(8)))
    assert any(r.get("order_id") for r in results)
    assert len(rig.remote.posts) == 1


async def test_reconcile_accepted_but_response_lost(rig):
    import httpx
    rig.live()
    original = rig.remote.handle

    async def lost_response(request):
        response = await original(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("ack lost")
        return response

    rig.client.http = httpx.AsyncClient(base_url=rig.settings.api_origin, transport=httpx.MockTransport(lost_response))
    args = rig.trade()
    assert (await rig.place(args))["status"] == "unknown"
    result = await rig.place(args)
    assert result["status"] == "resting" and result["reconciled"]
    assert len(rig.remote.posts) == 1


async def test_reconcile_unknown_after_restart_never_reposts(rig):
    import httpx
    rig.live()
    args = rig.trade()
    rig.remote.fail[("POST", "/portfolio/events/orders")] = httpx.ReadTimeout("lost")
    await rig.place(args)
    restarted = Orders(rig.settings, rig.client, Database(rig.settings.database_path, "production"))
    result = await restarted.get_order(GetOrderInput(request_id=args["request_id"]))
    assert result["status"] == "unknown"
    assert len([key for key, _ in rig.remote.calls if key[0] == "POST"]) == 1


async def test_reserve_persists_after_fill(rig):
    rig.live()
    rig.remote.place_status, rig.remote.fill_count = "executed", "10.00"
    args = rig.trade()
    await rig.place(args)
    row = rig.db.get(args["request_id"])
    assert row["reserve_cents"] == 1470
    assert await rig.orders.outstanding_reserve({"updated_ts": 0}) == 1470


async def test_startup_recovers_pre_submit_crash(rig):
    args = rig.trade()
    terms = rig.orders.terms(__import__("src.models", fromlist=["PlaceOrderInput"]).PlaceOrderInput.model_validate(args))
    payload = {**args, "dry_run": True, "environment": "production"}
    rig.db.claim(args["request_id"], payload, terms=terms, approval_id=args["approval_id"])
    rig.db.recover_validating()
    row = rig.db.get(args["request_id"])
    assert json.loads(row["result"])["reason"] == "interrupted_before_submission"


def test_database_has_unique_approval_constraint(rig):
    with rig.db.connection() as c:
        indexes = c.execute("PRAGMA index_list(requests)").fetchall()
        assert len([i for i in indexes if i["unique"]]) >= 2
