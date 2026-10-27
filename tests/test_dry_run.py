async def test_safe_defaults_and_valid_dry_run(rig):
    assert rig.settings.dry_run is True
    assert rig.settings.trading_enabled is False
    result = await rig.place()
    assert result["status"] == "simulated" and result["validation"] == "passed"
    assert result["would_submit"]["post_only"] is True
    assert result["fill_status"] == "not_simulated"
    assert not rig.remote.posts
    paths = [key[1] for key, _ in rig.remote.calls]
    for path in ["/portfolio/balance", "/portfolio/positions", "/portfolio/orders", "/exchange/status"]:
        assert path in paths
    assert paths.count("/markets/TEST-MARKET") == 2


async def test_trading_disabled(rig):
    rig.settings.dry_run = False
    assert (await rig.place())["reason"] == "trading_disabled"
    assert not rig.remote.posts


async def test_dry_run_cancellation(rig):
    from uuid import uuid4
    order = rig.remote.order()
    rig.remote.orders[order["order_id"]] = order
    result = await rig.tools.call("kalshi_cancel_order", {"request_id": str(uuid4()), "order_id": order["order_id"]}, ["read", "trade"])
    assert result["status"] == "simulated"
    assert not rig.remote.deletes


async def test_dry_run_does_not_become_live_on_replay(rig):
    args = rig.trade()
    await rig.place(args)
    rig.live()
    assert (await rig.place(args))["reason"] == "request_id_conflict"
    assert not rig.remote.posts
