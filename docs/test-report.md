# Verification results

Checked on 2026-10-26 using Python 3.12.14.

| Check | Result |
|---|---|
| `pytest -q` | **171 passed** |
| `ruff check src tests scripts` | Passed |
| `python -m pip --no-cache-dir check` | No broken requirements |
| `sh -n start.sh` | Passed |
| `render.yaml` against Render's official JSON Schema | Passed |
| Official MCP ClientSession over Streamable HTTP | Initialize, discovery and balance tool call passed with mocked upstream |
| OAuth authorization code + PKCE + refresh + revocation | Passed with mocked upstream |

There is one test-only Starlette warning about its planned TestClient migration from httpx to httpx2. Tests currently use httpx because the pinned MCP SDK uses it. It does not affect the production transport.

## Requested coverage

| Requested case | Representative test |
|---|---|
| Market lookup | `test_read_tools`, `test_market_call_over_mcp` |
| Balance lookup | `test_read_tools`, `test_official_mcp_client_round_trip` |
| Position lookup | `test_read_tools` |
| Valid dry run | `test_safe_defaults_and_valid_dry_run`, `test_owner_approves_exact_dry_run_over_http` |
| Trading disabled | `test_trading_disabled` |
| Missing request ID | `test_missing_request_id` |
| Duplicate request | `test_duplicate_request`, `test_concurrent_same_request` |
| Invalid market | `test_invalid_market` |
| Closed market | `test_market_validation` |
| Invalid side | `test_invalid_order_schema` |
| Invalid price | `test_invalid_order_schema`, `test_market_validation` |
| Invalid count | `test_invalid_order_schema` |
| Insufficient balance | `test_insufficient_balance_includes_fee_reserve` |
| Existing duplicate order | `test_duplicate_existing_order`, `test_duplicate_on_later_page` |
| Partial fill | `test_partial_fill` |
| Cancellation | `test_cancellation_while_disabled_and_halted`, `test_cancel_filled_race_preserves_executed_quantity` |
| API timeout | `test_timeout`, `test_post_timeout_never_retries`, `test_cancel_timeout_is_unknown_and_no_automatic_reissue` |
| Authentication failure | `test_authentication_failure` |
| Restart persistence | `test_restart_preserves_idempotency`, `test_reconcile_unknown_after_restart_never_reposts` |
| Malformed MCP | `test_malformed_mcp_request`, `test_oversized_body` |

Additional tests cover both signature algorithms, exact V2 order request schema and four-way price mapping, owner approval expiry/binding, reader enforcement, changing payloads under the same ID, lost acknowledgments, stale market snapshots, account projection lag, persistent stop controls, token secrecy, OAuth redirect restrictions and exclusive process locking.

All Kalshi calls in tests were mocked. No production credentials were used and no live order was placed. A real hosted dry run and Grok Custom Connector handshake remain owner deployment checks, described in the README.

## Optional per-order approval update

The deployment now sets `REQUIRE_TRADE_APPROVAL=false` at the owner's request. Thirteen additional tests cover no-approval dry run, live-disable enforcement, restart deduplication, optional approval validation, reader isolation, policy-override rejection, daily budget persistence/reset, unknown commitments and cancellation/refund behavior. Live trading remains disabled. The daily limit is a submission commitment ceiling including conservative fee reserves, not a loss guarantee.

## OAuth browser-origin regression

Six added cases exercise consent with an explicit same-origin or opaque Origin header, rejection of explicit foreign origins, the required cookie-bound nonce, and unchanged owner-route origin restrictions. The hosted error was reported as invalid_origin; the actual failing browser header was not captured. This change covers the opaque-origin case and must still be verified through a fresh Grok connection after deployment.

## OAuth cookie compatibility

Nine added HTTPS consent cases verify Secure/HttpOnly/host-only SameSite=None cookies, per-attempt cookie isolation, expiry, replay rejection, bad secrets, and missing/mismatched cookie diagnostics. Normal owner-route restrictions are unchanged. The real Grok browser flow still requires verification after the new fix is committed and deployed.
