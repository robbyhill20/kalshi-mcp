from uuid import uuid4

import time

import httpx
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from src.auth import OwnerOAuth
from src.database import Database
from src.kalshi_client import KalshiClient, MarginClient
from src.perps import PerpOrders
from src.server import create_app

from .conftest import Exchange

AXE = ["read", "axe"]
DIRECTOR = ["read", "trade"]


def point(remote, ticker, event, series, index):
    remote.market = {**remote.market, "ticker": ticker, "event_ticker": event, "exchange_index": index}
    remote.event = {"event_ticker": event, "series_ticker": series}
    remote.series = {"ticker": series, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def use_index(remote, series="KXINX"):
    point(remote, f"{series}-26NOV09H1600-T7000", f"{series}-26NOV09H1600", series, 0)


async def test_axe_cannot_trade_or_cancel_sports_and_director_still_can(rig):
    sports = rig.trade()
    denied = await rig.tools.call("kalshi_place_order", sports, AXE)
    assert denied["reason"] == "series_not_allowed"
    assert rig.db.get(sports["request_id"]) is None
    assert not rig.remote.posts

    rig.live()
    director = rig.trade()
    placed = await rig.place(director)
    assert placed["status"] == "resting"
    assert rig.db.get(director["request_id"])["desk"] == "sports"
    cancel = {"request_id": str(uuid4()), "order_id": placed["order_id"]}
    assert (await rig.tools.call("kalshi_cancel_order", cancel, AXE))["reason"] == "order_not_placed_by_axe"
    assert rig.remote.deletes == []
    assert (await rig.tools.call("kalshi_get_order", {"order_id": placed["order_id"]}, AXE))["reason"] == "order_not_placed_by_axe"
    assert (await rig.tools.call("kalshi_cancel_order", {"request_id": str(uuid4()), "order_id": placed["order_id"]},
                                 DIRECTOR))["status"] == "cancelled"


async def test_axe_index_order_uses_its_own_caps_and_ledger(rig):
    rig.live()
    use_index(rig.remote)
    rig.settings.axe_max_daily_order_cost_cents = 500
    rig.settings.max_daily_order_cost_cents = 500
    first = rig.trade(ticker=rig.remote.market["ticker"])
    placed = await rig.tools.call("kalshi_place_order", first, AXE)
    assert placed["status"] == "resting"
    row = rig.db.get(first["request_id"])
    assert row["desk"] == "axe" and row["exchange_index"] == 0 and row["daily_risk_cents"] == 476
    assert rig.db.daily_budget(500, desk="axe")["committed_cents"] == 476
    assert rig.db.daily_budget(500)["committed_cents"] == 0
    second = rig.trade(ticker=rig.remote.market["ticker"], limit_price_cents=48)
    assert (await rig.tools.call("kalshi_place_order", second, AXE))["reason"] == "axe_daily_order_cost_limit"
    sports = rig.trade(ticker=rig.remote.market["ticker"], limit_price_cents=40)
    assert (await rig.place(sports))["status"] == "resting"
    assert rig.db.daily_budget(500)["committed_cents"] > 0
    assert rig.db.daily_budget(500, desk="axe")["committed_cents"] == 476


async def test_axe_per_order_cap_is_not_the_sports_cap(rig):
    use_index(rig.remote)
    rig.settings.axe_max_order_cost_cents = 100
    args = rig.trade(ticker=rig.remote.market["ticker"])
    assert (await rig.tools.call("kalshi_place_order", args, AXE))["reason"] == "axe_order_cost_limit"
    assert (await rig.place(rig.trade(ticker=rig.remote.market["ticker"])))["status"] == "simulated"


async def test_shared_shard_cash_blocks_sports_only_up_to_axe_reserve(rig):
    use_index(rig.remote)
    preview = await rig.tools.call("kalshi_place_order", rig.trade(ticker=rig.remote.market["ticker"]), AXE)
    required = preview["required_balance_cents"]
    rig.remote.balance = required
    rig.live()
    live = rig.trade(ticker=rig.remote.market["ticker"])
    assert (await rig.tools.call("kalshi_place_order", live, AXE))["status"] == "resting"
    blocked = await rig.place(rig.trade(ticker=rig.remote.market["ticker"], limit_price_cents=48))
    assert blocked["reason"] == "insufficient_available_balance"
    assert rig.db.daily_budget(20000)["committed_cents"] == 0
    assert rig.db.daily_budget(20000, desk="axe")["committed_cents"] == required
    assert len(rig.remote.posts) == 1

    rig.remote.shard3_active = True
    rig.remote.balance = 100000
    point(rig.remote, "TEST-MARKET", "TEST-EVENT", "TEST", 3)
    sports = await rig.place(rig.trade())
    assert sports["status"] == "resting"
    assert rig.db.get(sports["request_id"])["exchange_index"] == 3


async def test_axe_sees_only_its_open_orders(rig):
    rig.live()
    use_index(rig.remote)
    axe = rig.trade(ticker=rig.remote.market["ticker"])
    axe_order = await rig.tools.call("kalshi_place_order", axe, AXE)
    point(rig.remote, "TEST-MARKET", "TEST-EVENT", "TEST", 0)
    sports = await rig.place()
    visible = await rig.tools.call("kalshi_get_open_orders", {}, AXE)
    assert [row["order_id"] for row in visible["orders"]] == [axe_order["order_id"]]
    director = await rig.tools.call("kalshi_get_open_orders", {}, DIRECTOR)
    assert {row["order_id"] for row in director["orders"]} == {axe_order["order_id"], sports["order_id"]}


async def test_axe_can_cancel_only_its_event_order(rig):
    rig.live()
    use_index(rig.remote)
    placed = await rig.tools.call("kalshi_place_order", rig.trade(ticker=rig.remote.market["ticker"]), AXE)
    assert (await rig.tools.call("kalshi_cancel_order",
                                 {"request_id": str(uuid4()), "order_id": placed["order_id"]}, AXE))["status"] == "cancelled"
    assert rig.remote.deletes == [placed["order_id"]]


async def test_mismatched_series_is_rejected(rig):
    use_index(rig.remote)
    rig.remote.event["series_ticker"] = "KXNBA"
    rig.remote.series["ticker"] = "KXNBA"
    result = await rig.tools.call("kalshi_place_order", rig.trade(ticker=rig.remote.market["ticker"]), AXE)
    assert result["reason"] == "series_not_allowed"
    assert not rig.remote.posts


async def test_axe_bearer_scopes(settings):
    oauth = OwnerOAuth(settings, Database(settings.database_path, settings.kalshi_env))
    axe = await oauth.verify_token("x" * 48)
    director = await oauth.verify_token("d" * 48)
    assert axe.scopes == ["read", "axe"] and axe.client_id == "axe"
    assert director.scopes == ["read", "trade"] and "axe" not in director.scopes


def listed(client, token):
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Authorization": "Bearer " + token,
                                    "Accept": "application/json, text/event-stream",
                                    "MCP-Protocol-Version": "2025-11-25"})
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def test_discovery_hides_perp_tools_until_configured(settings):
    remote = Exchange()
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(remote.handle)))
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert len(listed(client, "r" * 48)) == 7
        assert listed(client, "d" * 48) == listed(client, "r" * 48) | {"kalshi_place_order", "kalshi_cancel_order"}
        assert listed(client, "x" * 48) == listed(client, "d" * 48)
        assert not any(name.startswith("kalshi_") and "margin" in name or name.endswith("perp_order")
                       for name in listed(client, "x" * 48))


