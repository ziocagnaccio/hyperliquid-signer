"""
AI Trend Bot — Hyperliquid (BTC / ETH / SOL)
Flow:  TradingView (1H indicators) -> n8n -> THIS app -> Hyperliquid
                                                 \-> Claude rates it -> Telegram

What this app does:
  1. Receives the indicators from TradingView (via n8n).
  2. Decides LONG / SHORT / NOTHING  (volatility-gated "indicators vote").
  3. Sizes the position off your LIVE account value:
        collateral = POSITION_PCT * min(current_equity, INITIAL_CAPITAL)
        -> shrinks when you're losing, never grows past the start when winning.
  4. Refuses to open a 2nd position on a coin you're already in.
  5. Places a 10x isolated order with TP 3% / SL 4%.

Auth: uses the official hyperliquid-python-sdk (correct EIP-712 wallet signing).
"""

import os
import math
from datetime import datetime

from flask import Flask, request, jsonify
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

app = Flask(__name__)

# ========================= CONFIG — edit these =========================
INITIAL_CAPITAL = 2000.0          # your starting budget, in USD
POSITION_PCT    = 0.15            # collateral per trade = 15% of (capped) equity
LEVERAGE        = 10              # 10x
TP_PCT          = 0.03            # take profit at +3%
SL_PCT          = 0.04            # stop loss at -4%
COINS           = ["BTC", "ETH", "SOL"]
VOL_LIMITS      = {"BTC": 1.3, "ETH": 1.6, "SOL": 2.0}   # skip if atr_pct above this
SCORE_TO_TRADE  = 3              # need 3 of 5 votes (raise to 4 = stricter)
SLIPPAGE        = 0.01           # max 1% slippage on the market entry
# =======================================================================

# These come from Render Environment Variables — NEVER put real keys in this file.
WALLET_KEY     = os.environ["HL_PRIVATE_KEY"]   # API/agent wallet private key
MAIN_ADDR      = os.environ["HL_WALLET_ADDR"]   # your main account address (0x...)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

_wallet  = Account.from_key(WALLET_KEY)
info     = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(_wallet, constants.MAINNET_API_URL, account_address=MAIN_ADDR)


# --------------------------- helpers ---------------------------
def get_equity_and_positions():
    """Returns (account_value_usd, {coin: signed_size}) for any open positions."""
    s = info.user_state(MAIN_ADDR)
    equity = float(s["marginSummary"]["accountValue"])
    open_coins = {}
    for p in s.get("assetPositions", []):
        pos = p.get("position", {})
        szi = float(pos.get("szi", 0) or 0)
        if szi != 0:
            open_coins[pos.get("coin")] = szi
    return equity, open_coins


def sz_decimals(coin):
    for a in info.meta()["universe"]:
        if a["name"] == coin:
            return int(a["szDecimals"])
    return 2


def round_px(coin, px):
    """Round a price to Hyperliquid's valid precision (max 5 significant figures)."""
    if px <= 0:
        return px
    sig = 5 - int(math.floor(math.log10(abs(px)))) - 1
    max_dec = 6 - sz_decimals(coin)
    decimals = max(0, min(sig, max_dec))
    return round(px, decimals)


def decide(d):
    """Volatility gate first, then the indicators vote. Returns (side, score)."""
    coin = d["symbol"]
    if d["atr_pct"] > VOL_LIMITS.get(coin, 2.0):
        return "NOTHING", 0

    bull = sum([
        d["ema20"] > d["ema50"],
        d["ema50"] > d["ema100"],
        d["macd_hist"] > 0,
        45 <= d["rsi"] <= 68,
        d["vol_ratio"] > 1.1,
    ])
    bear = sum([
        d["ema20"] < d["ema50"],
        d["ema50"] < d["ema100"],
        d["macd_hist"] < 0,
        32 <= d["rsi"] <= 55,
        d["vol_ratio"] > 1.1,
    ])

    if bull >= SCORE_TO_TRADE and bull > bear:
        return "LONG", int(bull)
    if bear >= SCORE_TO_TRADE and bear > bull:
        return "SHORT", int(bear)
    return "NOTHING", 0


