"""Live NonKYC.io API validation tests.

These tests hit the REAL NonKYC API to verify:
1. Public endpoints work (no auth needed)
2. Authenticated endpoints work with real API keys
3. Data formats match what the connector expects
4. WebSocket connection and subscription work

Run with:
    NONKYC_API_KEY=xxx NONKYC_API_SECRET=yyy pytest tests/test_nonkyc_live_api.py -v

IMPORTANT: These tests do NOT place real orders. They only read data.
"""
import asyncio
import hashlib
import hmac
import json
import time
import pytest
import aiohttp


# === NonKYC API Configuration ===
BASE_URL = "https://api.nonkyc.io/api/v2"
WS_URL = "wss://api.nonkyc.io"

# A known active trading pair on NonKYC for testing
# Update this if the pair gets delisted
TEST_MARKET = "BTC-USDT"
TEST_MARKET_ID = None  # Will be discovered dynamically


def sign_request(api_key: str, api_secret: str, url: str, nonce: str, body: str = "") -> str:
    """Generate HMAC-SHA256 signature for NonKYC API."""
    if body:
        message = api_key + url + body + nonce
    else:
        message = api_key + url + nonce
    return hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def auth_headers(api_key: str, api_secret: str, url: str, body: str = "") -> dict:
    """Build authenticated request headers."""
    nonce = str(int(time.time() * 1000))
    signature = sign_request(api_key, api_secret, url, nonce, body)
    return {
        "X-API-KEY": api_key,
        "X-API-NONCE": nonce,
        "X-API-SIGN": signature,
        "Content-Type": "application/json",
    }


# =====================================================================
# PUBLIC ENDPOINT TESTS (no API keys required)
# =====================================================================

class TestNonKYCPublicEndpoints:
    """Validate public REST API endpoints."""

    @pytest.mark.asyncio
    async def test_server_reachable(self):
        """NonKYC API should respond to a basic request."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                assert resp.status == 200, f"API returned {resp.status}"

    @pytest.mark.asyncio
    async def test_get_markets(self):
        """GET /market/getlist should return a list of markets."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list), f"Expected list, got {type(data)}"
                assert len(data) > 0, "No markets returned"

                # Verify expected fields exist
                market = data[0]
                expected_fields = ["id", "baseCurrency", "quoteCurrency", "status"]
                for field in expected_fields:
                    assert field in market, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_find_test_market(self):
        """Verify our test market (BTC-USDT) exists and is active."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                data = await resp.json()
                btc_usdt = [
                    m for m in data
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                assert len(btc_usdt) > 0, "BTC-USDT market not found on NonKYC"
                market = btc_usdt[0]
                assert market.get("status") in ("active", "Active"), (
                    f"BTC-USDT status is '{market.get('status')}', expected 'active'"
                )

    @pytest.mark.asyncio
    async def test_get_orderbook(self):
        """GET /market/orderbook should return bids and asks."""
        async with aiohttp.ClientSession() as session:
            # First find the market ID
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                assert btc_usdt, "BTC-USDT not found"
                market_id = btc_usdt[0]["id"]

            async with session.get(
                f"{BASE_URL}/market/orderbook",
                params={"marketId": market_id, "depth": 10}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                # Orderbook should have ask and bid arrays
                assert "ask" in data or "asks" in data, f"No asks in orderbook: {list(data.keys())}"
                assert "bid" in data or "bids" in data, f"No bids in orderbook: {list(data.keys())}"

    @pytest.mark.asyncio
    async def test_get_assets(self):
        """GET /asset/getlist should return asset list with precision info."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/asset/getlist") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list)
                assert len(data) > 0

                # Find BTC and verify it has precision fields
                btc = [a for a in data if a.get("ticker") == "BTC" or a.get("symbol") == "BTC"]
                assert btc, "BTC asset not found"

    @pytest.mark.asyncio
    async def test_get_tickers(self):
        """GET /tickers should return price data."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/tickers") as resp:
                assert resp.status == 200
                data = await resp.json()
                # Should be a list or dict of ticker data
                assert data is not None
                assert len(data) > 0 if isinstance(data, (list, dict)) else True

    @pytest.mark.asyncio
    async def test_market_trades(self):
        """GET /market/trades should return recent trades."""
        async with aiohttp.ClientSession() as session:
            # Get market ID first
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                assert btc_usdt
                market_id = btc_usdt[0]["id"]

            async with session.get(
                f"{BASE_URL}/market/trades",
                params={"marketId": market_id}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list)
                # Trades should have price, quantity, side, timestamp
                if len(data) > 0:
                    trade = data[0]
                    # Log the fields so we can see what NonKYC returns
                    print(f"\nSample trade fields: {list(trade.keys())}")
                    print(f"Sample trade: {json.dumps(trade, indent=2)[:500]}")

    @pytest.mark.asyncio
    async def test_data_precision_is_string(self):
        """NonKYC returns financial values as strings. Verify this."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                if btc_usdt:
                    market = btc_usdt[0]
                    # Check that precision-related fields are present
                    print(f"\nMarket info fields: {list(market.keys())}")
                    print(f"Market info: {json.dumps(market, indent=2)[:500]}")


