import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pydantic import SecretStr, ValidationError

from src.config import Settings
from src.kalshi_client import KalshiClient


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
