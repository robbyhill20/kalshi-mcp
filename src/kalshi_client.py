import base64
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .errors import APIError
from .logging_config import sanitize

API_ROOT = "/trade-api/v2"


class KalshiClient:
    """Fixed endpoint methods only. No public generic request method or mutation retry."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        try:
            self._key = serialization.load_pem_private_key(
                settings.kalshi_private_key.get_secret_value().replace("\\n", "\n").encode(), None)
        except (ValueError, TypeError):
            raise ValueError("Invalid Kalshi PEM private key") from None
        if not isinstance(self._key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise ValueError("Kalshi requires an Ed25519 or RSA private key")
        if isinstance(self._key, rsa.RSAPrivateKey) and self._key.key_size < 2048:
            raise ValueError("RSA key must contain at least 2048 bits")
        self.http = httpx.AsyncClient(base_url=settings.api_origin, transport=transport,
                                      timeout=settings.api_timeout_seconds, follow_redirects=False,
                                      trust_env=False, limits=httpx.Limits(max_connections=10))

    def auth_headers(self, method, path, timestamp_ms=None):
        timestamp = str(timestamp_ms if timestamp_ms is not None else time.time_ns() // 1000000)
        message = (timestamp + method.upper() + path.split("?")[0]).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                          salt_length=padding.PSS.DIGEST_LENGTH),
                                       hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.settings.kalshi_api_key_id.get_secret_value(),
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}

    async def _request(self, method, endpoint, *, params=None, body=None):
        path = API_ROOT + endpoint
        try:
            response = await self.http.request(method, path, params=params, json=body,
                                                headers=self.auth_headers(method, path))
        except httpx.TimeoutException:
            raise APIError("api_timeout", ambiguous=method != "GET") from None
        except httpx.HTTPError:
            raise APIError("api_connection_error", ambiguous=method != "GET") from None
        if not response.is_success:
            status = response.status_code
            code = {401: "authentication_failed", 403: "permission_denied", 404: "not_found",
                    409: "exchange_conflict", 429: "rate_limited"}.get(status, "api_error")
            # Even conflict/rate-limit responses are not permission to re-submit.
            raise APIError(code, status, ambiguous=method != "GET" and (status >= 500 or status in {408, 409, 429}))
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return sanitize(result)
        except ValueError:
            raise APIError("invalid_api_response", ambiguous=method != "GET") from None

    async def close(self):
        await self.http.aclose()

    async def balance(self, exchange_index=None):
        params = {"subaccount": 0}
        if exchange_index is not None:
            params["exchange_index"] = exchange_index
        return await self._request("GET", "/portfolio/balance", params=params)

    async def positions(self, **params):
        return await self._request("GET", "/portfolio/positions", params={"subaccount": 0, **params})

    async def market(self, ticker):
        return await self._request("GET", f"/markets/{ticker}")

    async def markets(self, **params):
        return await self._request("GET", "/markets", params=params)

    async def orderbook(self, ticker, depth=20):
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def orders(self, **params):
        return await self._request("GET", "/portfolio/orders", params={"subaccount": 0, **params})

    async def order(self, order_id):
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def exchange(self):
        return await self._request("GET", "/exchange/status")

    async def create_order(self, body):
        return await self._request("POST", "/portfolio/events/orders", body=body)

    async def cancel_order(self, order_id, ticker):
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                                   params={"market_ticker": ticker, "exchange_index": -1, "subaccount": 0})

    async def all_pages(self, kind, **params):
        method, field = {"orders": (self.orders, "orders"),
                         "positions": (self.positions, "market_positions")}[kind]
        rows, cursor, seen = [], None, set()
        for _ in range(100):
            page = await method(limit=100, **params, **({"cursor": cursor} if cursor else {}))
            if not isinstance(page.get(field), list):
                raise APIError("invalid_api_response")
            rows.extend(page[field])
            cursor = page.get("cursor")
            if not cursor:
                return rows
            if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                raise APIError("invalid_pagination")
            seen.add(cursor)
        raise APIError("pagination_limit_exceeded")
