from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from src.kalshi_client import KalshiClient
from src.server import create_app

from .conftest import Exchange


@pytest.fixture
def https_web(settings):
    settings.public_base_url = "https://mcp.example.test"
    app = create_app(settings, KalshiClient(settings, httpx.MockTransport(Exchange().handle)))
    with TestClient(app, base_url=settings.public_base_url) as browser:
        yield browser, app


def begin_login(browser):
    registration = browser.post("/register", json={"redirect_uris": ["https://grok.com/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": "read"})
    assert registration.status_code == 201
    params = {"client_id": registration.json()["client_id"], "redirect_uri": "https://grok.com/callback",
              "response_type": "code", "state": "state", "code_challenge": "x" * 43,
              "code_challenge_method": "S256", "resource": "https://mcp.example.test/mcp"}
    authorization = browser.get("/authorize?" + urlencode(params), follow_redirects=False)
    location = authorization.headers["location"]
    nonce = parse_qs(urlsplit(location).query)["nonce"][0]
    response = browser.get(location)
    assert response.status_code == 200
    return nonce, response.headers["set-cookie"]


def submit(browser, nonce, origin="null", secret="r" * 48):
    return browser.post("/consent", data={"nonce": nonce, "login_token": secret},
                        headers={"Origin": origin} if origin else {}, follow_redirects=False)


@pytest.mark.parametrize("origin", [None, "null", "https://mcp.example.test"])
def test_https_oauth_cookie_is_cross_site_safe_and_host_bound(https_web, origin):
    browser, _ = https_web
    nonce, cookie = begin_login(browser)
    assert cookie.startswith("__Host-kalshi-consent-")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=none" in cookie
    assert "Max-Age=300" in cookie and "Path=/" in cookie and "Domain=" not in cookie
    response = submit(browser, nonce, origin)
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_parallel_login_attempts_have_independent_browser_cookies(https_web):
    browser, app = https_web
    first, _ = begin_login(browser)
    second, _ = begin_login(browser)
    assert first != second
    assert len([c for c in browser.cookies.jar if c.name.startswith("__Host-kalshi-consent-")]) == 2
    assert submit(browser, first).status_code == 200
    assert app.state.oauth.consent_info(second) is not None
    assert submit(browser, second).status_code == 200


def test_missing_browser_cookie_is_diagnosed_without_consuming_login(https_web):
    browser, app = https_web
    nonce, _ = begin_login(browser)
    browser.cookies.clear()
    response = submit(browser, nonce)
    assert response.status_code == 403 and response.json()["reason"] == "browser_cookie_missing"
    assert app.state.oauth.consent_info(nonce) is not None


def test_mismatched_browser_cookie_is_rejected(https_web):
    browser, _ = https_web
    nonce, cookie = begin_login(browser)
    name = cookie.split("=", 1)[0]
    browser.cookies.clear()
    browser.cookies.set(name, "wrong", domain="mcp.example.test", path="/")
    response = submit(browser, nonce)
    assert response.status_code == 403 and response.json()["reason"] == "browser_cookie_mismatch"


def test_wrong_connector_secret_still_rejected(https_web):
    browser, app = https_web
    nonce, _ = begin_login(browser)
    response = submit(browser, nonce, secret="wrong")
    assert response.status_code == 403
    assert app.state.oauth.consent_info(nonce) is not None
    with app.state.db.connection() as c:
        assert c.execute("SELECT count(*) FROM oauth WHERE kind IN ('access', 'code')").fetchone()[0] == 0


def test_consent_request_cannot_be_replayed(https_web):
    browser, _ = https_web
    nonce, _ = begin_login(browser)
    assert submit(browser, nonce).status_code == 200
    response = submit(browser, nonce)
    assert response.status_code == 403 and response.json()["reason"] == "login_request_expired_or_missing"


def test_expired_login_is_distinguished_from_cookie_failure(https_web):
    browser, app = https_web
    nonce, _ = begin_login(browser)
    with app.state.db.connection() as c:
        c.execute("UPDATE oauth SET expires=0 WHERE kind='pending'")
    response = submit(browser, nonce)
    assert response.status_code == 403 and response.json()["reason"] == "login_request_expired_or_missing"


def test_consent_policy_allows_registered_callback_without_intersecting_self_only_policy(https_web):
    browser, _ = https_web
    nonce, _ = begin_login(browser)
    page = browser.get("/consent", params={"nonce": nonce})
    policies = page.headers.get_list("content-security-policy")
    assert len(policies) == 1
    assert policies[0].endswith("form-action 'self' https://grok.com")
    assert "https://accounts.x.ai" not in policies[0]
    assert "script-src 'self'" in policies[0] and "frame-ancestors 'none'" in policies[0]
    approved = submit(browser, nonce)
    assert approved.status_code == 200
    assert 'http-equiv="refresh"' in approved.text
    assert 'href="https://grok.com/callback?' in approved.text
    assert "Do not resubmit" in approved.text
    assert approved.headers["content-security-policy"].endswith("form-action 'self'")
    assert "location" not in approved.headers
    assert "<form" not in approved.text and "login_token" not in approved.text
    assert len(approved.headers.get_list("content-security-policy")) == 1
    # Other pages keep their stricter policy.
    assert browser.get("/owner").headers["content-security-policy"].endswith("form-action 'self'")
