"""
ETH 15m Scalping Bot — Hyperliquid
Stack: TradingView → n8n → this app → Hyperliquid
Leverage: 7x | Capital: $500 | Both LONG & SHORT
Orders: LIMIT (maker fee 0.02% vs taker 0.05%) — 60% cheaper
"""
import os
import json
import hmac
import hashlib
import time
import requests
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG — set these as environment variables on Render
# ─────────────────────────────────────────────
HL_API_KEY      = os.environ.get("HL_API_KEY")
HL_API_SECRET   = os.environ.get("HL_API_SECRET")
HL_WALLET_ADDR  = os.environ.get("HL_WALLET_ADDR")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET", "your_webhook_secret")

HL_BASE_URL     = "https://api.hyperliquid.xyz"
SYMBOL          = "ETH"
LEVERAGE        = 7
CAPITAL         = 500.0
RISK_PCT        = 0.075       # 7.5% of capital per trade = $75 position
MAX_OPEN_TRADES = 1           # Only 1 position at a time

# Limit order offset — place order slightly better than market to get maker fee
# 0.1% below market for LONG (we bid lower = maker)
# 0.1% above market for SHORT (we offer higher = maker)
LIMIT_OFFSET_PCT = 0.001      # 0.1% offset = maker fee (0.02%) not taker (0.05%)

# If limit order not filled after this many seconds → cancel and skip trade
FILL_TIMEOUT_SEC = 30


# ─────────────────────────────────────────────
# HYPERLIQUID HELPERS
# ─────────────────────────────────────────────
def get_timestamp():
    return int(time.time() * 1000)

def sign_request(secret, data):
    message = json.dumps(data, separators=(',', ':'))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def get_eth_price():
    """Fetch current ETH mid price from Hyperliquid."""
    resp = requests.post(f"{HL_BASE_URL}/info", json={"type": "allMids"})
    mids = resp.json()
    return float(mids.get("ETH", 0))

def get_open_position():
    """Check if we already have an open ETH position."""
    payload = {
        "type": "clearinghouseState",
        "user": HL_WALLET_ADDR
    }
    resp = requests.post(f"{HL_BASE_URL}/info", json=payload)
    data = resp.json()
    positions = data.get("assetPositions", [])
    for p in positions:
        pos = p.get("position", {})
        if pos.get("coin") == SYMBOL:
            szi = float(pos.get("szi", 0))
            if szi != 0:
                return {
                    "side": "LONG" if szi > 0 else "SHORT",
                    "size": abs(szi),
                    "entry_price": float(pos.get("entryPx", 0))
                }
    return None

def get_open_orders():
    """Get all open orders for this wallet."""
    payload = {
        "type": "openOrders",
        "user": HL_WALLET_ADDR
    }
    resp = requests.post(f"{HL_BASE_URL}/info", json=payload)
    return resp.json()