def test_discovery_gives_perp_writes_to_axe_only(settings):
    remote = Exchange()
    settings.perps_enabled = True
    settings.kalshi_margin_api_key_id = SecretStr("margin-key")
    settings.kalshi_margin_private_key = settings.kalshi_private_key
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(remote.handle)))
    with TestClient(app, base_url="http://localhost:8000") as client:
        axe = listed(client, "x" * 48)
        director = listed(client, "d" * 48)
        reader = listed(client, "r" * 48)
        assert {"kalshi_place_perp_order", "kalshi_cancel_perp_order"} <= axe
        assert "kalshi_place_perp_order" not in director
        assert "kalshi_get_margin_balance" in reader
        assert "kalshi_place_perp_order" not in reader


class MarginBook:
    def __init__(self):
        self.calls = []
        self.posts = []
        self.deletes = []
        self.orders = {}
        self.equity = "1000.00"
        self.available = "1000.00"
        self.positions = []
        self.fractional = False
        self.mark_price = "7.1777"
        self.mark_ts_ms = None
        self.omit_mark = False

    def market(self):
        body = {"ticker": "KXTSLAPERP1", "status": "active", "tick_size": "0.0001",
                "fractional_trading_enabled": self.fractional, "contract_size": "0.020000",
                "market_version": 1, "schedule": None, "price": "7.1777", "minimum_quantity": "1.00"}
        if not self.omit_mark:
            ts = time.time_ns() // 1_000_000 if self.mark_ts_ms is None else self.mark_ts_ms
            body["settlement_mark_price"] = {"price": self.mark_price, "ts_ms": ts}
        return body

    async def handle(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        assert path.startswith("/trade-api/v2/margin/"), path
        assert request.headers["KALSHI-ACCESS-KEY"] == "margin-key"
        if request.method == "GET" and path == "/trade-api/v2/margin/markets/KXTSLAPERP1":
            return httpx.Response(200, json={"market": self.market()})
        if request.method == "GET" and path == "/trade-api/v2/margin/markets":
            return httpx.Response(200, json={"markets": [self.market()]})
        if path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook": {"bids": [["7.1700", "2.00"]], "asks": []}})
        if path == "/trade-api/v2/margin/positions":
            return httpx.Response(200, json={"positions": self.positions})
        if path == "/trade-api/v2/margin/balance":
            assert request.url.params["compute_available_balance"] == "true"
            sub = {"subaccount": 0, "subaccount_type": "cross", "position_value": "0.00",
                   "account_equity": self.equity, "maintenance_margin": "0.00", "initial_margin": "0.00",
                   "resting_orders_margin": "0.00", "available_balance": self.available}
            return httpx.Response(200, json={"subaccount_balances": [sub], "settled_funds": "0.00"})
        if request.method == "POST" and path == "/trade-api/v2/margin/orders":
            import json
            body = json.loads(request.content)
            self.posts.append(body)
            order = {"order_id": str(uuid4()), "client_order_id": body["client_order_id"], "ticker": body["ticker"],
                     "side": body["side"], "price": body["price"], "fill_count": "0.00",
                     "remaining_count": body["count"], "last_update_reason": ""}
            self.orders[order["order_id"]] = order
            return httpx.Response(201, json={"order_id": order["order_id"], "client_order_id": body["client_order_id"],
                                            "fill_count": "0.00", "remaining_count": body["count"]})
        if request.method == "GET" and path.startswith("/trade-api/v2/margin/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"order": order}) if order else httpx.Response(404, json={})
        if request.method == "DELETE" and path.startswith("/trade-api/v2/margin/orders/"):
            oid = path.rsplit("/", 1)[1]
            self.deletes.append(oid)
            self.orders[oid].update(remaining_count="0.00", last_update_reason="MarginCancel")
            return httpx.Response(200, json={"order_id": oid, "reduced_by": "1.00"})
        if path == "/trade-api/v2/margin/orders":
            return httpx.Response(200, json={"orders": list(self.orders.values()), "cursor": ""})
        return httpx.Response(404, json={})


def arm_margin(rig, book=None):
    book = book or MarginBook()
    pem = rig.settings.kalshi_private_key.get_secret_value()
    rig.settings.perps_enabled = True
    rig.settings.require_trade_approval = False
    rig.settings.kalshi_margin_api_key_id = SecretStr("margin-key")
    rig.settings.kalshi_margin_private_key = SecretStr(pem)
    margin = MarginClient(rig.settings, httpx.MockTransport(book.handle))
    rig.tools.margin = margin
    rig.tools.perps = PerpOrders(rig.settings, margin, rig.db)
    return book


def perp(count="1", price="7.1777", **overrides):
    return {"request_id": str(uuid4()), "ticker": "KXTSLAPERP1", "side": "bid",
            "count": count, "price": price, "time_in_force": "good_till_canceled", **overrides}


async def test_perps_stay_off_without_flag_or_credentials(rig):
    assert (await rig.tools.call("kalshi_place_perp_order", {}, AXE))["reason"] == "perps_disabled"
    assert (await rig.tools.call("kalshi_get_margin_markets", {}, DIRECTOR))["reason"] == "perps_disabled"
    rig.settings.perps_enabled = True
    assert (await rig.tools.call("kalshi_get_margin_balance", {}, AXE))["reason"] == "perps_not_configured"
    assert not rig.remote.calls


async def test_perp_validation_and_axe_only_writes(rig):
    book = arm_margin(rig)
    assert (await rig.tools.call("kalshi_place_perp_order", perp(ticker="KXBTCPERP1"), AXE))["reason"] == "perp_ticker_not_allowed"
    assert book.calls == []
    rig.settings.axe_perp_max_order_notional_cents = 500
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), AXE))["reason"] == "axe_perp_order_notional_limit"
    rig.settings.axe_perp_max_order_notional_cents = 10000
    book.equity, book.available = "5.00", "5.00"
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), AXE))["reason"] == "perp_leverage_cap"
    book.equity, book.available = "1000.00", "1000.00"
    assert (await rig.tools.call("kalshi_place_perp_order", perp(count="1.50"), AXE))["reason"] == "fractional_contracts_disabled"
    assert (await rig.tools.call("kalshi_place_perp_order", perp(reduce_only=True), AXE))["reason"] == "reduce_only_requires_immediate_time_in_force"
    assert not book.posts
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), DIRECTOR))["reason"] == "axe_permission_required"
    dry = await rig.tools.call("kalshi_place_perp_order", perp(), AXE)
    assert dry["status"] == "simulated" and dry["submitted"] is False
    assert dry["would_submit"]["self_trade_prevention_type"] == "taker_at_cross"
    assert dry["would_submit"]["ticker"] == "KXTSLAPERP1"
    assert not book.posts
    assert rig.db.daily_budget(20000, desk="axe", kind="perp_place")["committed_cents"] == 0
    markets = await rig.tools.call("kalshi_get_margin_markets", {"status": "active"}, AXE)
    assert markets["markets"][0]["ticker"] == "KXTSLAPERP1"