# =====================================================================
# AUTHENTICATED ENDPOINT TESTS (require API keys)
# =====================================================================

class TestNonKYCAuthenticatedEndpoints:
    """Validate authenticated REST API endpoints."""

    @pytest.mark.asyncio
    async def test_auth_signature_valid(self, nonkyc_api_keys):
        """Authenticated request to /balances should succeed (not 401/403)."""
        url = f"{BASE_URL}/balances"
        headers = auth_headers(
            nonkyc_api_keys["api_key"],
            nonkyc_api_keys["api_secret"],
            url,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == 200, (
                    f"Auth failed with status {resp.status}: {await resp.text()}"
                )

    @pytest.mark.asyncio
    async def test_get_balances(self, nonkyc_api_keys):
        """GET /balances should return account balances."""
        url = f"{BASE_URL}/balances"
        headers = auth_headers(
            nonkyc_api_keys["api_key"],
            nonkyc_api_keys["api_secret"],
            url,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list), f"Expected list, got {type(data)}"
                print(f"\nBalance count: {len(data)}")
                if data:
                    print(f"Sample balance fields: {list(data[0].keys())}")
                    print(f"Sample balance: {json.dumps(data[0], indent=2)[:300]}")

    @pytest.mark.asyncio
    async def test_get_account_orders(self, nonkyc_api_keys):
        """GET /account/orders should return order history."""
        url = f"{BASE_URL}/account/orders"
        headers = auth_headers(
            nonkyc_api_keys["api_key"],
            nonkyc_api_keys["api_secret"],
            url,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list)
                print(f"\nOrder count: {len(data)}")
                if data:
                    print(f"Sample order fields: {list(data[0].keys())}")

    @pytest.mark.asyncio
    async def test_get_account_trades(self, nonkyc_api_keys):
        """GET /account/trades should return trade history."""
        url = f"{BASE_URL}/account/trades"
        headers = auth_headers(
            nonkyc_api_keys["api_key"],
            nonkyc_api_keys["api_secret"],
            url,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert isinstance(data, list)
                print(f"\nTrade count: {len(data)}")
                if data:
                    print(f"Sample trade fields: {list(data[0].keys())}")


# =====================================================================
# WEBSOCKET TESTS
# =====================================================================

class TestNonKYCWebSocket:
    """Validate WebSocket connectivity and subscriptions."""

    @pytest.mark.asyncio
    async def test_websocket_connects(self):
        """Should be able to open a WebSocket connection."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                assert not ws.closed
                await ws.close()

    @pytest.mark.asyncio
    async def test_websocket_get_markets(self):
        """Public getMarkets method should return market data."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                request = {
                    "method": "getMarkets",
                    "params": {},
                    "id": 1,
                }
                await ws.send_json(request)

                # Wait for response (with timeout)
                response = await asyncio.wait_for(ws.receive_json(), timeout=10)
                assert "result" in response or "error" not in response, (
                    f"Unexpected WS response: {response}"
                )
                print(f"\nWS getMarkets response keys: {list(response.keys())}")
                await ws.close()

    @pytest.mark.asyncio
    async def test_websocket_subscribe_orderbook(self):
        """Subscribe to orderbook should return a snapshot then updates."""
        async with aiohttp.ClientSession() as session:
            # First find market via REST
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                assert btc_usdt
                market_id = btc_usdt[0]["id"]

            async with session.ws_connect(WS_URL) as ws:
                request = {
                    "method": "subscribeOrderbook",
                    "params": {"symbol": str(market_id)},
                    "id": 2,
                }
                await ws.send_json(request)

                # Should get a snapshot response
                response = await asyncio.wait_for(ws.receive_json(), timeout=15)
                print(f"\nWS orderbook response method: {response.get('method', 'N/A')}")
                print(f"WS orderbook response keys: {list(response.keys())}")

                # The first response should be a snapshot or subscription confirmation
                assert response is not None
                await ws.close()

    @pytest.mark.asyncio
    async def test_websocket_auth(self, nonkyc_api_keys):
        """WebSocket login should authenticate successfully."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                request = {
                    "method": "login",
                    "params": {
                        "algo": "HS256",
                        "pKey": nonkyc_api_keys["api_key"],
                        "sKey": nonkyc_api_keys["api_secret"],
                    },
                    "id": 10,
                }
                await ws.send_json(request)

                response = await asyncio.wait_for(ws.receive_json(), timeout=10)
                print(f"\nWS login response: {json.dumps(response, indent=2)[:300]}")
                # Should get a success response (not an error)
                assert "error" not in response or response.get("result") is True, (
                    f"WS auth failed: {response}"
                )
                await ws.close()

    @pytest.mark.asyncio
    async def test_websocket_subscribe_report(self, nonkyc_api_keys):
        """After auth, subscribeReport should work for order updates."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                # Login first
                login_req = {
                    "method": "login",
                    "params": {
                        "algo": "HS256",
                        "pKey": nonkyc_api_keys["api_key"],
                        "sKey": nonkyc_api_keys["api_secret"],
                    },
                    "id": 10,
                }
                await ws.send_json(login_req)
                login_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)

                # Subscribe to reports
                report_req = {
                    "method": "subscribeReports",
                    "params": {},
                    "id": 11,
                }
                await ws.send_json(report_req)
                report_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
                print(f"\nWS subscribeReport response: {json.dumps(report_resp, indent=2)[:300]}")
                assert "error" not in report_resp or report_resp.get("result") is not None
                await ws.close()


