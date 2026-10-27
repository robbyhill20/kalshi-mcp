from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Ticker = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"^[A-Z0-9][A-Z0-9_.-]*$")]
Cursor = Annotated[str, Field(strict=True, max_length=2048, pattern=r"^[A-Za-z0-9_+=/.-]*$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Empty(Input):
    pass


class MarketInput(Input):
    ticker: Ticker


class MarketsInput(Input):
    status: Literal["unopened", "open", "paused", "closed", "settled"] | None = None
    event_ticker: Ticker | None = None
    series_ticker: Ticker | None = None
    limit: int = Field(default=100, strict=True, ge=1, le=200)
    cursor: Cursor | None = None


class PositionsInput(Input):
    ticker: Ticker | None = None
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    cursor: Cursor | None = None


class OpenOrdersInput(Input):
    ticker: Ticker | None = None
    limit: int = Field(default=100, strict=True, ge=1, le=200)
    cursor: Cursor | None = None


class OrderbookInput(MarketInput):
    depth: int = Field(default=20, strict=True, ge=1, le=100)


class UUIDInput(Input):
    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def uuid_strings_only(cls, value, info):
        if value is not None and info.field_name.endswith("_id") and not isinstance(value, (str, UUID)):
            raise ValueError("UUID string required")
        return value


class GetOrderInput(UUIDInput):
    # Supply order_id for exchange lookup, request_id for local reconciliation, or both.
    order_id: UUID | None = None
    request_id: UUID | None = None


class TradeTerms(Input):
    request_id: UUID
    ticker: Ticker
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    contracts: int = Field(strict=True, ge=1)
    # Buy: maximum cost per contract; sell: minimum proceeds per contract.
    limit_price_cents: int = Field(strict=True, ge=1, le=99)
    allow_taker: bool = Field(default=False, strict=True)


class PlaceOrderInput(TradeTerms):
    approval_id: UUID | None = None


class CancelOrderInput(UUIDInput):
    request_id: UUID
    order_id: UUID


class ApprovalInput(TradeTerms):
    pass


class KillInput(Input):
    halted: bool = Field(strict=True)


TOOL_MODELS = {
    "kalshi_get_balance": Empty,
    "kalshi_get_positions": PositionsInput,
    "kalshi_get_market": MarketInput,
    "kalshi_get_markets": MarketsInput,
    "kalshi_get_orderbook": OrderbookInput,
    "kalshi_get_open_orders": OpenOrdersInput,
    "kalshi_get_order": GetOrderInput,
    "kalshi_place_order": PlaceOrderInput,
    "kalshi_cancel_order": CancelOrderInput,
}
WRITE_TOOLS = {"kalshi_place_order", "kalshi_cancel_order"}
