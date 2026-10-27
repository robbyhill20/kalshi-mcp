import asyncio
import json
from time import monotonic
from uuid import UUID

from .errors import APIError, Rejection
from .models import PlaceOrderInput, Ticker
from .validation import (available_cents, book_direction, normalize_order,
                         validate_account, validate_exchange, validate_market, yes_price)
from pydantic import TypeAdapter

TERMINAL = {"filled", "cancelled", "expired", "rejected"}


class Orders:
    def __init__(self, settings, client, database):
        self.settings, self.client, self.db = settings, client, database
        self.inflight = set()

    def terms(self, trade):
        return {**trade.model_dump(mode="json", exclude={"approval_id"}),
                "environment": self.settings.kalshi_env, "dry_run": self.settings.dry_run}

    def require_placement(self):
        if self.db.halted():
            raise Rejection("owner_kill_switch_active")
        # Dry-run simulations remain usable with the safe default TRADING_ENABLED=false.
        if not self.settings.dry_run and not self.settings.trading_enabled:
            raise Rejection("trading_disabled")

    def body(self, trade):
        return {"ticker": trade.ticker, "client_order_id": str(trade.request_id),
                "side": book_direction(trade), "count": f"{trade.contracts}.00",
                "price": f"{yes_price(trade):.4f}", "time_in_force": "good_till_canceled",
                "post_only": not trade.allow_taker, "cancel_order_on_pause": True,
                "self_trade_prevention_type": "taker_at_cross", "subaccount": 0,
                "exchange_index": -1}

    async def place(self, trade: PlaceOrderInput):
        payload = {**trade.model_dump(mode="json"), "dry_run": self.settings.dry_run,
                   "environment": self.settings.kalshi_env}
        existing = self.db.get(trade.request_id)
        if not existing:
            self.require_placement()
        existing = self.db.claim(trade.request_id, payload, terms=self.terms(trade),
                                  approval_id=str(trade.approval_id) if trade.approval_id else None,
                                  require_approval=self.settings.require_trade_approval)
        if existing:
            return await self.replay(existing)
        rid = str(trade.request_id)
        self.inflight.add(rid)
        submitted = False
        reserve = 0
        try:
            started = monotonic()
            market = (await self.client.market(trade.ticker))["market"]
            self.db.audit("market_snapshot", request_id=rid, market_snapshot=market, phase="initial")
            validate_market(market, trade)
            exchange = await self.client.exchange()
            index = validate_exchange(exchange, market)
            # Fetch every page: a first-page-only duplicate or holdings check is unsafe.
            positions = await self.client.all_pages("positions")
            orders = await self.client.all_pages("orders", status="resting")
            balance = await self.client.balance(exchange_index=index)
            other_reserve = await self.outstanding_reserve(balance)
            self.db.audit("account_snapshot", request_id=rid,
                          account_snapshot={"balance": balance, "positions": positions, "open_orders": orders})
            for row in self.db.reservations():
                prior = json.loads(row["payload"])
                if trade.action == "sell" and prior.get("ticker") == trade.ticker:
                    # Avoid selling holdings committed by a local order not yet in REST projections.
                    raise Rejection("unreconciled_local_commitment_blocks_sell")
                if row["status"] not in TERMINAL:
                    prior_trade = PlaceOrderInput.model_validate({k: v for k, v in prior.items()
                                                                 if k not in {"environment", "dry_run"}})
                    if (prior_trade.ticker == trade.ticker and book_direction(prior_trade) == book_direction(trade)
                            and yes_price(prior_trade) == yes_price(trade)):
                        raise Rejection("duplicate_existing_local_order")
            reserve = validate_account(trade, positions, orders, self.settings)
            self.db.audit("preflight_snapshot", request_id=rid, calling_tool="kalshi_place_order",
                          inputs=trade.model_dump(mode="json"), market_snapshot=market,
                          account_snapshot={"balance": balance, "positions": positions, "open_orders": orders},
                          validation_results={"required_cents": reserve, "local_reservations_cents": other_reserve})
            if reserve > available_cents(balance) - other_reserve:
                raise Rejection("insufficient_available_balance")
            if reserve > self.db.daily_budget(self.settings.max_daily_order_cost_cents)["remaining_cents"]:
                raise Rejection("owner_daily_order_cost_limit")
            # Fetch again immediately before submission; never act on a cached market.
            market = (await self.client.market(trade.ticker))["market"]
            self.db.audit("market_snapshot", request_id=rid, market_snapshot=market, phase="before_submission")
            validate_market(market, trade)
            if market.get("exchange_index") != index:
                raise Rejection("market_exchange_index_changed")
            if monotonic() - started > self.settings.snapshot_max_age_seconds:
                raise Rejection("account_snapshot_stale")
            if trade.approval_id is not None:
                self.db.check_approval(trade.approval_id)
            self.require_placement()
            body = self.body(trade)
            if self.settings.dry_run:
                result = {"status": "simulated", "request_id": rid,
                          "approval_id": str(trade.approval_id) if trade.approval_id else None,
                          "approval_required": self.settings.require_trade_approval,
                          "dry_run": True, "submitted": False, "would_submit": body,
                          "required_balance_cents": reserve, "available_balance_cents": available_cents(balance),
                          "market_snapshot": market, "validation": "passed", "fill_status": "not_simulated"}
                self.db.finish(rid, result, reserve=0)
                return result
            # Commit durable intent BEFORE calling Kalshi; any interruption after here is ambiguous.
            self.db.submitting(rid, reserve, self.settings.max_daily_order_cost_cents)
            submitted = True
            created = await self.client.create_order(body)
            order_id = str(UUID(created["order_id"]))
            if created.get("client_order_id", rid) != rid:
                raise Rejection("exchange_client_order_id_mismatch")
            immediate = normalize_order({**created, "status": "submitted"})
            result = {**immediate, "order_id": order_id, "request_id": rid,
                      "approval_id": str(trade.approval_id) if trade.approval_id else None, "submitted": True,
                      "approval_required": self.settings.require_trade_approval,
                      "dry_run": False, "submission": created}
            # Persist the ID before any follow-up HTTP request.
            self.db.finish(rid, result, reserve=reserve, keep_gate=True)
            try:
                order = (await self.client.order(order_id))["order"]
                self.check_local_identity(self.db.get(rid), order)
                result = {**result, **self.checked_order(order, order_id)}
            except (APIError, Rejection, KeyError, ValueError):
                result["reconciliation_required"] = True
            self.db.finish(rid, result, keep_gate=result["status"] == "unknown")
            return result
        except Rejection as exc:
            ambiguous = submitted and (not isinstance(exc, APIError) or exc.ambiguous)
            result = {"status": "unknown" if ambiguous else "rejected", "request_id": rid,
                      "reason": exc.code, "submitted": None if ambiguous else False,
                      "submission_attempted": submitted,
                      "reconciliation_required": ambiguous}
            if isinstance(exc, APIError):
                result["api_http_status"] = exc.status
            self.db.finish(rid, result, reserve=reserve if ambiguous else 0, keep_gate=ambiguous)
            return result
        except BaseException as exc:
            # Cancellation during POST is not evidence of rejection. Do not log exception text.
            result = {"status": "unknown" if submitted else "rejected", "request_id": rid,
                      "reason": "submission_interrupted" if submitted else "preflight_failed",
                      "submitted": None if submitted else False, "reconciliation_required": submitted}
            self.db.finish(rid, result, reserve=reserve if submitted else 0, keep_gate=submitted)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result
        finally:
            self.inflight.discard(rid)

    async def outstanding_reserve(self, balance):
        total = 0
        for row in self.db.reservations():
            # A terminal order can still precede the account API's latest reflected mutation.
            result = json.loads(row["result"]) if row["result"] else {}
            updated = balance.get("updated_ts")
            if result.get("status") in TERMINAL and isinstance(updated, int) and (
                row["terminal_observed_at"] is not None and updated >= row["terminal_observed_at"] + 2
            ):
                self.db.clear_reserve(row["request_id"])
            else:
                total += row["reserve_cents"]
        return total

    def checked_order(self, order, order_id):
        order_id = str(UUID(str(order_id)))
        if order.get("order_id") != str(order_id) or order.get("subaccount_number", 0) not in {None, 0}:
            raise Rejection("order_identity_or_account_mismatch")
        return normalize_order(order)

    def check_local_identity(self, row, order):
        if row["kind"] == "place":
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
        if rid in self.inflight:
            return {"request_id": rid, "status": "unknown", "in_progress": True}
        try:
            if row["order_id"]:
                order = (await self.client.order(row["order_id"]))["order"]
            elif row["kind"] == "place":
                payload = json.loads(row["payload"])
                matches = [o for o in await self.client.all_pages("orders", ticker=payload["ticker"])
                           if o.get("client_order_id") == rid]
                if len(matches) != 1:
                    raise Rejection("unresolved_client_order_id")
                order = matches[0]
            else:
                payload = json.loads(row["payload"])
                order = (await self.client.order(payload["order_id"]))["order"]
            self.check_local_identity(row, order)
            result = {**self.checked_order(order, order["order_id"]), "request_id": rid,
                      "reconciled": True, "submitted": True, "replayed": True}
            # An unconfirmed cancellation must remain unknown while the order is still active.
            if row["kind"] == "cancel" and result["status"] not in TERMINAL:
                result = {**result, "observed_status": result["status"], "status": "unknown",
                          "reconciliation_required": True}
            self.db.finish(rid, result, keep_gate=result["status"] == "unknown")
            return result
        except (Rejection, KeyError, ValueError) as exc:
            return {"status": "unknown", "request_id": rid, "order_id": row["order_id"],
                    "reason": exc.code if isinstance(exc, Rejection) else "reconciliation_failed",
                    "reconciliation_required": True, "submitted": None}

    async def get_order(self, args):
        if args.request_id:
            row = self.db.get(args.request_id)
            if not row:
                raise Rejection("local_request_not_found")
            if args.order_id and row["order_id"] != str(args.order_id):
                raise Rejection("order_id_request_id_mismatch")
            if row["status"] in {"simulated", "rejected"}:
                return json.loads(row["result"])
            return await self.reconcile(row)
        if not args.order_id:
            raise Rejection("order_id_or_request_id_required")
        order = (await self.client.order(str(args.order_id)))["order"]
        result = self.checked_order(order, str(args.order_id))
        self.db.observe(args.order_id, result)
        row = self.db.for_order(args.order_id)
        if row:
            self.check_local_identity(row, order)
            self.db.finish(row["request_id"], {**result, "request_id": row["request_id"]},
                           keep_gate=result["status"] == "unknown")
        return result

    async def cancel(self, args):
        payload = {**args.model_dump(mode="json"), "dry_run": self.settings.dry_run,
                   "environment": self.settings.kalshi_env}
        existing = self.db.claim(args.request_id, payload, kind="cancel")
        if existing:
            return await self.replay(existing)
        rid, oid = str(args.request_id), str(args.order_id)
        self.inflight.add(rid)
        attempted = False
        try:
            order = (await self.client.order(oid))["order"]
            result = self.checked_order(order, oid)
            ticker = TypeAdapter(Ticker).validate_python(order["ticker"])
            if result["status"] in TERMINAL:
                result.update(request_id=rid, cancellation_submitted=False)
            elif self.settings.dry_run:
                result = {**result, "status": "simulated", "request_id": rid, "dry_run": True,
                          "cancellation_submitted": False, "would_cancel_order_id": oid}
            else:
                # Cancellation is permitted even with trading disabled or the owner halt active.
                # Unlike placement, no balance/market-open check blocks risk-reducing cancellation.
                self.db.finish(rid, {"status": "unknown", "request_id": rid, "order_id": oid})
                attempted = True
                ack = await self.client.cancel_order(oid, ticker)
                order = (await self.client.order(oid))["order"]
                result = {**self.checked_order(order, oid), "request_id": rid,
                          "cancellation_submitted": True, "cancellation_ack": ack}
                if result["status"] not in TERMINAL:
                    result.update(observed_status=result["status"], status="unknown", reconciliation_required=True)
            self.db.finish(rid, result)
            local = self.db.for_order(oid)
            if local and result["status"] != "simulated":
                self.db.finish(local["request_id"], {**result, "request_id": local["request_id"]},
                               keep_gate=result["status"] == "unknown")
            return result
        except BaseException as exc:
            result = {"status": "unknown" if attempted else "rejected", "request_id": rid,
                      "order_id": oid, "reason": exc.code if isinstance(exc, Rejection) else "cancellation_failed",
                      "cancellation_submitted": None if attempted else False,
                      "reconciliation_required": attempted}
            self.db.finish(rid, result)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return result
        finally:
            self.inflight.discard(rid)