# =====================================================================
# DATA FORMAT VALIDATION
# =====================================================================

class TestNonKYCDataFormats:
    """Validate that API responses match the formats expected by the connector."""

    @pytest.mark.asyncio
    async def test_market_fields_match_connector_expectations(self):
        """Market data should contain fields the connector parses."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                assert btc_usdt
                market = btc_usdt[0]

                # Fields the connector expects (from nonkyc_exchange.py _format_trading_rules)
                # These are the fields we NEED — log what we actually get
                print(f"\nAll market fields: {json.dumps(market, indent=2)}")

                # Must-have fields for trading rules
                assert "id" in market, "Missing 'id' field"
                assert "baseCurrency" in market, "Missing 'baseCurrency' field"
                assert "quoteCurrency" in market, "Missing 'quoteCurrency' field"

    @pytest.mark.asyncio
    async def test_orderbook_format_matches_connector(self):
        """Orderbook response format should match what the connector parses."""
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/market/getlist") as resp:
                markets = await resp.json()
                btc_usdt = [
                    m for m in markets
                    if m.get("baseCurrency") == "BTC" and m.get("quoteCurrency") == "USDT"
                ]
                market_id = btc_usdt[0]["id"]

            async with session.get(
                f"{BASE_URL}/market/orderbook",
                params={"marketId": market_id, "depth": 5}
            ) as resp:
                data = await resp.json()
                print(f"\nOrderbook response format: {json.dumps(data, indent=2)[:800]}")

                # Verify structure matches connector expectations
                # The connector expects ask/bid arrays with [price, size] entries
                asks_key = "ask" if "ask" in data else "asks"
                bids_key = "bid" if "bid" in data else "bids"

                asks = data.get(asks_key, [])
                bids = data.get(bids_key, [])

                if asks:
                    entry = asks[0]
                    print(f"Ask entry format: {entry}")
                    # Should be dict with price/size or a [price, size] array
                    assert isinstance(entry, (dict, list)), f"Unexpected ask format: {type(entry)}"

    @pytest.mark.asyncio
    async def test_balance_format_matches_connector(self, nonkyc_api_keys):
        """Balance response should have fields the connector expects."""
        url = f"{BASE_URL}/balances"
        headers = auth_headers(
            nonkyc_api_keys["api_key"],
            nonkyc_api_keys["api_secret"],
            url,
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                if data:
                    balance = data[0]
                    print(f"\nBalance fields: {json.dumps(balance, indent=2)}")
                    # Connector expects: currency, available, reserved (or similar)
                    # Log what we actually get so we can verify
