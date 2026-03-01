# NonKYC.io Exchange Connector — Test Suite Documentation

This document covers all tests for the NonKYC.io Hummingbot integration across both repositories.

There are **two separate test suites** in two different repos:

| Suite | Repo | Runner | Tests | Needs API Keys | Needs Network |
|-------|------|--------|-------|----------------|---------------|
| Connector unit tests | `hummingbot` (branch `nonkyc`) | pytest | 320 | Some | Some |
| API validation suite | `hummingbot-api` | standalone script | 69 | Yes | Yes |

---

## Prerequisites

Both test suites require a `.env` file with your NonKYC API credentials. The `.env` file should be in the root of each respective repo.

```env
NONKYC_API_KEY=your_api_key_here
NONKYC_API_SECRET=your_api_secret_here
```

Python dependencies (both repos use Python 3.12):

```bash
# For the hummingbot connector tests
pip install pytest aioresponses

# For the API validation suite
pip install aiohttp websockets python-dotenv
```

---

## 1. Connector Unit Tests (hummingbot repo)

**Location:** `test/hummingbot/connector/exchange/nonkyc/`
**Total:** 320 tests across 17 files
**Last verified:** 320/320 passing

### Running from VS Code / Terminal (Windows)

```powershell
cd E:\tradingsoftware\hummingbot

# Run all NonKYC connector tests
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v

# Run a single test file
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_bugfixes.py -v

# Run a single test class
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_bugfixes.py::TestBug1OrderBookVariableName -v

# Run a single test method
python -m pytest test/hummingbot/connector/exchange/nonkyc/test_nonkyc_bugfixes.py::TestBug1OrderBookVariableName::test_diff_bids_and_asks_are_independent -v

# Run with short traceback (easier to scan)
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v --tb=short

# Run only fast offline tests (skip live API tests)
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v -k "not live"
```

### Running inside the Docker container

```bash
# Exec into the running hummingbot container
docker exec -it hummingbot bash

# Inside the container, the repo is at /home/hummingbot
cd /home/hummingbot

# Run all NonKYC tests
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v

# Run only offline tests (no API keys needed)
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v \
  --ignore=test/hummingbot/connector/exchange/nonkyc/test_nonkyc_live_api.py

# Note: the container must have network access for live API tests.
# If running behind the VPN (gluetun), this works automatically.
```

### Running the standalone auth diagnostic

This is a quick script that tests REST auth signatures against the live API. Useful for debugging auth issues without running the full test suite.

```powershell
# From VS Code / terminal
python test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py

# Inside Docker
python test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py
```

### Running the paper trade setup helper

Checks if `nonkyc` is in the `paper_trade_exchanges` list in `conf_client.yml`. Optionally patches it in.

```powershell
# Check only
python test/hummingbot/connector/exchange/nonkyc/setup_paper_trade.py

# Auto-patch conf_client.yml
python test/hummingbot/connector/exchange/nonkyc/setup_paper_trade.py --apply
```

---

## 2. API Validation Suite (hummingbot-api repo)

**Location:** `tests/run_nonkyc_tests.py`
**Total:** 69 tests across 7 phases (A–G)
**Last verified:** 67/69 passing (F3 timing tolerance, G2 awaiting Phase 1 P0-2 fix)

### Running from VS Code / Terminal (Windows)

```powershell
cd E:\tradingsoftware\hummingbot-api

# Run all phases
python tests/run_nonkyc_tests.py

# Run specific phases only
python tests/run_nonkyc_tests.py --phases A,B,C,D

# Skip order placement (no real money used)
python tests/run_nonkyc_tests.py --skip-orders

# Clean up stale orders from ALL prefixes (including live bot orders)
# WARNING: this cancels HBOT-CID orders which may belong to a running bot
python tests/run_nonkyc_tests.py --cleanup-all

# Combine flags
python tests/run_nonkyc_tests.py --phases E,F --cleanup-all

# Specify repo path for Phase G code verification
python tests/run_nonkyc_tests.py --repo-path E:\tradingsoftware\hummingbot-api
```

