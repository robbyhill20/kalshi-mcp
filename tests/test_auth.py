import base64

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pydantic import SecretStr, ValidationError

from src.config import Settings
from src.kalshi_client import KalshiClient, MarginClient


@pytest.mark.parametrize("algorithm", ["rsa", "ed25519"])
async def test_current_authentication_signature(settings, algorithm):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if algorithm == "rsa" else ed25519.Ed25519PrivateKey.generate()
    settings.kalshi_private_key = SecretStr(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    client = KalshiClient(settings)
    headers = client.auth_headers("get", "/trade-api/v2/portfolio/orders?limit=5", 1700000000000)
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    message = b"1700000000000GET/trade-api/v2/portfolio/orders"
    if algorithm == "rsa":
        key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        key.public_key().verify(signature, message)
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    await client.close()


def _verify(key, signature, message):
    if isinstance(key, rsa.RSAPrivateKey):
        key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        key.public_key().verify(signature, message)


@pytest.mark.parametrize("algorithm", ["rsa", "ed25519"])
async def test_signed_message_excludes_query_and_key_id_is_stripped(settings, algorithm):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if algorithm == "rsa" else ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    settings.kalshi_api_key_id = SecretStr(" event-key\n")
    settings.kalshi_private_key = SecretStr(pem)
    settings.kalshi_margin_api_key_id = SecretStr("\nmargin-key\n")
    settings.kalshi_margin_private_key = SecretStr(pem)
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"order_id": "00000000-0000-0000-0000-000000000001",
                                         "orders": [], "positions": [], "market_positions": [],
                                         "subaccount_balances": [], "settled_funds": "0", "cursor": ""})

    transport = httpx.MockTransport(handler)
    event = KalshiClient(settings, transport)
    margin = MarginClient(settings, transport)
    await event.orders(status="resting", cursor="abc/def")
    await event.cancel_order("oid", "TICKER")
    await margin.positions()
    await margin.balance()
    await margin.orders(ticker="KXTSLAPERP1", status="resting")
    await margin.create_order({"ticker": "KXTSLAPERP1", "client_order_id": "x"})
    await margin.cancel_order("00000000-0000-0000-0000-000000000001")
    await event.close()
    await margin.close()

    expected = [
        ("GET", "/trade-api/v2/portfolio/orders", "event-key",
         {"subaccount": "0", "status": "resting", "cursor": "abc/def"}),
        ("DELETE", "/trade-api/v2/portfolio/events/orders/oid", "event-key",
         {"market_ticker": "TICKER", "exchange_index": "-1", "subaccount": "0"}),
        ("GET", "/trade-api/v2/margin/positions", "margin-key", {}),
        ("GET", "/trade-api/v2/margin/balance", "margin-key",
         {"compute_available_balance": "true"}),
        ("GET", "/trade-api/v2/margin/orders", "margin-key",
         {"ticker": "KXTSLAPERP1", "status": "resting"}),
        ("POST", "/trade-api/v2/margin/orders", "margin-key", {}),
        ("DELETE", "/trade-api/v2/margin/orders/00000000-0000-0000-0000-000000000001", "margin-key", {}),
    ]
    assert len(seen) == len(expected)
    for request, (method, path, key_id, query) in zip(seen, expected):
        assert request.method == method
        assert request.url.scheme == "https"
        assert request.url.host == "external-api.kalshi.com"
        assert request.url.path == path
        assert dict(request.url.params) == query
        timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"])
        _verify(key, signature, f"{timestamp}{method}{path}".encode())
        if query:
            with pytest.raises(InvalidSignature):
                _verify(key, signature, f"{timestamp}{method}{request.url.raw_path.decode()}".encode())
        assert request.headers["KALSHI-ACCESS-KEY"] == key_id


async def test_authentication_failure(rig):
    rig.remote.fail[("GET", "/portfolio/balance")] = 401
    result = await rig.tools.call("kalshi_get_balance", {}, ["read"])
    assert result["reason"] == "authentication_failed"


async def test_timeout(rig):
    rig.remote.fail[("GET", "/portfolio/balance")] = httpx.ReadTimeout("private headers must not leak")
    result = await rig.tools.call("kalshi_get_balance", {}, ["read"])
    assert result["reason"] == "api_timeout"


def test_credentials_not_in_repr(settings):
    assert "test-key-id" not in repr(settings)
    assert settings.kalshi_private_key.get_secret_value() not in repr(settings)


def test_different_authentication_roles_required(settings):
    values = settings.model_dump()
    values["admin_token"] = values["mcp_read_token"]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_insecure_public_url_rejected(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), "public_base_url": "http://example.com"})


def test_api_hosts_are_fixed(settings):
    assert settings.api_origin == "https://external-api.kalshi.com"
    settings.kalshi_env = "demo"
    assert settings.api_origin == "https://external-api.demo.kalshi.co"
