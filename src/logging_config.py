import json
import logging
from datetime import datetime, timezone

SENSITIVE = {"private_key", "kalshi_private_key", "kalshi_api_key_id", "authorization",
             "signature", "token", "access_token", "refresh_token", "client_secret",
             "admin_token", "mcp_read_token", "mcp_director_token"}


def sanitize(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in SENSITIVE or k.lower().startswith("kalshi-access-")
                else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and ("PRIVATE KEY" in value or value.startswith("Bearer ")):
        return "[REDACTED]"
    return value


def configure_logging():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("kalshi.audit").setLevel(logging.INFO)
    # HTTP access logs include query strings (OAuth codes); disable at server launch too.
    for name in ("httpx", "httpcore", "uvicorn.access", "mcp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def audit_event(event, **fields):
    record = sanitize({"timestamp": datetime.now(timezone.utc).isoformat(),
                       "event": event, **fields})
    logging.getLogger("kalshi.audit").info(json.dumps(record, separators=(",", ":")))
    return record