### Running inside the Docker container

```bash
# The hummingbot-api tests are not typically run inside the hummingbot
# container. They run against the hummingbot-api service. If you need
# to run them from within the stack:

docker exec -it hummingbot-api bash
cd /home/hummingbot
python tests/run_nonkyc_tests.py

# Or from the host, targeting the container's Python:
docker exec -it hummingbot-api python tests/run_nonkyc_tests.py --skip-orders
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--phases A,B,C,...` | All (A–G) | Comma-separated list of phases to run |
| `--skip-orders` | Off | Skip Phase E entirely (no real orders placed) |
| `--cleanup-all` | Off | E0 cleanup also cancels `HBOT-CID` orders (live bot orders). Without this flag, only `HBOT-TEST` orders are cleaned up |
| `--repo-path PATH` | Auto-detect | Path to hummingbot-api repo root for Phase G code verification |

### Safety Notes

The E0 cleanup step runs at the start of Phase E. By default it **only** cancels orders with the `HBOT-TEST` prefix — these are orders created by the test script itself. It will never touch orders created by a live running Hummingbot bot (which use the `HBOT-CID` prefix).

If you pass `--cleanup-all`, the cleanup expands to include `HBOT-CID` orders too. Only use this when you are certain no live bots are running, as it will cancel their open orders.

---

## Test File Reference — Connector Unit Tests (320 tests)

### test_nonkyc_exchange.py — 37 tests (inherited)

The core exchange connector test suite. Inherits from Hummingbot's `AbstractExchangeConnectorTests` framework, which provides standardized tests that every exchange connector must pass. All tests use mocked HTTP responses — no live API calls.

Tests cover: order creation (buy/sell limit), order cancellation, cancel-all, balance updates, trading rule parsing, order status polling, lost order recovery, user stream event processing (balance updates, order fills, cancellations), trade fee handling, and trading pair validation.

### test_nonkyc_api_order_book_data_source.py — 9 tests

Tests the WebSocket order book data source that maintains the live order book. All mocked.

| Test | What it verifies |
|------|-----------------|
| `test_get_new_order_book_successful` | REST orderbook snapshot parsed into OrderBook object |
| `test_listen_for_order_book_snapshots_from_ws` | WS `snapshotOrderbook` messages create valid snapshots |
| `test_listen_for_order_book_diffs` | WS `updateOrderbook` messages create valid diffs |
| `test_listen_for_trades_logs_trade_messages` | WS trade messages queued correctly |
| `test_parse_trade_message_processes_all_trades` | Multiple trades in a single WS message all processed |
| `test_snapshot_trades_handled` | `snapshotTrades` with multiple entries parsed (not just first) |
| `test_sequence_gap_triggers_resync` | Sequence gap in WS orderbook triggers a full resync |
| `test_duplicate_sequence_skipped` | Duplicate sequence numbers silently dropped |
| `test_unsubscribe_sends_ws_messages` | Cleanup sends proper unsubscribe JSON-RPC messages |

### test_nonkyc_api_user_stream_data_source.py — 8 tests

Tests the authenticated WebSocket connection used for balance updates and order reports.

| Test | What it verifies |
|------|-----------------|
| `test_auth_response_validated_success` | WS login `result: true` accepted |
| `test_auth_failure_raises` | WS login error raises IOError |
| `test_auth_failure_not_retried` | Explicit auth failure (wrong creds) not retried |
| `test_auth_retries_on_timeout` | Timeout triggers retry with backoff |
| `test_auth_timeout_raises` | Max retries exhausted raises IOError |
| `test_auth_skips_non_auth_messages` | Non-auth messages (tickers, etc.) skipped during auth wait |
| `test_subscribe_channels` | Sends subscribeReports + subscribeBalances after auth |
| `test_user_stream_interruption_cleanup` | WS disconnect cleans up assistant reference |

