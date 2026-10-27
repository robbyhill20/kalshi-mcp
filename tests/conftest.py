import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.config import Settings
from src.database import Database
from src.kalshi_client import KalshiClient
from src.models import ApprovalInput
from src.orders import Orders
from src.tools import Tools


class Exchange:
    def __init__(self):
        self.calls = []
        self.posts = []
        self.deletes = []
        self.orders = {}
        self.balance = 100000
        self.event = {"event_ticker": "TEST-EVENT", "series_ticker": "TEST"}
        self.series = {"ticker": "TEST", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
        self.positions = []
        self.fail = {}
        self.market_reads = 0
        self.close_on_second_read = False
        self.place_status = "resting"
        self.fill_count = "0.00"
        self.pages = {}
        self.market = {"ticker": "TEST-MARKET", "event_ticker": "TEST-EVENT", "market_type": "binary", "status": "active",
                       "open_time": "2020-01-01T00:00:00Z", "close_time": "2099-01-01T00:00:00Z",
                       "notional_value_dollars": "1.0000", "exchange_index": 0,
                       "price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.01"}]}

    def order(self, **overrides):
        return {"order_id": str(uuid4()), "ticker": "TEST-MARKET", "client_order_id": str(uuid4()),
                "book_side": "bid", "outcome_side": "yes", "yes_price_dollars": "0.4700",
                "no_price_dollars": "0.5300", "status": "resting", "fill_count_fp": "0.00",
                "remaining_count_fp": "10.00", "initial_count_fp": "10.00", "subaccount_number": 0,
                "last_update_time": "2026-10-26T00:00:00Z", **overrides}

    async def handle(self, request):
        path = request.url.path.removeprefix("/trade-api/v2")
        key = (request.method, path)
        self.calls.append((key, dict(request.url.params)))
        assert request.headers["KALSHI-ACCESS-KEY"] == "test-key-id"
        assert "KALSHI-ACCESS-SIGNATURE" in request.headers
        if key in self.fail:
            value = self.fail[key]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(value, json={"message": "sensitive error body must never be logged"})
        if path == "/exchange/status":
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance": self.balance, "balance_dollars": f"{self.balance / 100:.4f}",
                                            "portfolio_value": 0, "updated_ts": 0})
        if path == "/portfolio/positions":
            return httpx.Response(200, json={"market_positions": self.positions, "event_positions": [], "cursor": ""})
        if path == "/markets/TEST-MARKET":
            self.market_reads += 1
            market = self.market.copy()
            if self.close_on_second_read and self.market_reads > 1:
                market["status"] = "closed"
            return httpx.Response(200, json={"market": market})
        if path == "/events/TEST-EVENT":
            return httpx.Response(200, json={"event": self.event})
        if path == "/series/TEST":
            return httpx.Response(200, json={"series": self.series})
        if path == "/markets":
            return httpx.Response(200, json={"markets": [self.market], "cursor": ""})
        if path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.4500", "10.00"]], "no_dollars": []}})
        if path == "/portfolio/orders":
            cursor = request.url.params.get("cursor", "")
            if cursor in self.pages:
                return httpx.Response(200, json=self.pages[cursor])
            orders = list(self.orders.values())
            if request.url.params.get("status"):
                orders = [o for o in orders if o["status"] == request.url.params["status"]]
            if request.url.params.get("ticker"):
                orders = [o for o in orders if o["ticker"] == request.url.params["ticker"]]
            return httpx.Response(200, json={"orders": orders, "cursor": ""})
        if request.method == "GET" and path.startswith("/portfolio/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"order": order}) if order else httpx.Response(404, json={})
        if request.method == "POST" and path == "/portfolio/events/orders":
            body = json.loads(request.content)
            self.posts.append(body)
            count = float(body["count"])
            remaining = f"{count - float(self.fill_count):.2f}"
            order = self.order(client_order_id=body["client_order_id"], ticker=body["ticker"],
                               book_side=body["side"], yes_price_dollars=body["price"],
                               status=self.place_status, fill_count_fp=self.fill_count,
                               remaining_count_fp=remaining, initial_count_fp=body["count"])
            self.orders[order["order_id"]] = order
            return httpx.Response(201, json={"order_id": order["order_id"], "client_order_id": body["client_order_id"],
                                            "fill_count": self.fill_count, "remaining_count": remaining, "ts_ms": 1})
        if request.method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            oid = path.rsplit("/", 1)[1]
            self.deletes.append(oid)
            self.orders[oid].update(status="canceled", remaining_count_fp="0.00")
            return httpx.Response(200, json={"order_id": oid, "reduced_by": "10.00", "ts_ms": 1})
        return httpx.Response(404, json={})


@pytest.fixture
def settings(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return Settings(_env_file=None, kalshi_api_key_id="test-key-id", kalshi_private_key=pem,
                    admin_token="a" * 48, mcp_read_token="r" * 48, mcp_director_token="d" * 48,
                    database_path=tmp_path / "state.sqlite3")


@pytest_asyncio.fixture
async def rig(settings):
    remote = Exchange()
    client = KalshiClient(settings, httpx.MockTransport(remote.handle))
    db = Database(settings.database_path, settings.kalshi_env)
    orders = Orders(settings, client, db)
    tools = Tools(settings, client, db, orders)

    def trade(**overrides):
        args = {"request_id": str(uuid4()), "ticker": "TEST-MARKET", "side": "yes", "action": "buy",
                "contracts": 10, "limit_price_cents": 47, "allow_taker": False, **overrides}
        terms = ApprovalInput.model_validate(args)
        args["approval_id"] = db.approve(orders.terms(terms))
        return args

    async def place(args=None):
        return await tools.call("kalshi_place_order", args or trade(), ["read", "trade"])

    def live():
        settings.dry_run = False
        settings.trading_enabled = True

    yield SimpleNamespace(settings=settings, remote=remote, client=client, db=db, orders=orders,
                          tools=tools, trade=trade, place=place, live=live)
    await client.close()