async def test_perp_live_order_is_idempotent_and_cancel_is_owned(rig):
    book = arm_margin(rig)
    rig.live()
    args = perp()
    first = await rig.tools.call("kalshi_place_perp_order", args, AXE)
    second = await rig.tools.call("kalshi_place_perp_order", args, AXE)
    assert first["status"] == "resting" and second["replayed"] is True
    assert len(book.posts) == 1
    assert book.posts[0]["client_order_id"] == args["request_id"]
    assert rig.db.daily_budget(20000)["committed_cents"] == 0
    assert rig.db.daily_budget(20000, desk="axe", kind="perp_place")["committed_cents"] == 718
    stranger = str(uuid4())
    assert (await rig.tools.call("kalshi_cancel_perp_order",
                                 {"request_id": str(uuid4()), "order_id": stranger}, AXE))["reason"] == "order_not_placed_by_axe"
    assert book.deletes == []
    cancelled = await rig.tools.call("kalshi_cancel_perp_order",
                                     {"request_id": str(uuid4()), "order_id": first["order_id"]}, AXE)
    assert cancelled["status"] == "cancelled"
    assert book.deletes == [first["order_id"]]


async def test_perp_gates_match_event_orders(rig):
    book = arm_margin(rig)
    rig.db.halt(True)
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), AXE))["reason"] == "owner_kill_switch_active"
    rig.db.halt(False)
    rig.settings.dry_run = False
    rig.settings.trading_enabled = False
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), AXE))["reason"] == "trading_disabled"
    assert not book.posts


