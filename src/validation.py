from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from .errors import Rejection


def decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return number
    except (ValueError, InvalidOperation):
        raise Rejection("invalid_exchange_numeric_field") from None


def book_direction(trade):
    return "bid" if (trade.action == "buy") == (trade.side == "yes") else "ask"


def yes_price(trade):
    price = Decimal(trade.limit_price_cents) / 100
    return price if trade.side == "yes" else 1 - price


def validate_market(market, trade, now=None):
    now = now or datetime.now(timezone.utc)
    if market.get("ticker") != trade.ticker:
        raise Rejection("market_ticker_mismatch")
    if market.get("status") != "active":
        raise Rejection("market_not_open")
    if market.get("market_type") != "binary" or decimal(market.get("notional_value_dollars")) != 1:
        raise Rejection("only_one_dollar_binary_markets_supported")
    try:
        opened = datetime.fromisoformat(market["open_time"].replace("Z", "+00:00"))
        closed = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        if not opened <= now < closed:
            raise Rejection("market_not_open")
    except (KeyError, ValueError, TypeError):
        raise Rejection("invalid_market_times") from None
    ranges = market.get("price_ranges")
    if not isinstance(ranges, list) or not ranges:
        raise Rejection("missing_market_price_grid")
    price = yes_price(trade)
    try:
        for band in ranges:
            start, end, step = (decimal(band[k]) for k in ("start", "end", "step"))
            if step > 0 and start <= price <= end and (price - start) % step == 0:
                return
    except (KeyError, TypeError):
        raise Rejection("invalid_market_price_grid") from None
    raise Rejection("price_not_on_market_grid")


def validate_exchange(exchange, market):
    index = market.get("exchange_index")
    if type(index) is not int or index < 0:
        raise Rejection("missing_market_exchange_index")
    statuses = exchange.get("exchange_index_statuses")
    state = next((s for s in statuses if s.get("exchange_index") == index), None) if isinstance(statuses, list) else None
    if state is None and index == 0:
        state = exchange
    if not state or state.get("exchange_active") is not True or state.get("trading_active") is not True:
        raise Rejection("exchange_trading_paused_or_unknown")
    return index


def estimate_fee_reserve(trade, event, series):
    fee_type = event.get("fee_type_override")
    multiplier = event.get("fee_multiplier_override")
    if (fee_type is None) != (multiplier is None):
        raise Rejection("incomplete_event_fee_override")
    if fee_type is None:
        fee_type, multiplier = series.get("fee_type"), series.get("fee_multiplier")
    maker_rates = {"quadratic": Decimal(0), "quadratic_with_maker_fees": Decimal("0.0175"),
                   "quadratic_with_combo_maker_fees": Decimal("0.035")}
    if fee_type not in maker_rates:
        raise Rejection("unsupported_fee_model")
    multiplier = decimal(multiplier)
    if not 0 <= multiplier <= 1000 or isinstance(event.get("fee_multiplier_override", series.get("fee_multiplier")), bool):
        raise Rejection("invalid_fee_multiplier")
    rate = Decimal("0.07") if trade.allow_taker else maker_rates[fee_type]
    price = Decimal(trade.limit_price_cents) / 100
    # Taking can execute at a better price. Reserve the maximum fee over the
    # permitted execution-price interval, whose quadratic peak is at 50 cents.
    if trade.allow_taker:
        price = min(price, Decimal("0.5")) if trade.action == "buy" else max(price, Decimal("0.5"))
    model = rate * multiplier * trade.contracts * price * (1 - price)
    model = model.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    fee_cents = int((model * 100).to_integral_value(rounding=ROUND_CEILING))
    # One extra cent covers temporary rounding before accumulator rebates.
    return {"fee_type": fee_type, "fee_multiplier": str(multiplier),
            "liquidity_assumption": "taker_upper_bound" if trade.allow_taker else "maker_only",
            "estimated_fee_cents": fee_cents, "rounding_cushion_cents": 1,
            "fee_reserve_cents": fee_cents + 1}


def validate_account(trade, positions, orders, settings, fee_reserve_cents):
    if trade.contracts > settings.max_contracts:
        raise Rejection("owner_contract_limit")
    # Reserve gross collateral even when an order might reduce other exposure.
    cost = trade.contracts * (trade.limit_price_cents if trade.action == "buy" else 100 - trade.limit_price_cents)
    if cost > settings.max_order_cost_cents:
        raise Rejection("owner_order_cost_limit")
    for order in orders:
        if order.get("ticker") != trade.ticker:
            continue
        direction = order.get("book_side")
        if direction not in {"bid", "ask"}:
            raise Rejection("unrecognized_existing_order_direction")
        if direction == book_direction(trade) and decimal(order.get("yes_price_dollars")) == yes_price(trade):
            raise Rejection("duplicate_existing_order")
    if trade.action == "sell":
        position = sum((decimal(p.get("position_fp")) for p in positions if p.get("ticker") == trade.ticker), Decimal(0))
        owned = max(Decimal(0), position if trade.side == "yes" else -position)
        reducing_direction = "ask" if trade.side == "yes" else "bid"
        reserved = sum((decimal(o.get("remaining_count_fp")) for o in orders
                        if o.get("ticker") == trade.ticker and o.get("book_side") == reducing_direction), Decimal(0))
        if trade.contracts > owned - reserved:
            raise Rejection("insufficient_uncommitted_contracts_to_sell")
    return cost + fee_reserve_cents + trade.contracts * settings.fee_buffer_cents_per_contract


def available_cents(balance):
    if "balance_dollars" in balance:
        return int(decimal(balance["balance_dollars"]) * 100)
    if type(balance.get("balance")) is not int:
        raise Rejection("invalid_balance")
    return balance["balance"]


def normalize_order(order):
    status = order.get("status")
    fill = decimal(order.get("fill_count_fp", order.get("fill_count", "0")))
    remaining = decimal(order.get("remaining_count_fp", order.get("remaining_count", "0")))
    if fill < 0 or remaining < 0:
        raise Rejection("invalid_exchange_order_counts")
    if status in {"canceled", "cancelled"}:
        state = "cancelled"
    elif status == "expired":
        state = "expired"
    elif status == "rejected":
        state = "rejected"
    elif status == "executed" and remaining == 0 and fill > 0:
        state = "filled"
    elif remaining > 0 and fill > 0:
        state = "partially_filled"
    elif status == "resting" and remaining > 0:
        state = "resting"
    elif status in {"pending", "submitted"}:
        state = "submitted"
    else:
        state = "unknown"
    return {"status": state, "order_id": order.get("order_id"),
            "filled_contracts": str(fill), "remaining_contracts": str(remaining),
            "exchange_status": status, "order": order}