### test_nonkyc_auth.py — 4 tests

Tests the authentication module (HMAC-SHA256 signatures).

| Test | What it verifies |
|------|-----------------|
| `test_rest_authenticate_get` | GET request signed with correct header format |
| `test_rest_authenticate_post` | POST request signed with minified body included |
| `test_ws_authenticate_message` | WS login message has correct HS256 structure |
| `test_ws_nonce_is_string_not_list_repr` | WS nonce is a clean string, not Python list `repr()` |

### test_nonkyc_order_book.py — 7 tests

Tests the OrderBook message parsing (converting raw API data to internal format).

| Test | What it verifies |
|------|-----------------|
| `test_snapshot_message_from_exchange` | REST snapshot → OrderBookMessage |
| `test_snapshot_message_from_ws` | WS snapshot → OrderBookMessage |
| `test_diff_message_from_exchange` | WS diff update → OrderBookMessage |
| `test_trade_message_from_exchange_with_iso_timestamp` | ISO8601 trade timestamp parsed correctly |
| `test_trade_message_from_exchange_with_timestampms` | Integer ms trade timestamp parsed correctly |
| `test_trade_messages_from_exchange_multiple` | Multiple trades in one message all returned |
| `test_trade_messages_from_exchange_empty_data` | Empty trade data returns empty list |

### test_nonkyc_utils.py — 7 tests

Tests utility functions (timestamp conversion, config validation, fee defaults).

| Test | What it verifies |
|------|-----------------|
| `test_convert_fromiso_to_unix_timestamp_with_z_suffix` | ISO8601 with "Z" suffix parsed |
| `test_convert_fromiso_to_unix_timestamp_with_offset` | ISO8601 with timezone offset parsed |
| `test_convert_fromiso_to_unix_timestamp_timezone_correctness` | Timezone conversion produces correct epoch |
| `test_default_fees_are_0_0015` | Default maker/taker fees are 0.15% |
| `test_example_pair_is_valid` | EXAMPLE_PAIR is "BTC-USDT" |
| `test_is_exchange_information_valid_active` | Active markets pass validation |
| `test_is_exchange_information_valid_inactive` | Inactive markets filtered out |

### test_nonkyc_web_utils.py — 4 tests

Tests URL construction and API factory creation.

| Test | What it verifies |
|------|-----------------|
| `test_public_rest_url` | Public REST URL built correctly |
| `test_private_rest_url` | Private REST URL built correctly |
| `test_get_current_server_time` | Server time endpoint called and parsed |
| `test_build_api_factory` | WebAssistantsFactory created with auth |

### test_nonkyc_bugfixes.py — 16 tests

Tests for the original 5 bug fixes identified in the initial code review.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestBug1OrderBookVariableName` | 2 | Order book diff correctly separates bids and asks (variable name fix) |
| `TestBug2BalanceUpdateNullGuard` | 3 | `balanceUpdate` with null/missing params doesn't crash |
| `TestBug3RequestOrderStatusPreferExchangeId` | 3 | Order status lookup prefers exchange_order_id over client_order_id |
| `TestBug4SymbolSplitGuard` | 3 | Symbol parsing handles normal, no-slash, and multi-slash cases |
| `TestBug5RejectedOrderState` | 5 | "Rejected" order state maps to FAILED, all 19 states present |

### test_nonkyc_phase5a.py — 8 tests

Phase 5A fixes: order types, decimal precision, cancel-all endpoint, server time.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase5AOrderTypes` | 4 | LIMIT_MAKER maps to "limit", supported types list correct |
| `TestPhase5ADecimalPrecision` | 2 | Decimal step sizes use exact math (not float) |
| `TestPhase5AConstants` | 2 | CANCEL_ALL_ORDERS_PATH_URL exists and has rate limit |

### test_nonkyc_phase5c.py — 11 tests

