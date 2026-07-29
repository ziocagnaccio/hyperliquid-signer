"""
AI Trend Bot — Hyperliquid (BTC / ETH / SOL)
Flow:  TradingView (indicators + S/R) -> n8n -> THIS app -> Hyperliquid -> Telegram
Timeframe is set in TradingView, NOT here.

FIX in this version: the entry now WAITS until the position is confirmed filled
before attaching TP/SL, and clears any leftover orders first. This stops the
"canceled due to reduce-only" problem that was closing positions early.

Position size by conviction score: 5/5 -> $200, 4/5 -> $175, 3/5 -> $150. Fade -> $200.
Exits: TP +4% | SL -3% | trailing take profit.
Entry guards: cooldown + re-entry distance + support/resistance + fade.

Auth: uses the official hyperliquid-python-sdk (correct EIP-712 wallet signing).
"""
import os
import time
import math
from datetime import datetime
from flask import Flask, request, jsonify
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

app = Flask(__name__)

# ========================= CONFIG — edit these =========================
INITIAL_CAPITAL = 1000.0
LEVERAGE        = 10
TP_PCT          = 0.04
SL_PCT          = 0.03
COINS           = ["BTC", "ETH", "SOL"]
VOL_LIMITS      = {"BTC": 1.3, "ETH": 1.6, "SOL": 2.0}
SCORE_TO_TRADE  = 3
SLIPPAGE        = 0.01

SCORE_COLLATERAL = {3: 150.0, 4: 175.0, 5: 200.0}
FADE_COLLATERAL  = 200.0

TRAIL_ACTIVATE  = 0.015
TRAIL_GIVEBACK  = 0.007

COOLDOWN_HOURS     = 2
REENTRY_MIN_PCT    = 0.01
REENTRY_ATR_MULT   = 0.75
SR_ATR_BUFFER      = 1.5

SR_STRONG_TOUCHES    = 2
SR_FADE_ATR_MULT     = 1.0
SR_FADE_SL_ATR_MULT  = 0.3

# how long to wait for the entry to confirm filled before attaching TP/SL
FILL_WAIT_TRIES     = 12     # number of checks
FILL_WAIT_SECONDS   = 0.5    # pause between checks (12 x 0.5s = up to 6s)
# =======================================================================

