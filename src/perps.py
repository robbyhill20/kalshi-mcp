import asyncio
import json
import time
from decimal import ROUND_CEILING, Decimal
from time import monotonic
from uuid import UUID

from .errors import APIError, Rejection
from .validation import decimal


def perp_ticker_allowed(ticker, symbols):
    if not ticker.startswith("KX") or "PERP" not in ticker:
        return False
    symbol, _, generation = ticker[2:].partition("PERP")
    return bool(symbol and symbol in symbols and generation.isdigit())


def perp_terms(order, settings):
    return {**order.model_dump(mode="json", exclude={"approval_id"}),
            "environment": settings.kalshi_env, "dry_run": settings.dry_run}


def mark_price(market, max_age_seconds, now_ms=None):
    # A low ask is allowed to fill near the mark, not at its limit.
    quoted = market.get("settlement_mark_price")
    if not isinstance(quoted, dict):
        raise Rejection("missing_mark_price")
    ts = quoted.get("ts_ms")
    if type(ts) is not int or quoted.get("price") is None:
        raise Rejection("missing_mark_price")
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    age_ms = now_ms - ts
    if age_ms < 0 or age_ms > max_age_seconds * 1000:
        raise Rejection("stale_mark_price")
    mark = decimal(quoted["price"])
    if mark <= 0:
        raise Rejection("missing_mark_price")
    return mark


def margin_order_state(order):
    fill = decimal(order.get("fill_count") or "0")
    remaining = decimal(order.get("remaining_count") or "0")
    reason = order.get("last_update_reason") or ""
    if fill < 0 or remaining < 0:
        raise Rejection("invalid_exchange_order_counts")
    if remaining > 0 and fill > 0:
        state = "partially_filled"
    elif remaining > 0:
        state = "resting"
    elif fill > 0:
        state = "filled"
    elif reason in {"MarginCancel", "SelfTradeCancel", "ExpiryCancel", "PostOnlyCrossCancel",
                    "ReduceOnlyCancel", "Decrease"}:
        state = "cancelled"
    else:
        state = "unknown"
    return {"status": state, "order_id": order.get("order_id"),
            "filled_contracts": str(fill), "remaining_contracts": str(remaining),
            "exchange_reason": reason, "order": order}