Phase 5C fixes: dynamic fee computation from trade history.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase5CDynamicFees` | 11 | Fee cache initialized empty, computes from trade history, averages multiple trades, classifies maker/taker, handles errors, skips zero-fee trades, falls back to defaults |

### test_nonkyc_phase5d.py — 11 tests

Phase 5D fixes: crash recovery and batch order cancellation.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase5DCrashRecovery` | 11 | cancel_all overridden, batch cancel per symbol, orphan detection, fallback to individual cancel, empty tracker with exchange orders, partial failure handling, multi-symbol support |

### test_nonkyc_phase6.py — 13 tests

Phase 6: Rate oracle integration (NonKYC as a price source for cross-exchange strategies).

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase6RateOracle` | 13 | Registered in RATE_ORACLE_SOURCES, correct class/name/inheritance, builds connector without keys, returns mid prices, filters by quote token, handles errors, skips crossed/zero books |

### test_nonkyc_phase7a.py — 29 tests

Phase 7A: auth hardening, order type safety, fee extraction, cancel fallback, trading rules.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase7APostAuth` | 3 | POST body minified, signature uses same string as body |
| `TestPhase7AGetAuth` | 3 | GET params sorted for deterministic signatures |
| `TestPhase7AOrderTypeSafety` | 6 | Case-insensitive order type mapping, unknown types default to LIMIT |
| `TestPhase7AFeeExtraction` | 6 | alternateFeeAsset handling, WS tradeFee field, zero fee |
| `TestPhase7ACancelFallback` | 4 | exchange_order_id preferred, fallback to client_order_id |
| `TestPhase7ATradingRules` | 7 | allowMarketOrders, maximumQuantity, min_order_size, min_notional |

### test_nonkyc_phase7b.py — 22 tests