def collateral_for(equity):
    """Shrinks when the account shrinks; never grows past the starting capital."""
    base = min(equity, INITIAL_CAPITAL)
    return round(POSITION_PCT * base, 2)


# --------------------------- routes ---------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(force=True, silent=True) or {}
    body = raw.get("body", raw)   # n8n sometimes nests the payload under "body"

    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        return jsonify({"status": "error", "reason": "unauthorized"}), 401

    try:
        sig = {
            "symbol":    str(body["symbol"]).upper(),
            "price":     float(body["price"]),
            "ema20":     float(body["ema20"]),
            "ema50":     float(body["ema50"]),
            "ema100":    float(body["ema100"]),
            "rsi":       float(body["rsi"]),
            "macd_hist": float(body["macd_hist"]),
            "vol_ratio": float(body["vol_ratio"]),
            "atr_pct":   float(body["atr_pct"]),
        }
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"status": "error", "reason": f"bad payload: {e}"}), 200

    coin = sig["symbol"]
    if coin not in COINS:
        return jsonify({"status": "ignored", "reason": f"{coin} not enabled"}), 200

    side, score = decide(sig)
    if side == "NOTHING":
        return jsonify({"status": "no_trade", "coin": coin,
                        "reason": "filters not met"}), 200

    equity, open_coins = get_equity_and_positions()
    if coin in open_coins:
        return jsonify({"status": "skipped", "coin": coin,
                        "reason": "already in a position on this coin"}), 200

    collateral = collateral_for(equity)
    price      = sig["price"]
    notional   = collateral * LEVERAGE
    size       = round(notional / price, sz_decimals(coin))
    if size <= 0:
        return jsonify({"status": "error", "reason": "size rounded to 0 — raise POSITION_PCT"}), 200

    is_buy = side == "LONG"
    if is_buy:
        tp = round_px(coin, price * (1 + TP_PCT))
        sl = round_px(coin, price * (1 - SL_PCT))
    else:
        tp = round_px(coin, price * (1 - TP_PCT))
        sl = round_px(coin, price * (1 + SL_PCT))

    try:
        exchange.update_leverage(LEVERAGE, coin, is_cross=False)   # isolated
        entry = exchange.market_open(coin, is_buy, size, None, SLIPPAGE)
        # TP and SL as reduce-only trigger orders on the opposite side
        exchange.order(coin, not is_buy, size, tp,
                       {"trigger": {"triggerPx": tp, "isMarket": False, "tpsl": "tp"}},
                       reduce_only=True)
        exchange.order(coin, not is_buy, size, sl,
                       {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}},
                       reduce_only=True)
    except Exception as e:
        return jsonify({"status": "error", "coin": coin, "reason": str(e)}), 200

    return jsonify({
        "status": "executed",
        "coin": coin,
        "side": side,
        "score": score,
        "entry_price": price,
        "collateral_usd": collateral,
        "leverage": LEVERAGE,
        "exposure_usd": round(notional, 2),
        "size": size,
        "tp": tp,
        "sl": sl,
        "account_equity": round(equity, 2),
        "equity_used_for_sizing": round(min(equity, INITIAL_CAPITAL), 2),
        "time": datetime.utcnow().isoformat()
    }), 200


@app.route("/status", methods=["GET"])
def status():
    try:
        equity, open_coins = get_equity_and_positions()
        return jsonify({
            "status": "running",
            "coins": COINS,
            "leverage": f"{LEVERAGE}x isolated",
            "initial_capital": INITIAL_CAPITAL,
            "position_pct": POSITION_PCT,
            "next_trade_collateral": collateral_for(equity),
            "account_equity": round(equity, 2),
            "open_positions": open_coins,
            "tp_pct": TP_PCT,
            "sl_pct": SL_PCT,
        })
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "ai-trend-bot"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
