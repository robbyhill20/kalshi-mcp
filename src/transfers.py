"""Owner-only cash moves between Kalshi exchange indexes. Bots cannot call this."""
import json
from decimal import Decimal, ROUND_DOWN

from .errors import APIError

# A retry of an unresolved request posts again with the same Kalshi client_transfer_id.
_UNRESOLVED = {"validating", "submitting", "unknown"}
_inflight = set()


def primary_exchange_balances(payload):
    """Primary subaccount (0) cash on each exchange index Kalshi returned."""
    rows = payload.get("subaccount_balances") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise APIError("invalid_api_response")
    balances = []
    for row in rows:
        if not isinstance(row, dict):
            raise APIError("invalid_api_response")
        number = row.get("subaccount_number")
        index = row.get("exchange_index")
        text = row.get("balance")
        if isinstance(number, bool) or not isinstance(number, int):
            raise APIError("invalid_api_response")
        if number != 0:
            continue
        if isinstance(index, bool) or not isinstance(index, int) or not isinstance(text, str):
            raise APIError("invalid_api_response")
        balances.append({"exchange_index": index, "balance_cents": _cents(text), "balance_dollars": text})
    balances.sort(key=lambda item: item["exchange_index"])
    return {"balances": balances}


def _cents(text):
    try:
        amount = Decimal(text)
    except Exception:
        raise APIError("invalid_api_response") from None
    if not amount.is_finite() or amount < 0:
        raise APIError("invalid_api_response")
    # Whole cents, never rounded up. balance_dollars keeps Kalshi's exact figure.
    return int((amount * 100).to_integral_value(rounding=ROUND_DOWN))


def _terms(transfer):
    return {"request_id": str(transfer.request_id), "from_index": transfer.from_index,
            "to_index": transfer.to_index, "amount_cents": transfer.amount_cents}


def _audit(db, event, transfer, **fields):
    db.audit(event, **_terms(transfer), **fields)


def _body(transfer):
    # POST /portfolio/subaccounts/transfer moves numbered subaccounts on one shard.
    # Moving idx0 -> idx3 is POST /portfolio/intra_exchange_instance_transfer.
    # Its amount is centicents: 1 cent = 100 centicents.
    return {"source": "event_contract", "destination": "event_contract",
            "amount": transfer.amount_cents * 100, "client_transfer_id": str(transfer.request_id),
            "source_exchange_shard": transfer.from_index, "destination_exchange_shard": transfer.to_index,
            "source_subaccount": 0, "destination_subaccount": 0}


def _pending(transfer):
    return {**_terms(transfer), "status": "unknown", "in_progress": True, "submitted": None,
            "next_step": "This request id is already being sent. Retry the same request id to see the result."}


def _replay(row):
    if not row["result"]:
        return {"request_id": row["request_id"], "status": "unknown", "submitted": None, "replayed": True,
                "next_step": "Retry this same request id. A new request id can move the money again."}
    return {**json.loads(row["result"]), "replayed": True}


async def move_exchange_cash(settings, client, db, transfer):
    if transfer.amount_cents > settings.max_transfer_cents:
        _audit(db, "owner_exchange_transfer_rejected", transfer, reason="transfer_amount_limit",
               max_transfer_cents=settings.max_transfer_cents, submitted=False)
        return {**_terms(transfer), "status": "rejected", "reason": "transfer_amount_limit", "submitted": False,
                "max_transfer_cents": settings.max_transfer_cents,
                "next_step": "Choose a smaller amount. Nothing was sent to Kalshi."}
    rid = str(transfer.request_id)
    payload = {**_terms(transfer), "dry_run": settings.dry_run, "environment": settings.kalshi_env}
    existing = db.claim(rid, payload, kind="transfer", require_approval=False, desk="owner")
    if existing:
        if rid in _inflight:
            return _pending(transfer)
        if existing["status"] not in _UNRESOLVED:
            return _replay(existing)
    if settings.dry_run:
        # A live attempt that Kalshi has not confirmed must stay unknown. Never pretend it was a dry run.
        if existing and existing["status"] == "unknown":
            return _replay(existing)
        return _simulate(db, transfer)
    return await _submit(client, db, transfer, _body(transfer))


def _simulate(db, transfer):
    rid = str(transfer.request_id)
    if rid in _inflight:
        return _pending(transfer)
    result = {**_terms(transfer), "status": "simulated", "dry_run": True, "submitted": False,
              "amount_centicents": transfer.amount_cents * 100, "would_submit": _body(transfer),
              "next_step": "Dry run only. Nothing was sent to Kalshi."}
    _inflight.add(rid)
    try:
        db.finish(rid, result)
        _audit(db, "owner_exchange_transfer_simulated", transfer, dry_run=True, submitted=False)
        return result
    finally:
        _inflight.discard(rid)


async def _submit(client, db, transfer, body):
    rid = str(transfer.request_id)
    _inflight.add(rid)
    try:
        db.note_transfer_submitting(rid)
        _audit(db, "owner_exchange_transfer_requested", transfer, dry_run=False)
        try:
            created = await client.transfer_between_exchange_indexes(body)
        except APIError as exc:
            result = _from_error(transfer, exc)
            db.finish(rid, result)
            _audit(db, "owner_exchange_transfer_result", transfer, status=result["status"],
                   reason=exc.code, submitted=result["submitted"])
            return result
        result = _from_response(transfer, created)
        db.finish(rid, result)
        _audit(db, "owner_exchange_transfer_result", transfer, status=result["status"],
               submitted=result["submitted"], transfer_id=result.get("transfer_id"))
        return result
    finally:
        _inflight.discard(rid)


def _from_error(transfer, exc):
    base = {**_terms(transfer), "dry_run": False, "reason": exc.code}
    if exc.status is not None:
        base["api_http_status"] = exc.status
    if exc.status == 409:
        # Same client_transfer_id was already accepted. Kalshi creates no second transfer.
        return {**base, "status": "accepted", "already_accepted": True, "submitted": True,
                "created_another": False,
                "next_step": "Kalshi already accepted this request id and did not create another transfer. "
                             "Check pool balances."}
    if exc.ambiguous:
        return {**base, "status": "unknown", "submitted": None, "reconciliation_required": True,
                "next_step": "Kalshi did not confirm the transfer. Retry this same request id. "
                             "A new request id can move the money again."}
    return {**base, "status": "rejected", "submitted": False,
            "next_step": "Kalshi rejected the transfer. Check pool balances before using a new request id."}


def _from_response(transfer, created):
    transfer_id = created.get("transfer_id") if isinstance(created, dict) else None
    base = {**_terms(transfer), "dry_run": False, "amount_centicents": transfer.amount_cents * 100}
    if not isinstance(transfer_id, str) or not transfer_id or len(transfer_id) > 200:
        return {**base, "status": "unknown", "submitted": None, "reason": "invalid_api_response",
                "reconciliation_required": True,
                "next_step": "Kalshi did not confirm the transfer. Retry this same request id. "
                             "A new request id can move the money again."}
    return {**base, "status": "accepted", "submitted": True, "transfer_id": transfer_id,
            "next_step": "Kalshi accepted the transfer and processes it asynchronously. Check pool balances to confirm."}