Phase 7B: WS JSON-RPC compliance, variable naming, domain, documentation.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPhase7BWsJsonRpcId` | 3 | Auth login payload has `id` field |
| `TestPhase7BOrderBookWsId` | 3 | Order book WS IDs increment correctly |
| `TestPhase7BUserStreamWsId` | 2 | User stream WS IDs start at 100 |
| `TestPhase7BVariableNaming` | 4 | `nonkyc_order_type` renamed (lowercase), old name removed |
| `TestPhase7BDefaultDomain` | 3 | DEFAULT_DOMAIN is "nonkyc" not "com" |
| `TestPhase7BLimitMakerDoc` | 3 | LIMIT_MAKER docstring exists with correct content |
| `TestPhase7BRateLimitDocs` | 2 | Rate limits documented |
| `TestPhase7BReadme` | 2 | README.md exists with content |

### test_nonkyc_phase7c.py — 30 tests

Phase 7C: edge cases, error paths, and resilience.

| Class | Tests | What it verifies |
|-------|-------|-----------------|
| `TestPostAuthBodyMatchesSignature` | 1 | POST auth deterministic |
| `TestGetAuthStableWithUnorderedParams` | 1 | GET param order doesn't affect signature |
| `TestAlternateFeeAssetHandling` | 2 | Alternate fee asset null/present paths |
| `TestWsBalanceUpdateEvent` | 1 | Incremental balance update structure |
| `TestWsActiveOrdersSnapshot` | 3 | activeOrders via result/params keys, non-list guard |
| `TestGetLastTradedPriceFallback` | 2 | Primary endpoint + tickers list fallback |
| `TestMarketOrderNoPrice` | 3 | MARKET orders exclude price param |
| `TestCancelAllDeterministicOrder` | 1 | Symbols processed in sorted order |
| `TestCancelAllTimeout` | 1 | Timeout marks all as failed |
| `TestUpdateTradingFeesEdgeCases` | 4 | Empty history, malformed trades, maker-only, taker-only |
| `TestPlaceCancelFallback` | 2 | Cancel with valid ID vs None |
| `TestHttp200WithErrorBody` | 2 | Error body inside HTTP 200 detected |
| `TestAllowMarketOrdersExclusion` | 2 | Trading rule allowMarketOrders flag |
| `TestMaxOrderSizeValidation` | 2 | maximumQuantity parsing |
| `TestMultiPairSubscribeSequenceCleanup` | 3 | Sequence tracking per pair, cleanup on unsubscribe |

### test_nonkyc_live_api.py — 82 tests

The comprehensive live API validation script that runs directly against the NonKYC production API. Organized into 16 tiers covering every phase of connector development. Requires API keys and network access.

Tiers 1–4 cover public/authenticated REST and WebSocket endpoints. Tiers 5–7 cover phase-specific fix validation. Tiers 8–16 validate every feature phase (5A through 7C) against the live API, confirming that mocked test assumptions match real exchange behavior.

---

## Test File Reference — API Validation Suite (69 tests)

### Phase A: Connector Logic (10 tests) — Offline

Validates cryptographic signatures and data parsing without making any network calls.

| Test | What it verifies |
|------|-----------------|
| A1 | REST GET signature is 64-char hex |
| A2 | REST POST signature differs from GET (body changes hash) |
| A3 | Different nonces produce different signatures |
| A4 | REST nonce is 13-digit millisecond timestamp |
| A5 | WS HS256 signature = HMAC(secret, nonce) |
| A6 | WS signature differs from REST signature (different input format) |
| A7 | Trading pair parsing handles `/`, `_`, `-` separators |
| A8 | Order type mapping: LIMIT→limit, MARKET→market, LIMIT_MAKER→limit |
| A9 | All 13 order states map to valid Hummingbot targets |
| A10 | String-to-Decimal preserves arbitrary precision |

### Phase B: REST Public Endpoints (12 tests) — No API keys needed

Tests all public REST endpoints against the live NonKYC API.

| Test | What it verifies |
|------|-----------------|
| B1 | Server reachable (HTTP 200) |
| B2 | `/market/getlist` returns markets (351+) |
| B3 | Market object has required fields (symbol, primaryAsset, secondaryAsset, isActive) |
| B4 | Market has `primaryTicker` field at top level |
| B5 | BTC/USDT market exists and is active |
| B6 | Market has `priceDecimals` + `quantityDecimals` |
| B7 | `/market/orderbook` returns asks and bids |
| B8 | Orderbook entries have `price` and `quantity` keys |
| B9 | `/asset/getlist` returns assets (381+) |
| B10 | BTC asset found in asset list |
| B11 | `/tickers` returns ticker data |
| B12 | `/market/trades` returns trade list |

### Phase C: REST Authenticated Endpoints (10 tests) — API keys required

Tests authenticated endpoints and verifies auth rejection for bad credentials.

| Test | What it verifies |
|------|-----------------|
| C1 | Valid signature accepted (GET /balances → 200) |
| C2 | Invalid signature rejected (→ 401) |
| C3 | Missing auth headers rejected (→ 401) |
| C4 | `/balances` returns list of balances |
| C5 | Balance entry has `asset`, `available`, `held` fields |
| C6 | At least one non-zero balance exists |
| C7 | `/account/orders` returns order list |
| C8 | `/account/trades` returns trade list |
| C9 | Trade has `side` and `triggeredBy` fields |
| C10 | Trade has `fee`, `price`, `quantity` fields |

### Phase D: WebSocket Public + Authenticated (12 tests) — API keys required

Tests the JSON-RPC 2.0 WebSocket API.

| Test | What it verifies |
|------|-----------------|
| D1 | WS connects to `wss://api.nonkyc.io` |
| D2 | WS `getMarkets` returns market list |
| D3 | WS `getAssets` returns asset list |
| D4 | WS `subscribeOrderbook` receives snapshot |
| D5 | Snapshot has asks, bids, sequence fields |
| D6 | WS `subscribeTicker` receives ticker updates |
| D7 | WS connection stable for 5 seconds |
| D8 | WS `login` succeeds with HS256 auth |
| D9 | WS `subscribeReports` returns `activeOrders` |
| D10 | WS `getTradingBalance` returns balance data |
| D11 | WS `subscribeBalances` returns `currentBalances` with `ticker` field |
| D12 | REST vs WS field mapping documented (REST=`asset`, WS sub=`ticker`, WS get=`asset`) |