@pytest.mark.parametrize("side", ["bid", "ask"])
async def test_perp_exposure_uses_mark_not_a_far_limit(rig, side):
    book = arm_margin(rig)
    book.mark_price = "7.0000"
    book.equity = "50.00"
    book.available = "100000.00"
    result = await rig.tools.call("kalshi_place_perp_order", perp(side=side, count="10", price="0.0100"), AXE)
    assert result["reason"] == "perp_leverage_cap"
    assert not book.posts


async def test_resting_axe_perps_count_toward_leverage(rig):
    book = arm_margin(rig)
    rig.live()
    book.equity = "100.00"
    book.available = "100000.00"
    first = await rig.tools.call("kalshi_place_perp_order", perp(count="10", price="7.1777"), AXE)
    assert first["status"] == "resting"
    second = await rig.tools.call("kalshi_place_perp_order", perp(count="10", price="7.1777"), AXE)
    assert second["reason"] == "perp_leverage_cap"
    assert len(book.posts) == 1


async def test_missing_or_stale_mark_fails_closed(rig):
    book = arm_margin(rig)
    book.omit_mark = True
    missing = await rig.tools.call("kalshi_place_perp_order", perp(), AXE)
    assert missing["reason"] == "missing_mark_price"
    book.omit_mark = False
    book.mark_ts_ms = 0
    stale = await rig.tools.call("kalshi_place_perp_order", perp(), AXE)
    assert stale["reason"] == "stale_mark_price"
    assert not book.posts


async def test_perp_place_honors_approval_flag(rig):
    book = arm_margin(rig)
    rig.settings.require_trade_approval = True
    denied = await rig.tools.call("kalshi_place_perp_order", perp(), AXE)
    assert denied["reason"] == "missing_expired_or_mismatched_owner_approval"
    assert not book.calls
    from src.models import PlacePerpInput
    args = perp()
    approved = PlacePerpInput.model_validate(args)
    args["approval_id"] = rig.db.approve(rig.tools.perps.terms(approved))
    assert (await rig.tools.call("kalshi_place_perp_order", args, AXE))["status"] == "simulated"
    rig.settings.require_trade_approval = False
    assert (await rig.tools.call("kalshi_place_perp_order", perp(), AXE))["status"] == "simulated"


