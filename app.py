"""
AI Trend Bot — Hyperliquid (BTC / ETH)
Flow:  TradingView (indicators) -> n8n -> THIS app -> Hyperliquid -> Telegram
Timeframe is set in TradingView, NOT here.

Exits on a position:
  - Take profit at +4% (hard ceiling)
  - Stop loss at -3% (hard floor)
  - TRAILING take profit: locks in profit if price gives back from its best point

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
INITIAL_CAPITAL = 1000.0          # your starting budget, in USD
POSITION_PCT    = 0.15            # collateral per trade = 15% of (capped) equity
LEVERAGE        = 10              # 10x
TP_PCT          = 0.04            # take profit at +4%
SL_PCT          = 0.03            # stop loss at -3%
COINS           = ["BTC", "ETH"]
VOL_LIMITS      = {"BTC": 1.3, "ETH": 1.6}   # skip if atr_pct above this
SCORE_TO_TRADE  = 3              # need 3 of 5 votes (raise to 4 = stricter)
SLIPPAGE        = 0.01           # max 1% slippage on the market entry

# --- Trailing take profit (checked by /manage on a timer) ---
TRAIL_ACTIVATE  = 0.015          # arm trailing once price moved +1.5% in your favor
TRAIL_GIVEBACK  = 0.007          # then close if it gives back 0.7% from the best point
# =======================================================================

# These come from Render Environment Variables — NEVER put real keys in this file.
WALLET_KEY     = os.environ["HL_PRIVATE_KEY"]   # API/agent wallet private key
MAIN_ADDR      = os.environ["HL_WALLET_ADDR"]   # your main account address (0x...)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

_wallet  = Account.from_key(WALLET_KEY)
info     = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(_wallet, constants.MAINNET_API_URL, account_address=MAIN_ADDR)

# Remembers the best profit % reached per coin, so trailing can measure the give-back.
peaks = {}


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


def cancel_coin_orders(coin):
    """Cancel any leftover TP/SL trigger orders for a coin."""
    try:
        for o in info.open_orders(MAIN_ADDR):
            if o.get("coin") == coin:
                exchange.cancel(coin, o["oid"])
    except Exception as e:
        print(f"[cancel] {coin}: {e}")


def close_position(coin):
    """Market-close the position on a coin, then clean up its TP/SL orders."""
    exchange.market_close(coin)
    cancel_coin_orders(coin)


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
        # one position per coin — don't stack or flip, just wait for it to close
        current_side = "LONG" if open_coins[coin] > 0 else "SHORT"
        return jsonify({"status": "skipped", "coin": coin,
                        "reason": f"already {current_side} on this coin"}), 200

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

    # fresh position -> reset any old peak so trailing starts clean
    peaks.pop(coin, None)

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


@app.route("/manage", methods=["GET"])
def manage():
    """Called every few minutes (by UptimeRobot). Runs the trailing take-profit:
    remembers each position's best profit, and closes it if it gives back too much."""
    try:
        s = info.user_state(MAIN_ADDR)
        mids = info.all_mids()
        closes = []
        open_now = set()

        for p in s.get("assetPositions", []):
            pos = p.get("position", {})
            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue
            coin = pos.get("coin")
            open_now.add(coin)
            entry = float(pos.get("entryPx", 0) or 0)
            mark = float(mids.get(coin, 0) or 0)
            if entry <= 0 or mark <= 0:
                continue

            # favorable price move (before leverage), matching how TP/SL are defined
            if szi > 0:            # LONG
                profit = (mark - entry) / entry
            else:                  # SHORT
                profit = (entry - mark) / entry

            peak = max(peaks.get(coin, profit), profit)
            peaks[coin] = peak

            # armed once it reached the activation level; close if it gives back enough
            if peak >= TRAIL_ACTIVATE and (peak - profit) >= TRAIL_GIVEBACK:
                try:
                    close_position(coin)
                    peaks.pop(coin, None)
                    closes.append({
                        "coin": coin,
                        "closed_at_pct": round(profit * 100, 2),
                        "peak_pct": round(peak * 100, 2),
                    })
                except Exception as e:
                    print(f"[trail] close {coin} failed: {e}")

        # forget peaks for coins that are no longer open
        for c in list(peaks.keys()):
            if c not in open_now:
                peaks.pop(c, None)

        return jsonify({
            "status": "managed",
            "trailing_closes": closes,
            "tracked": {k: round(v * 100, 2) for k, v in peaks.items()},
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


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