def cancel_order(order_id: int):
    """Cancel a specific open order."""
    timestamp = get_timestamp()
    payload = {
        "action": {
            "type": "cancel",
            "cancels": [{"a": 4, "o": order_id}]
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[CANCEL] Order {order_id} | Response: {resp.json()}")

def set_leverage():
    """Set leverage to 5x isolated for ETH."""
    timestamp = get_timestamp()
    payload = {
        "action": {
            "type": "updateLeverage",
            "asset": 4,        # ETH asset index on Hyperliquid
            "isCross": False,  # Isolated margin
            "leverage": LEVERAGE  # 7x isolated
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[LEVERAGE] Set to {LEVERAGE}x | Response: {resp.json()}")


def place_limit_order(side: str, size_usd: float, signal_price: float, sl: float, tp: float):
    """
    Place a LIMIT order (maker fee = 0.02%) instead of market order (taker = 0.05%).

    For LONG:  place limit buy slightly BELOW current price → maker fee
    For SHORT: place limit sell slightly ABOVE current price → maker fee

    If not filled within FILL_TIMEOUT_SEC → cancel and skip trade.
    Fee saving: 0.05% taker → 0.02% maker = 60% cheaper per trade.
    """
    is_buy = side == "LONG"
    eth_size = round(size_usd / signal_price, 4)

    # Calculate limit price with offset
    # LONG:  bid 0.1% below signal price (we want to buy cheaper = maker)
    # SHORT: offer 0.1% above signal price (we want to sell higher = maker)
    if is_buy:
        limit_price = round(signal_price * (1 - LIMIT_OFFSET_PCT), 2)
    else:
        limit_price = round(signal_price * (1 + LIMIT_OFFSET_PCT), 2)

    print(f"[LIMIT ORDER] {side} {eth_size} ETH")
    print(f"  Signal price: ${signal_price} | Limit price: ${limit_price}")
    print(f"  Offset: {LIMIT_OFFSET_PCT*100}% | Fee: maker 0.02%")

    # Set leverage first
    set_leverage()
    time.sleep(0.5)

    timestamp = get_timestamp()
    payload = {
        "action": {
            "type": "order",
            "orders": [
                {
                    "a": 4,                          # ETH asset index
                    "b": is_buy,                     # True = buy, False = sell
                    "p": str(limit_price),           # Limit price (not 0 like market)
                    "s": str(eth_size),              # Size in ETH
                    "r": False,                      # Not reduce-only
                    "t": {
                        "limit": {
                            "tif": "Gtc"             # Good Till Cancel (stays open)
                        }
                    }
                }
            ],
            "grouping": "na"
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }

    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    result = resp.json()
    print(f"[ORDER PLACED] Response: {result}")

    if result.get("status") != "ok":
        return {"success": False, "result": result, "reason": "Order placement failed"}

    # Extract order ID from response
    order_id = None
    try:
        order_id = result["response"]["data"]["statuses"][0]["resting"]["oid"]
        print(f"[ORDER ID] {order_id} — waiting up to {FILL_TIMEOUT_SEC}s for fill...")
    except (KeyError, IndexError):
        print("[WARNING] Could not extract order ID — assuming filled immediately")

    # ── Wait for fill ──────────────────────────────────────────────
    filled = False
    waited = 0
    check_interval = 3  # check every 3 seconds

    while waited < FILL_TIMEOUT_SEC:
        time.sleep(check_interval)
        waited += check_interval

        position = get_open_position()
        if position and position["side"] == side:
            filled = True
            print(f"[FILLED] ✓ {side} position confirmed after {waited}s")
            print(f"  Entry: ${position['entry_price']} | Size: {position['size']} ETH")
            break

        print(f"[WAITING] {waited}s — not filled yet...")

    if not filled:
        # Cancel the unfilled order and skip trade
        if order_id:
            cancel_order(order_id)
        print(f"[TIMEOUT] Order not filled in {FILL_TIMEOUT_SEC}s — cancelled. Skipping trade.")
        return {
            "success": False,
            "result": result,
            "reason": f"Not filled within {FILL_TIMEOUT_SEC}s — limit price ${limit_price} may be too far from market"
        }

    # ── Place SL and TP now that we're filled ─────────────────────
    actual_position = get_open_position()
    actual_size = actual_position["size"] if actual_position else eth_size
    place_sl_tp(is_buy, actual_size, sl, tp, get_timestamp())

    return {
        "success": True,
        "result": result,
        "limit_price": limit_price,
        "filled_after_seconds": waited,
        "fee_rate": "0.02% maker"
    }


def place_sl_tp(is_buy: bool, size: float, sl: float, tp: float, nonce: int):
    """Place stop-loss and take-profit as reduce-only orders."""
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}

    orders = [
        # Stop Loss — triggers as market when price hits SL level
        {
            "a": 4,
            "b": not is_buy,
            "p": str(round(sl, 2)),
            "s": str(size),
            "r": True,
            "t": {"trigger": {"isMarket": True, "tpsl": "sl", "triggerPx": str(round(sl, 2))}}
        },
        # Take Profit — limit order at TP level
        {
            "a": 4,
            "b": not is_buy,
            "p": str(round(tp, 2)),
            "s": str(size),
            "r": True,
            "t": {"trigger": {"isMarket": False, "tpsl": "tp", "triggerPx": str(round(tp, 2))}}
        }
    ]

    payload = {
        "action": {
            "type": "order",
            "orders": orders,
            "grouping": "normalTpsl"
        },
        "nonce": nonce,
        "signature": sign_request(HL_API_SECRET, {"nonce": nonce})
    }

    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[SL/TP SET] SL=${sl} TP=${tp} | Response: {resp.json()}")


def close_position(side: str, size: float):
    """Close an open position with a market order (speed > fees for closing)."""
    is_buy = side == "SHORT"
    timestamp = get_timestamp()

    payload = {
        "action": {
            "type": "order",
            "orders": [
                {
                    "a": 4,
                    "b": is_buy,
                    "p": "0",
                    "s": str(size),
                    "r": True,
                    "t": {"limit": {"tif": "Ioc"}}
                }
            ],
            "grouping": "na"
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }

    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[CLOSE] {side} position closed | Response: {resp.json()}")


# ─────────────────────────────────────────────
# WEBHOOK — receives signal from n8n
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives signal from n8n (originally from TradingView Pine Script).
    Places a LIMIT order at 0.1% offset to get maker fee (0.02% vs 0.05%).
    Waits up to 30 seconds for fill, then cancels if not filled.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    # Security check
    if data.get("secret") != WEBHOOK_SECRET:
        print("[SECURITY] Invalid webhook secret — rejected")
        return jsonify({"error": "Unauthorized"}), 401

    side   = data.get("side", "").upper()
    symbol = data.get("symbol", "")
    price  = float(data.get("price", 0))
    sl     = float(data.get("sl", 0))
    tp     = float(data.get("tp", 0))
    atr    = float(data.get("atr", 0))
    rsi    = float(data.get("rsi", 0))
    adx    = float(data.get("adx", 0))

    print(f"\n{'='*55}")
    print(f"[SIGNAL] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {side} {symbol} @ ${price}")
    print(f"  SL: ${sl} | TP: ${tp}")
    print(f"  ATR: {round(atr,2)} | RSI: {round(rsi,1)} | ADX: {round(adx,1)}")
    print(f"{'='*55}")

    # Validate
    if side not in ["LONG", "SHORT"]:
        return jsonify({"error": f"Invalid side: {side}"}), 400
    if symbol != SYMBOL:
        return jsonify({"error": f"Wrong symbol: {symbol}"}), 400
    if price <= 0 or sl <= 0 or tp <= 0:
        return jsonify({"error": "Invalid price/sl/tp values"}), 400

    # No overlapping trades
    existing = get_open_position()
    if existing:
        print(f"[SKIP] Already in {existing['side']} position — ignoring signal")
        return jsonify({
            "status": "skipped",
            "reason": f"Already in {existing['side']} position",
            "existing": existing
        }), 200

    # Position size
    position_usd = CAPITAL * RISK_PCT  # $100
    print(f"[EXECUTING] {side} ${position_usd} × {LEVERAGE}x = ${position_usd * LEVERAGE} exposure")

    # Place limit order (maker fee)
    result = place_limit_order(side, position_usd, price, sl, tp)

    if result["success"]:
        return jsonify({
            "status": "executed",
            "order_type": "limit",
            "fee_rate": "0.02% maker",
            "side": side,
            "position_usd": position_usd,
            "leverage": LEVERAGE,
            "exposure_usd": position_usd * LEVERAGE,
            "limit_price": result.get("limit_price"),
            "sl": sl,
            "tp": tp,
            "filled_after_seconds": result.get("filled_after_seconds")
        }), 200
    else:
        return jsonify({
            "status": "failed",
            "reason": result.get("reason"),
            "result": result.get("result")
        }), 200  # 200 not 500 — failed trades are expected sometimes, not errors


@app.route("/status", methods=["GET"])
def status():
    """Health check + current open position."""
    position = get_open_position()
    price = get_eth_price()
    return jsonify({
        "status": "running",
        "symbol": SYMBOL,
        "leverage": f"{LEVERAGE}x",
        "capital": f"${CAPITAL}",
        "position_size": f"${CAPITAL * RISK_PCT} per trade",
        "order_type": "limit (maker 0.02%)",
        "eth_price": f"${price}",
        "open_position": position,
        "timestamp": datetime.now().isoformat()
    })


@app.route("/close", methods=["POST"])
def manual_close():
    """Manually close open position (emergency use)."""
    position = get_open_position()
    if not position:
        return jsonify({"status": "no_open_position"}), 200
    close_position(position["side"], position["size"])
    return jsonify({"status": "closed", "was": position})


@app.route("/orders", methods=["GET"])
def open_orders():
    """Show all open orders (useful to check pending limit orders)."""
    orders = get_open_orders()
    return jsonify({"open_orders": orders})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 ETH Scalping Bot")
    print(f"   Symbol:    {SYMBOL}")
    print(f"   Leverage:  {LEVERAGE}x isolated")
    print(f"   Per trade: ${CAPITAL * RISK_PCT} (${CAPITAL * RISK_PCT * LEVERAGE} exposure)")
    print(f"   Orders:    LIMIT at {LIMIT_OFFSET_PCT*100}% offset (maker fee 0.02%)")
    print(f"   Fill wait: {FILL_TIMEOUT_SEC}s max\n")
    app.run(host="0.0.0.0", port=port, debug=False)