@pytest.mark.parametrize("symbol", ["TSLA", "NVDA", "AAPL"])
def test_default_perp_symbols_match_demo_tickers(settings, symbol):
    from src.perps import perp_ticker_allowed
    assert perp_ticker_allowed(f"KX{symbol}PERP1", settings.axe_perp_symbol_set)
    assert not perp_ticker_allowed("KXBTCPERP1", settings.axe_perp_symbol_set)
    assert settings.axe_index_series_set == {"KXINX", "KXINXU", "KXINXPOS", "KXNASDAQ100", "KXNASDAQ100U", "KXDJI"}


def _margin_transport(seen):
    async def handle(request):
        seen.append((request.headers["KALSHI-ACCESS-KEY"], request.url.path))
        return httpx.Response(200, json={"markets": []})

    return handle


async def _read_margin_markets(settings, seen):
    client = MarginClient(settings, httpx.MockTransport(_margin_transport(seen)))
    try:
        await client.markets()
    finally:
        await client.close()


async def test_margin_markets_use_event_key_when_margin_secrets_are_absent(settings):
    seen = []
    settings.perps_enabled = True
    for margin_key, margin_pem in ((None, None), (SecretStr(""), SecretStr(""))):
        settings.kalshi_margin_api_key_id = margin_key
        settings.kalshi_margin_private_key = margin_pem
        await _read_margin_markets(settings, seen)
    assert len(seen) == 2
    for access_key, path in seen:
        assert access_key == "test-key-id"
        assert path.startswith("/trade-api/v2/margin/")


async def test_explicit_margin_key_signs_margin_markets(settings):
    seen = []
    settings.kalshi_margin_api_key_id = SecretStr("margin-key")
    settings.kalshi_margin_private_key = settings.kalshi_private_key
    await _read_margin_markets(settings, seen)
    access_key, path = seen[0]
    assert access_key == "margin-key"
    assert path.startswith("/trade-api/v2/margin/")


def test_perps_default_off(settings):
    assert settings.perps_enabled is False
    assert settings.perps_configured() is False


def test_discovery_lists_margin_balance_for_reader_without_margin_secrets(settings):
    remote = Exchange()
    settings.perps_enabled = True
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(remote.handle)))
    with TestClient(app, base_url="http://localhost:8000") as client:
        axe = listed(client, "x" * 48)
        director = listed(client, "d" * 48)
        reader = listed(client, "r" * 48)
        assert {"kalshi_place_perp_order", "kalshi_cancel_perp_order"} <= axe
        assert "kalshi_place_perp_order" not in director
        assert "kalshi_get_margin_balance" in reader
        assert "kalshi_place_perp_order" not in reader


@pytest.mark.parametrize("which", ["key_only", "key_and_blank_pem", "pem_only", "pem_and_blank_key"])
async def test_partial_margin_pair_signs_nothing(settings, which):
    settings.perps_enabled = True
    event_pem = settings.kalshi_private_key
    if which == "key_only":
        settings.kalshi_margin_api_key_id = SecretStr("margin-key")
        settings.kalshi_margin_private_key = None
    elif which == "key_and_blank_pem":
        settings.kalshi_margin_api_key_id = SecretStr("margin-key")
        settings.kalshi_margin_private_key = SecretStr("")
    elif which == "pem_only":
        settings.kalshi_margin_api_key_id = None
        settings.kalshi_margin_private_key = event_pem
    else:
        settings.kalshi_margin_api_key_id = SecretStr("")
        settings.kalshi_margin_private_key = event_pem
    remote = Exchange()
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(remote.handle)))
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert "kalshi_get_margin_balance" not in listed(client, "r" * 48)
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "kalshi_get_margin_balance", "arguments": {}}},
                               headers={"Authorization": "Bearer " + "r" * 48,
                                        "Accept": "application/json, text/event-stream",
                                        "MCP-Protocol-Version": "2025-11-25"})
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"]["reason"] == "perps_not_configured"
    assert remote.calls == []
    built = None
    try:
        built = MarginClient(settings)
    except ValueError as exc:
        assert str(exc) == "Set both KALSHI_MARGIN_API_KEY_ID and KALSHI_MARGIN_PRIVATE_KEY, or leave both empty"
    else:
        raise AssertionError("partial margin pair must be refused")
    finally:
        if built is not None:
            await built.close()