### Phase E: Order Lifecycle (11 tests) — Requires USDT balance

Places, verifies, and cancels real orders on the exchange. Uses 80% of available balance with a limit price 50% below market (ensures orders never fill).

| Test | What it verifies |
|------|-----------------|
| E0 | Cleanup stale test orders (only `HBOT-TEST` prefix by default) |
| E1 | Fetch BTC/USDT last price |
| E2 | Place limit BUY at 50% below market → status=New |
| E3 | Order visible in `GET /account/orders` |
| E4 | Order retrievable via `GET /getorder/{id}` → status=Active |
| E5 | Order visible in WS `subscribeReports` → activeOrders |
| E6 | Cancel order via `POST /cancelorder` → `{success: true, id: "..."}` |
| E7 | Order no longer in active orders after cancel |
| E8 | Re-cancelling an already-cancelled order returns HTTP 400 gracefully |
| E9 | Cancel via userProvidedId — REST accepts both ID formats |
| E10 | Order below minimum size rejected (HTTP 400) |

### Phase F: Data Consistency Validation (9 tests) — API keys required

Cross-validates data between REST and WebSocket to ensure the connector's field mapping is correct.

| Test | What it verifies |
|------|-----------------|
| F1 | REST `/balances` matches WS `getTradingBalance` (asset names + amounts) |
| F2 | REST `asset` field == WS `subscribeBalances` `ticker` field |
| F3 | REST vs WS orderbook structure valid (both have asks/bids) |
| F4 | Symbol format: `BTC/USDT` → Hummingbot `BTC-USDT`, `primaryTicker` present |
| F5 | Market precision: `priceDecimals` and `quantityDecimals` present and numeric |
| F6 | Fee rates sane (0–5%): maker/taker classification from `side` vs `triggeredBy` |
| F7 | Ticker `lastPrice` matches market data |
| F8 | All order states seen in responses map to known Hummingbot states |
| F9 | Server time drift < 5 seconds |

### Phase G: Code Verification (5 tests) — Reads local repo files

Scans the `hummingbot-api` codebase to verify that Phase 1 infrastructure fixes have been applied. Does not make API calls.

| Test | What it verifies |
|------|-----------------|
| G1 | `docker_service.py` uses `DOCKER_BOT_NETWORK_MODE` (VPN routing for spawned bots) |
| G2 | `unified_connector_service.py` starts `_status_polling_task` (Phase 1 P0-2) |
| G3 | `/health` endpoint exists in API server |
| G4 | Container filter matches `hummingbot-nonkyc` image name |
| G5 | `debug_mode` has safety warning guard |

---

## Known Failures

| Test | Status | Explanation |
|------|--------|-------------|
| F3 | Timing tolerance | REST and WS orderbook snapshots are not atomic — slight differences expected. Not a connector bug. |
| G2 | Awaiting fix | Phase 1 fix P0-2 (`_status_polling_task`) has not been applied to the hummingbot-api repo yet. |

---

## Quick Reference

```powershell
# ── FAST CHECK (no orders, no money) ──
python tests/run_nonkyc_tests.py --phases A,B,C,D --skip-orders

# ── FULL VALIDATION (places + cancels real orders) ──
python tests/run_nonkyc_tests.py

# ── FULL VALIDATION + CLEANUP STALE BOT ORDERS ──
python tests/run_nonkyc_tests.py --cleanup-all

# ── CONNECTOR UNIT TESTS (all 320, mostly offline) ──
cd E:\tradingsoftware\hummingbot
python -m pytest test/hummingbot/connector/exchange/nonkyc/ -v

# ── AUTH DIAGNOSTIC ──
python test/hummingbot/connector/exchange/nonkyc/nonkyc_auth_test.py
```
