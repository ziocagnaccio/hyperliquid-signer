"""
ETH 15m Scalping Bot — Hyperliquid
Stack: TradingView -> n8n -> this app -> Hyperliquid
Leverage: 7x | Capital: $500 | Both LONG & SHORT
Orders: LIMIT (maker fee 0.02% vs taker 0.05%) -- 60% cheaper
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

# ---------------------------------------------
# CONFIG
# ---------------------------------------------
HL_API_KEY       = os.environ.get("HL_API_KEY")
HL_API_SECRET    = os.environ.get("HL_API_SECRET")
HL_WALLET_ADDR   = os.environ.get("HL_WALLET_ADDR")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "your_webhook_secret")

HL_BASE_URL      = "https://api.hyperliquid.xyz"
SYMBOL           = "ETH"
LEVERAGE         = 7
CAPITAL          = 500.0
RISK_PCT         = 0.15       # 15% of $500 capital = $75 per trade
LIMIT_OFFSET_PCT = 0.001       # 0.1% offset for maker fee
FILL_TIMEOUT_SEC = 30


# ---------------------------------------------
# HELPERS
# ---------------------------------------------
def get_timestamp():
    return int(time.time() * 1000)


def sign_request(secret, data):
    message = json.dumps(data, separators=(',', ':'))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def get_eth_price():
    resp = requests.post(f"{HL_BASE_URL}/info", json={"type": "allMids"})
    mids = resp.json()
    return float(mids.get("ETH", 0))


def get_open_position():
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
    payload = {
        "type": "openOrders",
        "user": HL_WALLET_ADDR
    }
    resp = requests.post(f"{HL_BASE_URL}/info", json=payload)
    return resp.json()


def cancel_order(order_id):
    timestamp = get_timestamp()
    payload = {
        "action": {
            "type": "cancel",
            "cancels": [
                {"a": 4, "o": order_id}
            ]
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[CANCEL] Order {order_id} | Response: {resp.json()}")


def set_leverage():
    timestamp = get_timestamp()
    action = {
        "type": "updateLeverage",
        "asset": 4,
        "isCross": False,
        "leverage": LEVERAGE
    }
    payload = {
        "action": action,
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[LEVERAGE] Set to {LEVERAGE}x | Response: {resp.json()}")


def place_sl_tp(is_buy, size, sl, tp, nonce):
    sl_order = {
        "a": 4,
        "b": not is_buy,
        "p": str(round(sl, 2)),
        "s": str(size),
        "r": True,
        "t": {
            "trigger": {
                "isMarket": True,
                "tpsl": "sl",
                "triggerPx": str(round(sl, 2))
            }
        }
    }
    tp_order = {
        "a": 4,
        "b": not is_buy,
        "p": str(round(tp, 2)),
        "s": str(size),
        "r": True,
        "t": {
            "trigger": {
                "isMarket": False,
                "tpsl": "tp",
                "triggerPx": str(round(tp, 2))
            }
        }
    }
    payload = {
        "action": {
            "type": "order",
            "orders": [sl_order, tp_order],
            "grouping": "normalTpsl"
        },
        "nonce": nonce,
        "signature": sign_request(HL_API_SECRET, {"nonce": nonce})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[SL/TP] SL=${sl} TP=${tp} | Response: {resp.json()}")


def place_limit_order(side, size_usd, signal_price, sl, tp):
    is_buy = side == "LONG"
    eth_size = round(size_usd / signal_price, 4)

    if is_buy:
        limit_price = round(signal_price * (1 - LIMIT_OFFSET_PCT), 2)
    else:
        limit_price = round(signal_price * (1 + LIMIT_OFFSET_PCT), 2)

    print(f"[LIMIT ORDER] {side} {eth_size} ETH @ ${limit_price} (signal was ${signal_price})")

    set_leverage()
    time.sleep(0.5)

    timestamp = get_timestamp()
    order = {
        "a": 4,
        "b": is_buy,
        "p": str(limit_price),
        "s": str(eth_size),
        "r": False,
        "t": {
            "limit": {
                "tif": "Gtc"
            }
        }
    }
    payload = {
        "action": {
            "type": "order",
            "orders": [order],
            "grouping": "na"
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    result = resp.json()
    print(f"[ORDER PLACED] {result}")

    if result.get("status") != "ok":
        return {"success": False, "result": result, "reason": "Order placement failed"}

    order_id = None
    try:
        order_id = result["response"]["data"]["statuses"][0]["resting"]["oid"]
        print(f"[ORDER ID] {order_id} — waiting up to {FILL_TIMEOUT_SEC}s for fill...")
    except (KeyError, IndexError):
        print("[WARNING] Could not extract order ID")

    # Wait for fill
    filled = False
    waited = 0
    check_interval = 3

    while waited < FILL_TIMEOUT_SEC:
        time.sleep(check_interval)
        waited += check_interval
        position = get_open_position()
        if position and position["side"] == side:
            filled = True
            print(f"[FILLED] {side} confirmed after {waited}s @ ${position['entry_price']}")
            break
        print(f"[WAITING] {waited}s elapsed...")

    if not filled:
        if order_id:
            cancel_order(order_id)
        print(f"[TIMEOUT] Not filled in {FILL_TIMEOUT_SEC}s — cancelled")
        return {
            "success": False,
            "result": result,
            "reason": f"Not filled within {FILL_TIMEOUT_SEC}s"
        }

    actual = get_open_position()
    actual_size = actual["size"] if actual else eth_size
    place_sl_tp(is_buy, actual_size, sl, tp, get_timestamp())

    return {
        "success": True,
        "result": result,
        "limit_price": limit_price,
        "filled_after_seconds": waited,
        "fee_rate": "0.02% maker"
    }


def close_position(side, size):
    is_buy = side == "SHORT"
    timestamp = get_timestamp()
    order = {
        "a": 4,
        "b": is_buy,
        "p": "0",
        "s": str(size),
        "r": True,
        "t": {
            "limit": {
                "tif": "Ioc"
            }
        }
    }
    payload = {
        "action": {
            "type": "order",
            "orders": [order],
            "grouping": "na"
        },
        "nonce": timestamp,
        "signature": sign_request(HL_API_SECRET, {"nonce": timestamp})
    }
    headers = {"Content-Type": "application/json", "HL-API-KEY": HL_API_KEY}
    resp = requests.post(f"{HL_BASE_URL}/exchange", json=payload, headers=headers)
    print(f"[CLOSE] {side} closed | Response: {resp.json()}")


# ---------------------------------------------
# ROUTES
# ---------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    side   = data.get("side", "").upper()
    symbol = data.get("symbol", "")
    price  = float(data.get("price", 0))
    sl     = float(data.get("sl", 0))
    tp     = float(data.get("tp", 0))
    atr    = float(data.get("atr", 0))
    rsi    = float(data.get("rsi", 0))
    adx    = float(data.get("adx", 0))

    print(f"\n{'='*50}")
    print(f"[SIGNAL] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {side} {symbol} @ ${price}")
    print(f"  SL: ${sl} | TP: ${tp}")
    print(f"  ATR: {round(atr,2)} | RSI: {round(rsi,1)} | ADX: {round(adx,1)}")
    print(f"{'='*50}")

    if side not in ["LONG", "SHORT"]:
        return jsonify({"error": f"Invalid side: {side}"}), 400
    if symbol != SYMBOL:
        return jsonify({"error": f"Wrong symbol: {symbol}"}), 400
    if price <= 0 or sl <= 0 or tp <= 0:
        return jsonify({"error": "Invalid price/sl/tp"}), 400

    existing = get_open_position()
    if existing:
        print(f"[SKIP] Already in {existing['side']} — skipping")
        return jsonify({
            "status": "skipped",
            "reason": f"Already in {existing['side']} position",
            "existing": existing
        }), 200

    position_usd = CAPITAL * RISK_PCT  # $37.50
    print(f"[EXECUTING] {side} ${position_usd} x {LEVERAGE}x = ${position_usd * LEVERAGE} exposure")

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
        }), 200


@app.route("/status", methods=["GET"])
def status():
    position = get_open_position()
    price = get_eth_price()
    return jsonify({
        "status": "running",
        "symbol": SYMBOL,
        "leverage": f"{LEVERAGE}x",
        "capital": f"${CAPITAL}",
        "per_trade": f"${CAPITAL * RISK_PCT}",
        "exposure_per_trade": f"${CAPITAL * RISK_PCT * LEVERAGE}",
        "order_type": "limit (maker 0.02%)",
        "eth_price": f"${price}",
        "open_position": position,
        "timestamp": datetime.now().isoformat()
    })


@app.route("/close", methods=["POST"])
def manual_close():
    position = get_open_position()
    if not position:
        return jsonify({"status": "no_open_position"}), 200
    close_position(position["side"], position["size"])
    return jsonify({"status": "closed", "was": position})


@app.route("/orders", methods=["GET"])
def open_orders():
    orders = get_open_orders()
    return jsonify({"open_orders": orders})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nETH Scalping Bot")
    print(f"  Symbol:    {SYMBOL}")
    print(f"  Leverage:  {LEVERAGE}x isolated")
    print(f"  Per trade: ${CAPITAL * RISK_PCT} (${CAPITAL * RISK_PCT * LEVERAGE} exposure)")
    print(f"  Orders:    LIMIT at {LIMIT_OFFSET_PCT*100}% offset (maker 0.02%)")
    print(f"  Timeout:   {FILL_TIMEOUT_SEC}s\n")
    app.run(host="0.0.0.0", port=port, debug=False)