WALLET_KEY     = os.environ["HL_PRIVATE_KEY"]
MAIN_ADDR      = os.environ["HL_WALLET_ADDR"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

_wallet  = Account.from_key(WALLET_KEY)
info     = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(_wallet, constants.MAINNET_API_URL, account_address=MAIN_ADDR)

peaks = {}
known_open = {}
last_close = {}

# --------------------------- helpers ---------------------------

def get_equity_and_positions():
    s = info.user_state(MAIN_ADDR)
    equity = float(s["marginSummary"]["accountValue"])
    open_coins = {}
    for p in s.get("assetPositions", []):
        pos = p.get("position", {})
        szi = float(pos.get("szi", 0) or 0)
        if szi != 0:
            open_coins[pos.get("coin")] = szi
    return equity, open_coins


def update_close_tracking(open_coins):
    global known_open
    mids = None
    for coin in COINS:
        was_open = coin in known_open
        is_open = coin in open_coins
        if was_open and not is_open:
            try:
                mids = mids if mids is not None else info.all_mids()
                px = float(mids.get(coin, 0) or 0)
            except Exception:
                px = 0.0
            last_close[coin] = {"price": px, "time": datetime.utcnow(), "side": known_open[coin]}
    known_open = {c: ("LONG" if szi > 0 else "SHORT") for c, szi in open_coins.items()}


def blocked_by_cooldown_or_price(coin, price, atr_pct):
    lc = last_close.get(coin)
    if not lc or lc["price"] <= 0:
        return None
    hours_since = (datetime.utcnow() - lc["time"]).total_seconds() / 3600.0
    if hours_since < COOLDOWN_HOURS:
        return f"cooldown active ({hours_since:.2f}h < {COOLDOWN_HOURS}h since last close)"
    atr_abs = (atr_pct / 100.0) * price
    required_move = max(REENTRY_MIN_PCT * price, REENTRY_ATR_MULT * atr_abs)
    moved = abs(price - lc["price"])
    if moved < required_move:
        return (f"price hasn't moved enough since last close "
                f"({moved:.2f} < required {required_move:.2f}, "
                f"= max({REENTRY_MIN_PCT*100:.1f}% of price, {REENTRY_ATR_MULT}xATR))")
    return None


def blocked_by_level(side, price, atr_pct, resistance, support):
    atr_abs = (atr_pct / 100.0) * price
    if atr_abs <= 0:
        return None
    if side == "LONG" and resistance and resistance > 0 and price < resistance:
        if (resistance - price) < SR_ATR_BUFFER * atr_abs:
            return f"too close to resistance {resistance} (within {SR_ATR_BUFFER}xATR)"
    if side == "SHORT" and support and support > 0 and price > support:
        if (price - support) < SR_ATR_BUFFER * atr_abs:
            return f"too close to support {support} (within {SR_ATR_BUFFER}xATR)"
    return None


def plan_fade_trade(coin, price, atr_pct, resistance, res_touches, support, sup_touches):
    atr_abs = (atr_pct / 100.0) * price
    if atr_abs <= 0:
        return None
    if support and support > 0 and sup_touches >= SR_STRONG_TOUCHES and price > support:
        if (price - support) < SR_FADE_ATR_MULT * atr_abs:
            sl = support - SR_FADE_SL_ATR_MULT * atr_abs
            sl = max(sl, price * (1 - SL_PCT))
            tp = min(resistance, price * (1 + TP_PCT)) if (resistance and resistance > price) else price * (1 + TP_PCT)
            return {"side": "LONG", "sl": sl, "tp": tp, "touches": sup_touches, "level": support}
    if resistance and resistance > 0 and res_touches >= SR_STRONG_TOUCHES and price < resistance:
        if (resistance - price) < SR_FADE_ATR_MULT * atr_abs:
            sl = resistance + SR_FADE_SL_ATR_MULT * atr_abs
            sl = min(sl, price * (1 + SL_PCT))
            tp = max(support, price * (1 - TP_PCT)) if (support and support < price) else price * (1 - TP_PCT)
            return {"side": "SHORT", "sl": sl, "tp": tp, "touches": res_touches, "level": resistance}
    return None


def sz_decimals(coin):
    for a in info.meta()["universe"]:
        if a["name"] == coin:
            return int(a["szDecimals"])
    return 2


def round_px(coin, px):
    if px <= 0:
        return px
    sig = 5 - int(math.floor(math.log10(abs(px)))) - 1
    max_dec = 6 - sz_decimals(coin)
    decimals = max(0, min(sig, max_dec))
    return round(px, decimals)


def decide(d):
    coin = d["symbol"]
    if d["atr_pct"] > VOL_LIMITS.get(coin, 2.0):
        return "NOTHING", 0
    bull = sum([
        d["ema20"] > d["ema50"], d["ema50"] > d["ema100"], d["macd_hist"] > 0,
        45 <= d["rsi"] <= 68, d["vol_ratio"] > 1.1,
    ])
    bear = sum([
        d["ema20"] < d["ema50"], d["ema50"] < d["ema100"], d["macd_hist"] < 0,
        32 <= d["rsi"] <= 55, d["vol_ratio"] > 1.1,
    ])
    if bull >= SCORE_TO_TRADE and bull > bear:
        return "LONG", int(bull)
    if bear >= SCORE_TO_TRADE and bear > bull:
        return "SHORT", int(bear)
    return "NOTHING", 0


def collateral_for(equity, score=None):
    base = SCORE_COLLATERAL.get(score, FADE_COLLATERAL)
    return round(min(base, equity * 0.95), 2)


def cancel_coin_orders(coin):
    try:
        for o in info.open_orders(MAIN_ADDR):
            if o.get("coin") == coin:
                exchange.cancel(coin, o["oid"])
    except Exception as e:
        print(f"[cancel] {coin}: {e}")


def close_position(coin):
    print(f"[CLOSE] market-closing {coin}")
    exchange.market_close(coin)
    cancel_coin_orders(coin)


def wait_for_fill(coin, side):
    """Poll until the position actually exists on the correct side. Returns the
    filled size (absolute), or 0 if it never confirmed."""
    want_long = side == "LONG"
    for _ in range(FILL_WAIT_TRIES):
        time.sleep(FILL_WAIT_SECONDS)
        _, oc = get_equity_and_positions()
        if coin in oc:
            szi = oc[coin]
            if (szi > 0) == want_long and abs(szi) > 0:
                return abs(szi)
    return 0.0


# --------------------------- routes ---------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(force=True, silent=True) or {}
    body = raw.get("body", raw)
    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        return jsonify({"status": "error", "reason": "unauthorized"}), 401
    try:
        sig = {
            "symbol": str(body["symbol"]).upper(),
            "price": float(body["price"]),
            "ema20": float(body["ema20"]), "ema50": float(body["ema50"]), "ema100": float(body["ema100"]),
            "rsi": float(body["rsi"]), "macd_hist": float(body["macd_hist"]),
            "vol_ratio": float(body["vol_ratio"]), "atr_pct": float(body["atr_pct"]),
            "resistance": float(body.get("resistance", 0) or 0),
            "support": float(body.get("support", 0) or 0),
            "res_touches": int(float(body.get("res_touches", 0) or 0)),
            "sup_touches": int(float(body.get("sup_touches", 0) or 0)),
        }
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"status": "error", "reason": f"bad payload: {e}"}), 200

    coin = sig["symbol"]
    if coin not in COINS:
        return jsonify({"status": "ignored", "reason": f"{coin} not enabled"}), 200
    if sig["atr_pct"] > VOL_LIMITS.get(coin, 2.0):
        return jsonify({"status": "no_trade", "coin": coin, "reason": "volatility too high"}), 200

    price = sig["price"]
    fade = plan_fade_trade(coin, price, sig["atr_pct"], sig["resistance"], sig["res_touches"],
                           sig["support"], sig["sup_touches"])
    if fade:
        side, score, trade_type = fade["side"], None, "fade"
    else:
        side, score = decide(sig)
        if side == "NOTHING":
            return jsonify({"status": "no_trade", "coin": coin, "reason": "filters not met"}), 200
        trade_type = "trend"

    equity, open_coins = get_equity_and_positions()
    update_close_tracking(open_coins)

    if coin in open_coins:
        current_side = "LONG" if open_coins[coin] > 0 else "SHORT"
        return jsonify({"status": "skipped", "coin": coin,
                        "reason": f"already {current_side} on this coin"}), 200

    guard_reason = blocked_by_cooldown_or_price(coin, price, sig["atr_pct"])
    if guard_reason:
        return jsonify({"status": "skipped", "coin": coin, "reason": guard_reason}), 200

    if trade_type == "trend":
        level_reason = blocked_by_level(side, price, sig["atr_pct"], sig["resistance"], sig["support"])
        if level_reason:
            return jsonify({"status": "skipped", "coin": coin, "reason": level_reason}), 200

    collateral = collateral_for(equity, score)
    notional   = collateral * LEVERAGE
    size       = round(notional / price, sz_decimals(coin))
    if size <= 0:
        return jsonify({"status": "error", "reason": "size rounded to 0 — collateral too small"}), 200

    is_buy = side == "LONG"
    if trade_type == "fade":
        tp, sl = round_px(coin, fade["tp"]), round_px(coin, fade["sl"])
    elif is_buy:
        tp, sl = round_px(coin, price * (1 + TP_PCT)), round_px(coin, price * (1 - SL_PCT))
    else:
        tp, sl = round_px(coin, price * (1 - TP_PCT)), round_px(coin, price * (1 + SL_PCT))

    try:
        # 1) clear any leftover orders on this coin (e.g. from a manual close)
        cancel_coin_orders(coin)
        # 2) set leverage and open
        exchange.update_leverage(LEVERAGE, coin, is_cross=False)
        exchange.market_open(coin, is_buy, size, None, SLIPPAGE)
        # 3) WAIT until the position is really open before attaching TP/SL,
        #    otherwise Hyperliquid cancels the reduce-only orders
        filled = wait_for_fill(coin, side)
        if filled <= 0:
            return jsonify({"status": "error", "coin": coin,
                            "reason": "entry not confirmed filled — no TP/SL attached"}), 200
        # 4) attach TP/SL against the ACTUAL filled size
        exchange.order(coin, not is_buy, filled, tp,
                       {"trigger": {"triggerPx": tp, "isMarket": False, "tpsl": "tp"}}, reduce_only=True)
        exchange.order(coin, not is_buy, filled, sl,
                       {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
    except Exception as e:
        return jsonify({"status": "error", "coin": coin, "reason": str(e)}), 200

    peaks.pop(coin, None)
    known_open[coin] = side

    return jsonify({
        "status": "executed", "coin": coin, "side": side, "trade_type": trade_type, "score": score,
        "fade_level": fade["level"] if fade else None, "fade_touches": fade["touches"] if fade else None,
        "entry_price": price, "collateral_usd": collateral, "leverage": LEVERAGE,
        "exposure_usd": round(notional, 2), "size": filled, "tp": tp, "sl": sl,
        "resistance": sig["resistance"], "support": sig["support"],
        "account_equity": round(equity, 2), "time": datetime.utcnow().isoformat()
    }), 200


@app.route("/manage", methods=["GET"])
def manage():
    try:
        s = info.user_state(MAIN_ADDR)
        mids = info.all_mids()
        closes = []
        open_now = set()
        open_coins_now = {}
        for p in s.get("assetPositions", []):
            pos = p.get("position", {})
            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue
            coin = pos.get("coin")
            open_now.add(coin)
            open_coins_now[coin] = szi
            entry = float(pos.get("entryPx", 0) or 0)
            mark = float(mids.get(coin, 0) or 0)
            if entry <= 0 or mark <= 0:
                continue
            profit = (mark - entry) / entry if szi > 0 else (entry - mark) / entry
            peak = max(peaks.get(coin, profit), profit)
            peaks[coin] = peak
            if peak >= TRAIL_ACTIVATE and (peak - profit) >= TRAIL_GIVEBACK:
                try:
                    print(f"[trail] closing {coin} at {round(profit*100,2)}% (peak {round(peak*100,2)}%)")
                    close_position(coin)
                    peaks.pop(coin, None)
                    closes.append({"coin": coin, "closed_at_pct": round(profit * 100, 2),
                                   "peak_pct": round(peak * 100, 2)})
                except Exception as e:
                    print(f"[trail] close {coin} failed: {e}")
        for c in [cl["coin"] for cl in closes]:
            open_now.discard(c)
            open_coins_now.pop(c, None)
        update_close_tracking(open_coins_now)
        for c in list(peaks.keys()):
            if c not in open_now:
                peaks.pop(c, None)
        return jsonify({"status": "managed", "trailing_closes": closes,
                        "tracked": {k: round(v * 100, 2) for k, v in peaks.items()}}), 200
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


@app.route("/status", methods=["GET"])
def status():
    try:
        equity, open_coins = get_equity_and_positions()
        return jsonify({
            "status": "running", "coins": COINS, "leverage": f"{LEVERAGE}x isolated",
            "initial_capital": INITIAL_CAPITAL, "score_collateral": SCORE_COLLATERAL,
            "fade_collateral": FADE_COLLATERAL, "account_equity": round(equity, 2),
            "open_positions": open_coins, "tp_pct": TP_PCT, "sl_pct": SL_PCT,
            "cooldown_hours": COOLDOWN_HOURS, "reentry_atr_mult": REENTRY_ATR_MULT,
            "sr_atr_buffer": SR_ATR_BUFFER, "sr_strong_touches": SR_STRONG_TOUCHES,
            "last_close": {c: {"price": v["price"], "side": v["side"], "time": v["time"].isoformat()}
                           for c, v in last_close.items()},
        })
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "ai-trend-bot"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
