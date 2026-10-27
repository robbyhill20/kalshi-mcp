# Implementation verification

Official sources checked on **2026-10-26**. This is an implementation record, not a guarantee that upstream APIs will remain unchanged.

Updated owner preference: per-order approval is optional. The included deployment sets `REQUIRE_TRADE_APPROVAL=false`; safe dry-run/trading-disabled defaults remain. A transactional daily submission ceiling and all duplicate/role/stop controls apply in both modes.

| Subject | Official source | Implementation |
|---|---|---|
| Credentials and signatures | [Kalshi API keys](https://docs.kalshi.com/getting_started/api_keys) | Detect parsed Ed25519 or RSA key. Base64 signature over timestamp milliseconds + uppercase method + full path excluding query. RSA-PSS/SHA256 uses digest-length salt. |
| Fixed upstream hosts | [Environments](https://docs.kalshi.com/getting_started/api_environments) | Recommended `external-api` production/demo hosts; no caller-selected endpoint. |
| Available cash | [Balance](https://docs.kalshi.com/api-reference/portfolio/get-balance) | Primary subaccount; placement scopes balance to the market's exchange index. Available balance already accounts for exchange holds. Additional local reserves deliberately err toward rejection. |
| Holdings | [Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions) | Uses `position_fp`, including negative NO positions. Entire unsettled position pagination is checked. |
| Markets | [Get market](https://docs.kalshi.com/api-reference/market/get-market), [list](https://docs.kalshi.com/api-reference/market/get-markets), [orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook) | Exact ticker paths; closed/paused markets rejected for placement. |
| Market lifecycle | [Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle) | REST trading status is `active`; filter status `open` is different. Also check open/close timestamps. |
| Tick grid and quantities | [Fixed point](https://docs.kalshi.com/getting_started/fixed_point_migration) | Decimal arithmetic; `price_ranges` checked without rounding. Connector deliberately accepts whole contracts and whole-cent limits only. |
| Current create endpoint | [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2) | `POST /portfolio/events/orders`, fixed-point `count` and `price`, `bid`/`ask`, required STP and time-in-force, automatic shard routing. No legacy creation endpoint. |
| Book price semantics | [Order direction](https://docs.kalshi.com/getting_started/order_direction), `BookSide` in [OpenAPI](https://docs.kalshi.com/openapi.yaml) | V2 book price is YES-denominated. Buy YES = bid at p; buy NO = ask at 1-p; sell YES = ask at p; sell NO = bid at 1-p. This is representation translation, not changing the user's chosen outcome. |
| Order reads | [Get order](https://docs.kalshi.com/api-reference/orders/get-order), [get orders](https://docs.kalshi.com/api-reference/orders/get-orders) | Reads remain `/portfolio/orders`. `resting` includes partly filled orders; canonical `book_side` and `_fp` quantities are used. |
| Current cancellation | [Cancel V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2) | `DELETE /portfolio/events/orders/{order_id}` with verified `market_ticker`, shard routing and primary subaccount; follow with GET. Ack alone is not final status. |
| Grok consumer connector | [Custom connectors](https://docs.x.ai/grok/connectors), [public reachability/transport](https://docs.x.ai/grok/connectors/custom-mcp-tunneling) | Public HTTPS `/mcp`, Streamable HTTP. OAuth owner consent so the UI need not expose custom-header settings. |
| xAI API integration | [Remote MCP](https://docs.x.ai/developers/tools/remote-mcp) | HTTPS; bearer authorization and tool allowlists supported. API `require_approval` is not supported, so approval enforcement lives here. API parameters are not assumed to be consumer UI options. |
| MCP protocol/authentication | [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk), [authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization) | Pinned stable 1.x SDK; stateless Streamable HTTP, OAuth discovery, PKCE, dynamic registration, access/refresh tokens, resource binding. |
| Managed hosting | [Render Python service](https://render.com/docs/deploy-fastapi), [persistent disks](https://render.com/docs/disks), [Blueprint specification](https://render.com/docs/blueprint-spec) | Paid single-instance service, persistent SQLite disk, managed HTTPS. |

The checked Kalshi OpenAPI document reports version **3.35.0**. A source fingerprint and a minimal contract fixture are stored beside this record. The fixture keeps only the create request contract needed to detect accidental legacy field use; it is not an alternate API specification.

## Safety review

- **Duplicates:** request primary key, approval unique constraint, exact payload comparison, durable intent before POST, persistent global placement gate, exchange client ID equal to request UUID, open-order pagination and local active-order duplicate checks. Distinct request IDs can intentionally authorize distinct trades. When approval is required, approval IDs are also checked and consumed once. Deleting/restoring the database undermines deduplication.
- **Credential exposure:** no PEM in MCP/OAuth responses; separate owner, reader and director secrets; no raw HTTP exception bodies; no access-query logging; redacted audit records; sanitized startup configuration errors. Secrets remain in hosting environment storage. Database contains account snapshots and OAuth client credentials: protect backups.
- **Retry ambiguity:** no automatic upstream HTTP retries, redirects or replacement orders. Timeout/disconnect/5xx/conflict after POST leaves an unresolved intent. Absence from a list never proves failure. Unknown orders block new placements until a positively identified order is reconciled.
- **Partial fills:** use Decimal quantities; report both executed and remaining contracts. Cancellation affects only remaining contracts; fills persist.
- **Staleness:** fresh account data, final market refetch, market time checks, per-shard exchange status, short preflight age bound. REST snapshots cannot form an atomic exchange snapshot. Kalshi remains the final authority. Local reserves persist through projection lag. Run a dedicated account with no competing writers for the strongest isolation.
- **Untrusted arguments:** additional fields rejected; bounded inputs; fixed upstream hosts/routes; validated ticker/UUID path segments; no shell/code/URL/config/credential tools. No strategy logic exists.
- **Caller roles:** identity comes from credential scopes. The text name “Sports Director” has no security significance. A shared director credential grants its holders cancellation rights and execution within configured owner policy; isolate bot connections or use xAI API tool allowlists.
- **Unknown recovery:** order IDs are persisted before follow-up reads. Reconciliation is read-only and can run with trading disabled. Automated retry on a new request ID is prohibited. If the exchange cannot positively resolve an intent (including historical cutoff), leave placement blocked and contact Kalshi support. There is intentionally no bot-accessible reset.

## Remaining deployment verification

Automated checks mock Kalshi; they do not prove that a real key has current permissions, that hosting egress is permitted by your Kalshi account, or that a particular Grok account exposes identical connector UI controls. Real authenticated dry-run checks and an actual Grok connection require the owner's credentials and hosted service. No live trade was placed during development.

## Grok Bot callback compatibility

Cursor documents its fixed MCP callbacks at https://prod.cursor.com/docs/mcp#static-redirect-url. Grok Bot uses Cursor connector infrastructure (https://docs.x.ai/grok-bot/computer-and-apps). The server accepts exactly `https://www.cursor.com/agents/mcp/oauth/callback` and `http://localhost:8787/callback` in addition to its configured HTTPS hosts. Other Cursor paths, HTTP ports, loopback hosts and query variants remain rejected. The actual callback used by this account has not yet been observed; validate with a fresh Bot sign-in after deployment.

Consent completion now returns an HTML document with an escaped, previously validated callback in a meta-refresh and fallback link. This ends the form POST before cross-origin callback navigation. A localhost browser reproduction demonstrated that a 303 callback chain stalls under form-action CSP while document navigation reaches the final origin. The one-time consent cookie is deleted and consumed requests still cannot be replayed. Real Sports Director completion remains to be verified after deployment.

## Price-based fee reserves

Verified official Get Series, Get Event and openapi.yaml fee_type/fee_multiplier/event overrides; https://docs.kalshi.com/getting_started/fee_rounding explains six-decimal trade fees, cent precision and accumulated rebates. Maker coefficients: 0 for quadratic, 0.0175 for quadratic_with_maker_fees, 0.035 for quadratic_with_combo_maker_fees; taker 0.07, all scaled by current multiplier. Taking reserves the quadratic maximum over prices permitted by the buy/sell limit. Estimates round up to cents and add a one-cent per-order rounding cushion plus any explicit owner buffer. Unknown/flat models reject before submission. Metadata is fetched anew for each new request, never for replay. These reserves are estimates; future fee changes and exchange accounting remain authoritative. No live order was submitted during verification.
