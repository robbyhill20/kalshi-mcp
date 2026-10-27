import pytest


@pytest.mark.parametrize("name,args,field", [
    ("kalshi_get_market", {"ticker": "TEST-MARKET"}, "market"),
    ("kalshi_get_balance", {}, "balance"),
    ("kalshi_get_positions", {}, "market_positions"),
    ("kalshi_get_markets", {}, "markets"),
    ("kalshi_get_orderbook", {"ticker": "TEST-MARKET"}, "orderbook_fp"),
    ("kalshi_get_open_orders", {}, "orders"),
])
async def test_read_tools(rig, name, args, field):
    assert field in await rig.tools.call(name, args, ["read"])
    assert not rig.remote.posts and not rig.remote.deletes


@pytest.mark.parametrize("args", [
    {"ticker": "../../portfolio/orders"}, {"ticker": "https://evil.test"},
    {"ticker": "TEST-MARKET", "url": "https://evil.test"},
    {"ticker": "TEST-MARKET", "KALSHI_ENV": "demo"},
    {"ticker": "TEST-MARKET", "command": "whoami"},
])
async def test_unsafe_arguments_rejected(rig, args):
    result = await rig.tools.call("kalshi_get_market", args, ["read"])
    assert result["reason"] == "invalid_arguments"
    assert not rig.remote.calls


async def test_no_generic_request_tool(rig):
    assert (await rig.tools.call("kalshi_request", {}, ["read", "trade"]))["reason"] == "unknown_tool"