class PerpOrders:
    def __init__(self, settings, margin, database):
        self.settings, self.margin, self.db = settings, margin, database
        self.inflight = set()

    def require_placement(self):
        if self.db.halted():
            raise Rejection("owner_kill_switch_active")
        if not self.settings.dry_run and not self.settings.trading_enabled:
            raise Rejection("trading_disabled")

    def body(self, order, market, count, price):
        body = {"ticker": order.ticker, "client_order_id": str(order.request_id),
                "side": order.side, "count": f"{count:.2f}", "price": f"{price:.4f}",
                "time_in_force": order.time_in_force, "post_only": order.post_only,
                "self_trade_prevention_type": "taker_at_cross", "cancel_order_on_pause": True,
                "reduce_only": order.reduce_only, "subaccount": 0}
        version = market.get("market_version")
        if type(version) is int:
            body["market_version"] = version
        return body

    def terms(self, order):
        return perp_terms(order, self.settings)

    def open_exposure(self, order, open_orders):
        axe_ids = self.db.request_ids_for("axe", "perp_place")
        exposure = Decimal(0)
        seen = set()
        for row in open_orders:
            client_id = row.get("client_order_id")
            if row.get("ticker") not in {None, order.ticker} or client_id not in axe_ids:
                continue
            if client_id == str(order.request_id):
                continue
            remaining = decimal(row.get("remaining_count") or "0")
            if remaining <= 0:
                continue
            side = row.get("side")
            if side not in {"bid", "ask"}:
                raise Rejection("unrecognized_existing_order_direction")
            exposure += remaining if side == "bid" else -remaining
            seen.add(client_id)
        for row in self.db.rows_for("axe", "perp_place"):
            if row["request_id"] in seen or row["request_id"] == str(order.request_id):
                continue
            if row["status"] in {"filled", "cancelled", "expired", "rejected", "simulated"}:
                continue
            payload = json.loads(row["payload"])
            if payload.get("ticker") != order.ticker or payload.get("side") not in {"bid", "ask"}:
                continue
            remaining = decimal(payload.get("count") or "0")
            if remaining <= 0:
                continue
            exposure += remaining if payload["side"] == "bid" else -remaining
        return exposure

    def validate(self, order, market, positions, balance, open_orders):
        if market.get("ticker") != order.ticker:
            raise Rejection("market_ticker_mismatch")
        if market.get("status") != "active":
            raise Rejection("market_not_open")
        schedule = market.get("schedule")
        if isinstance(schedule, dict) and schedule.get("is_open") is False:
            raise Rejection("market_not_open")
        if order.reduce_only and order.time_in_force not in {"immediate_or_cancel", "fill_or_kill"}:
            raise Rejection("reduce_only_requires_immediate_time_in_force")
        if order.post_only and order.time_in_force != "good_till_canceled":
            raise Rejection("post_only_requires_gtc")
        count, price = decimal(order.count), decimal(order.price)
        if count <= 0 or price <= 0:
            raise Rejection("invalid_perp_size")
        tick = decimal(market.get("tick_size"))
        if tick <= 0 or price % tick != 0:
            raise Rejection("price_not_on_market_grid")
        if not market.get("fractional_trading_enabled") and count != count.to_integral_value():
            raise Rejection("fractional_contracts_disabled")
        minimum = market.get("minimum_quantity")
        if minimum is not None and count < decimal(minimum):
            raise Rejection("below_minimum_quantity")
        # Position notional is |qty| * mark. A crossing order fills near the mark,
        # so a limit below the mark must not shrink the cap. contract_size is already
        # inside the quoted contract price.
        mark = mark_price(market, self.settings.snapshot_max_age_seconds)
        exposure_price = max(price, mark)
        notional = count * exposure_price
        notional_cents = int((notional * 100).to_integral_value(rounding=ROUND_CEILING))
        if notional_cents > self.settings.axe_perp_max_order_notional_cents:
            raise Rejection("axe_perp_order_notional_limit")
        current = self.open_exposure(order, open_orders)
        for row in positions:
            if row.get("market_ticker") == order.ticker and row.get("subaccount") in {None, 0}:
                current += decimal(row.get("position"))
        delta = count if order.side == "bid" else -count
        new_position = current + delta
        increasing = abs(new_position) > abs(current)
        sub = primary_subaccount(balance)
        if increasing:
            equity = decimal(sub.get("account_equity"))
            # The create-order body has no leverage field. This is our exposure cap.
            if equity <= 0 or abs(new_position) * exposure_price > self.settings.axe_perp_max_leverage * equity:
                raise Rejection("perp_leverage_cap")
            if notional / self.settings.axe_perp_max_leverage > decimal(sub.get("available_balance")):
                raise Rejection("insufficient_margin_balance")
        return notional_cents, count, price

    async def place(self, order):
        if not perp_ticker_allowed(order.ticker, self.settings.axe_perp_symbol_set):
            raise Rejection("perp_ticker_not_allowed")
        payload = {**order.model_dump(mode="json"), "dry_run": self.settings.dry_run,
                   "environment": self.settings.kalshi_env, "desk": "axe"}
        existing = self.db.get(order.request_id)
        if not existing:
            self.require_placement()
        existing = self.db.claim(order.request_id, payload, kind="perp_place", terms=self.terms(order),
                                 approval_id=str(order.approval_id) if order.approval_id else None,
                                 require_approval=self.settings.require_trade_approval, desk="axe")
        if existing:
            return await self.replay(existing)
        rid = str(order.request_id)
        self.inflight.add(rid)
        submitted = False
        reserve = 0
        try:
            started = monotonic()
            market = (await self.margin.market(order.ticker))["market"]
            self.db.audit("market_snapshot", request_id=rid, market_snapshot=market, phase="initial")
            positions = (await self.margin.positions(ticker=order.ticker)).get("positions") or []
            balance = await self.margin.balance()
            listed = await self.margin.orders(ticker=order.ticker, status="resting")
            open_orders = listed.get("orders") if isinstance(listed, dict) else None
            if not isinstance(positions, list) or not isinstance(open_orders, list):
                raise Rejection("invalid_margin_positions")
            reserve, count, price = self.validate(order, market, positions, balance, open_orders)
            limit = self.settings.axe_perp_max_daily_notional_cents
            if reserve > self.db.daily_budget(limit, desk="axe", kind="perp_place")["remaining_cents"]:
                raise Rejection("perp_daily_notional_limit")
            market = (await self.margin.market(order.ticker))["market"]
            self.db.audit("market_snapshot", request_id=rid, market_snapshot=market, phase="before_submission")
            self.validate(order, market, positions, balance, open_orders)
            if monotonic() - started > self.settings.snapshot_max_age_seconds:
                raise Rejection("account_snapshot_stale")
            if order.approval_id is not None:
                self.db.check_approval(order.approval_id)
            self.require_placement()
            body = self.body(order, market, count, price)
            if self.settings.dry_run:
                result = {"status": "simulated", "request_id": rid, "dry_run": True, "submitted": False,
                          "would_submit": body, "required_notional_cents": reserve, "validation": "passed"}
                self.db.finish(rid, result, reserve=0)
                return result
            self.db.submitting(rid, reserve, limit)
            submitted = True
            created = await self.margin.create_order(body)
            order_id = str(UUID(created["order_id"]))
            if created.get("client_order_id", rid) != rid:
                raise Rejection("exchange_client_order_id_mismatch")
            immediate = margin_order_state({**created, "order_id": order_id, "ticker": order.ticker,
                                            "side": order.side, "price": body["price"]})
            result = {**immediate, "order_id": order_id, "request_id": rid, "submitted": True,
                      "dry_run": False, "submission": created}
            self.db.finish(rid, result, reserve=reserve, keep_gate=True)
            try:
                found = (await self.margin.order(order_id))["order"]
                self._check_identity(self.db.get(rid), found)
                result = {**result, **margin_order_state(found)}
            except (APIError, Rejection, KeyError, ValueError):
                result["reconciliation_required"] = True
            self.db.finish(rid, result, keep_gate=result["status"] == "unknown")
            return result
        except Rejection as exc:
            ambiguous = submitted and (not isinstance(exc, APIError) or exc.ambiguous)
            result = {"status": "unknown" if ambiguous else "rejected", "request_id": rid,
                      "reason": exc.code, "submitted": None if ambiguous else False,
                      "submission_attempted": submitted, "reconciliation_required": ambiguous}
            if isinstance(exc, APIError):
                result["api_http_status"] = exc.status
            self.db.finish(rid, result, reserve=reserve if ambiguous else 0, keep_gate=ambiguous)
            return result
        except BaseException as exc:
            result = {"status": "unknown" if submitted else "rejected", "request_id": rid,
                      "reason": "submission_interrupted" if submitted else "preflight_failed",
                      "submitted": None if submitted else False, "reconciliation_required": submitted}
            self.db.finish(rid, result, reserve=reserve if submitted else 0, keep_gate=submitted)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result
        finally:
            self.inflight.discard(rid)

    def _check_identity(self, row, order):
        payload = json.loads(row["payload"])
        if order.get("client_order_id") != row["request_id"] or order.get("ticker") != payload["ticker"]:
            raise Rejection("exchange_order_identity_mismatch")

    async def replay(self, row):
        if row["request_id"] in self.inflight:
            return {"request_id": row["request_id"], "status": "unknown", "in_progress": True,
                    "reconciliation_required": True}
        if row["status"] in {"validating", "submitting", "unknown"}:
            return await self.reconcile(row)
        return {**json.loads(row["result"]), "replayed": True}

    async def reconcile(self, row):
        rid = row["request_id"]
        try:
            if row["order_id"]:
                order = (await self.margin.order(row["order_id"]))["order"]
            else:
                payload = json.loads(row["payload"])
                order = await self.find_client_order(payload["ticker"], rid)
                if order is None:
                    raise Rejection("unresolved_client_order_id")
            self._check_identity(row, order)
            result = {**margin_order_state(order), "request_id": rid, "reconciled": True,
                      "submitted": True, "replayed": True}
            self.db.finish(rid, result, keep_gate=result["status"] == "unknown")
            return result
        except (Rejection, KeyError, ValueError) as exc:
            return {"status": "unknown", "request_id": rid, "order_id": row["order_id"],
                    "reason": exc.code if isinstance(exc, Rejection) else "reconciliation_failed",
                    "reconciliation_required": True, "submitted": None}

    async def find_client_order(self, ticker, client_order_id):
        cursor, seen = None, set()
        for _ in range(20):
            page = await self.margin.orders(ticker=ticker, limit=200, **({"cursor": cursor} if cursor else {}))
            orders = page.get("orders")
            if not isinstance(orders, list):
                raise Rejection("invalid_api_response")
            for order in orders:
                if order.get("client_order_id") == client_order_id:
                    return order
            cursor = page.get("cursor")
            if not cursor or not isinstance(cursor, str) or cursor in seen:
                return None
            seen.add(cursor)
        return None

    async def cancel(self, args):
        local = self.db.for_order(str(args.order_id), kind="perp_place")
        if not local or local["desk"] != "axe":
            raise Rejection("order_not_placed_by_axe")
        payload = {**args.model_dump(mode="json"), "dry_run": self.settings.dry_run,
                   "environment": self.settings.kalshi_env, "desk": "axe"}
        existing = self.db.claim(args.request_id, payload, kind="cancel", desk="axe")
        if existing:
            return await self.replay(existing)
        rid, oid = str(args.request_id), str(args.order_id)
        self.inflight.add(rid)
        attempted = False
        try:
            order = (await self.margin.order(oid))["order"]
            self._check_identity(local, order)
            result = margin_order_state(order)
            if result["status"] in {"filled", "cancelled"}:
                result.update(request_id=rid, cancellation_submitted=False)
            elif self.settings.dry_run:
                result = {**result, "status": "simulated", "request_id": rid, "dry_run": True,
                          "cancellation_submitted": False, "would_cancel_order_id": oid}
            else:
                self.db.finish(rid, {"status": "unknown", "request_id": rid, "order_id": oid})
                attempted = True
                ack = await self.margin.cancel_order(oid)
                order = (await self.margin.order(oid))["order"]
                result = {**margin_order_state(order), "request_id": rid,
                          "cancellation_submitted": True, "cancellation_ack": ack}
                if result["status"] not in {"filled", "cancelled"}:
                    result.update(observed_status=result["status"], status="unknown", reconciliation_required=True)
            self.db.finish(rid, result)
            if result["status"] != "simulated":
                self.db.finish(local["request_id"], {**result, "request_id": local["request_id"]},
                               keep_gate=result["status"] == "unknown")
            return result
        except BaseException as exc:
            result = {"status": "unknown" if attempted else "rejected", "request_id": rid, "order_id": oid,
                      "reason": exc.code if isinstance(exc, Rejection) else "cancellation_failed",
                      "cancellation_submitted": None if attempted else False,
                      "reconciliation_required": attempted}
            self.db.finish(rid, result)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result
        finally:
            self.inflight.discard(rid)


def primary_subaccount(balance):
    rows = balance.get("subaccount_balances")
    if not isinstance(rows, list):
        raise Rejection("invalid_margin_balance")
    for row in rows:
        if row.get("subaccount") == 0:
            return row
    raise Rejection("missing_margin_balance")
