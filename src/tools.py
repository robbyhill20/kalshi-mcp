from uuid import uuid4

from pydantic import ValidationError

from .errors import APIError, Rejection
from .models import PERP_READ, PERP_WRITE, TOOL_MODELS, WRITE_TOOLS
from .perps import PerpOrders


class Tools:
    def __init__(self, settings, client, db, orders, margin=None):
        self.settings, self.client, self.db, self.orders = settings, client, db, orders
        self.margin = margin
        self.perps = PerpOrders(settings, margin, db) if margin is not None else None

    def _deny(self, name, audit_id, reason):
        self.db.audit("access_denied", calling_tool=name, request_id=audit_id)
        return {"status": "rejected", "reason": reason}

    async def call(self, name, arguments, scopes):
        audit_id = str(uuid4())
        model = TOOL_MODELS.get(name)
        if not model:
            return {"status": "rejected", "reason": "unknown_tool"}
        if "read" not in scopes:
            return self._deny(name, audit_id, "director_permission_required")
        if name in PERP_READ or name in PERP_WRITE:
            if not self.settings.perps_enabled:
                return self._deny(name, audit_id, "perps_disabled")
            if self.perps is None:
                return self._deny(name, audit_id, "perps_not_configured")
            if name in PERP_WRITE and ("axe" not in scopes or "trade" in scopes):
                return self._deny(name, audit_id, "axe_permission_required")
        if name in WRITE_TOOLS and "trade" not in scopes and "axe" not in scopes:
            return self._deny(name, audit_id, "director_permission_required")
        desk = "axe" if "axe" in scopes and "trade" not in scopes else "sports"
        try:
            args = model.model_validate(arguments)
        except ValidationError as exc:
            # Locations/types only. Never echo untrusted input or exceptions into logs.
            errors = [{"field": ".".join(str(v) for v in e["loc"]), "type": e["type"]}
                      for e in exc.errors(include_input=False, include_url=False, include_context=False)]
            self.db.audit("invalid_arguments", calling_tool=name, request_id=audit_id, validation_results=errors)
            return {"status": "rejected", "reason": "invalid_arguments", "errors": errors}
        inputs = args.model_dump(mode="json", exclude_none=True)
        audit_id = inputs.get("request_id") or audit_id
        self.db.audit("tool_call", calling_tool=name, request_id=audit_id, inputs=inputs)
        try:
            params = args.model_dump(exclude_none=True)
            if name == "kalshi_get_balance":
                result = await self.client.balance()
            elif name == "kalshi_get_positions":
                result = await self.client.positions(**params)
            elif name == "kalshi_get_market":
                result = await self.client.market(**params)
            elif name == "kalshi_get_markets":
                result = await self.client.markets(**params)
            elif name == "kalshi_get_orderbook":
                result = await self.client.orderbook(**params)
            elif name == "kalshi_get_open_orders":
                result = await self.open_orders(args, desk)
            elif name == "kalshi_get_order":
                result = await self.orders.get_order(args, desk)
            elif name == "kalshi_place_order":
                result = await self.orders.place(args, desk)
            elif name == "kalshi_cancel_order":
                result = await self.orders.cancel(args, desk)
            elif name == "kalshi_get_margin_markets":
                result = await self.margin.markets(**params)
            elif name == "kalshi_get_margin_orderbook":
                result = await self.margin.orderbook(**params)
            elif name == "kalshi_get_margin_positions":
                result = await self.margin.positions()
            elif name == "kalshi_get_margin_balance":
                result = await self.margin.balance()
            elif name == "kalshi_place_perp_order":
                result = await self.perps.place(args)
            elif name == "kalshi_cancel_perp_order":
                result = await self.perps.cancel(args)
            else:
                return {"status": "rejected", "reason": "unknown_tool"}
            self.db.audit("tool_result", calling_tool=name, request_id=audit_id, result=result)
            return result
        except Rejection as exc:
            result = {"status": "rejected", "reason": exc.code}
            if isinstance(exc, APIError):
                result["api_http_status"] = exc.status
            self.db.audit("tool_error", calling_tool=name, request_id=audit_id, result=result)
            return result
        except Exception:
            # An unexpected local error must not disclose keys or request headers.
            self.db.audit("tool_error", calling_tool=name, request_id=audit_id, reason="internal_error")
            return {"status": "unknown", "reason": "internal_error",
                    "reconciliation_required": name in WRITE_TOOLS or name in PERP_WRITE}

    async def open_orders(self, args, desk):
        params = args.model_dump(exclude_none=True)
        if desk != "axe":
            return await self.client.orders(status="resting", **params)
        owned = self.db.request_ids_for("axe", "place")
        rows = [row for row in await self.client.all_pages("orders", status="resting")
                if row.get("client_order_id") in owned]
        return {"orders": rows, "cursor": ""}
