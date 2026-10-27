import httpx
import pytest
from pydantic import TypeAdapter

from src.errors import Rejection
from src.models import PlaceOrderInput
from src.validation import estimate_fee_reserve


def terms(rig, **changes):
    return TypeAdapter(PlaceOrderInput).validate_python(rig.trade(**changes))


@pytest.mark.parametrize("taker,price,count,fee", [(False, 1, 1, 1), (False, 47, 10, 5),
    (True, 47, 10, 18), (True, 99, 100, 175), (True, 1, 100, 7)])
async def test_price_and_liquidity_fee_calculation(rig, taker, price, count, fee):
    value = estimate_fee_reserve(terms(rig, allow_taker=taker, limit_price_cents=price, contracts=count),
                                 rig.remote.event, rig.remote.series)
    assert value["estimated_fee_cents"] == fee
    assert value["fee_reserve_cents"] == fee + 1


async def test_one_cent_dry_run_requires_three_cents(rig):
    result = await rig.place(rig.trade(contracts=1, limit_price_cents=1))
    assert result["status"] == "simulated" and result["required_balance_cents"] == 3
    assert result["fee_calculation"]["extra_owner_buffer_cents"] == 0
    assert not rig.remote.posts


@pytest.mark.parametrize("fee_type,multiplier,expected", [("quadratic", 1, 0),
    ("quadratic_with_maker_fees", 0, 0), ("quadratic_with_maker_fees", 0.5, 3),
    ("quadratic_with_combo_maker_fees", 1, 9)])
async def test_event_override_precedes_series(rig, fee_type, multiplier, expected):
    rig.remote.event.update(fee_type_override=fee_type, fee_multiplier_override=multiplier)
    result = await rig.place()
    assert result["fee_calculation"]["estimated_fee_cents"] == expected


@pytest.mark.parametrize("metadata", [{"fee_type": "flat"}, {"fee_type": "future"},
    {"fee_multiplier": -1}, {"fee_multiplier": "NaN"}, {"fee_multiplier": None}])
async def test_unsupported_or_invalid_metadata_blocks_submission(rig, metadata):
    rig.live()
    rig.remote.series.update(metadata)
    result = await rig.place()
    assert result["status"] == "rejected" and result["submitted"] is False
    assert not rig.remote.posts


async def test_fee_api_timeout_is_definite_preflight_rejection(rig):
    rig.live()
    rig.remote.fail[("GET", "/series/TEST")] = httpx.ReadTimeout("timeout")
    result = await rig.place()
    assert result["reason"] == "api_timeout" and result["submitted"] is False
    assert not rig.remote.posts


async def test_partial_override_rejected(rig):
    rig.remote.event["fee_type_override"] = "quadratic"
    with pytest.raises(Rejection, match="incomplete_event_fee_override"):
        estimate_fee_reserve(terms(rig), rig.remote.event, rig.remote.series)


async def test_sell_taker_reserves_price_improvement_peak(rig):
    value = estimate_fee_reserve(terms(rig, action="sell", limit_price_cents=1, allow_taker=True),
                                 rig.remote.event, rig.remote.series)
    assert value["estimated_fee_cents"] == 18


async def test_explicit_owner_cushion_is_preserved(rig):
    rig.settings.fee_buffer_cents_per_contract = 2
    assert (await rig.place())["required_balance_cents"] == 496


async def test_fee_metadata_identity_mismatch_rejected(rig):
    rig.remote.series["ticker"] = "OTHER"
    assert (await rig.place())["reason"] == "fee_series_ticker_mismatch"
    assert not rig.remote.posts


async def test_upstream_identifier_cannot_inject_endpoint(rig):
    rig.remote.market["event_ticker"] = "../portfolio/balance"
    assert (await rig.place())["status"] == "rejected"
    assert not any(key[1].startswith("/events/") for key, _ in rig.remote.calls)


async def test_fee_data_refetched_for_each_new_request(rig):
    first = await rig.place()
    rig.remote.series["fee_multiplier"] = 2
    second = await rig.place()
    assert first["required_balance_cents"] == 476
    assert second["required_balance_cents"] == 480
    assert sum(key == ("GET", "/series/TEST") for key, _ in rig.remote.calls) == 2
