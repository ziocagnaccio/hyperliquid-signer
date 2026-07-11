"""
AI Trend Bot — Hyperliquid (BTC / ETH / SOL)
Flow:  TradingView (indicators + S/R) -> n8n -> THIS app -> Hyperliquid -> Telegram
Timeframe is set in TradingView, NOT here.

Position size is now fixed $ collateral by conviction score, not a % of equity:
  5/5 votes -> $250, 4/5 -> $200, 3/5 -> $150. Fade trades (see below) -> $200.

Exits on a position:
  - Take profit at +4% (hard ceiling)
  - Stop loss at -3% (hard floor)
  - TRAILING take profit: locks in profit if price gives back from its best point

Entry guards (NEW):
  - Cooldown: after a coin's position closes (TP/SL/manual), wait COOLDOWN_HOURS
    before considering a new entry on that coin.
  - Re-entry price filter: even after the cooldown, don't re-enter unless price has
    moved at least REENTRY_ATR_MULT * ATR away from the price where the last
    position closed. This is what actually blocks re-entries during a sideways
    market, where time alone would eventually let a bad re-entry through.
  - Support/Resistance filter: TradingView now sends the nearest pivot-based
    resistance (above) and support (below). A LONG signal is skipped if price is
    within SR_ATR_BUFFER * ATR of the resistance above it; a SHORT signal is
    skipped if price is within that distance of the support below it. Once price
    closes beyond the level (it breaks), the check naturally stops blocking,
    so the bot is free to trade the breakout.

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
LEVERAGE        = 10              # 10x
TP_PCT          = 0.04            # take profit at +4%
SL_PCT          = 0.03            # stop loss at -3%
COINS           = ["BTC", "ETH", "SOL"]
VOL_LIMITS      = {"BTC": 1.3, "ETH": 1.6, "SOL": 2.0}   # skip if atr_pct above this
SCORE_TO_TRADE  = 3              # need 3 of 5 votes (raise to 4 = stricter)
SLIPPAGE        = 0.01           # max 1% slippage on the market entry

# --- Position size by conviction score — flat $ collateral, not % of equity ---
SCORE_COLLATERAL = {3: 150.0, 4: 200.0, 5: 250.0}   # vote score -> collateral in USD
FADE_COLLATERAL  = 200.0                            # fade trades have no vote score, use mid-size

# --- Trailing take profit (checked by /manage on a timer) ---
TRAIL_ACTIVATE  = 0.015          # arm trailing once price moved +1.5% in your favor
TRAIL_GIVEBACK  = 0.007          # then close if it gives back 0.7% from the best point

# --- Entry guards: cooldown + re-entry distance + support/resistance ---
# NOTE: the re-entry floor is NOT based on TP_PCT. You don't always exit at +4% —
# trailing can close as early as ~+0.8%, SL closes at -3%, and manual closes can
# happen at any price. So this is just a flat "don't treat this as a new
# opportunity unless price has genuinely moved" floor, independent of how/why
# the last position closed.
COOLDOWN_HOURS     = 2           # minimum wait after ANY close before re-entering the coin
REENTRY_MIN_PCT    = 0.01        # floor: always require at least this % move from the close price (1%)
REENTRY_ATR_MULT   = 0.75        # ...OR this many ATRs, whichever is BIGGER (scales up in volatile markets)
SR_ATR_BUFFER      = 0.5         # skip entries within this many ATRs of an opposing S/R level

# --- Fade a strong level (overrides the trend signal) ---
# If price sits right at a level that has rejected price SR_STRONG_TOUCHES+ times
# in the lookback, trade AGAINST the trend signal instead: long off strong
# support, short off strong resistance. Weaker levels (fewer touches) only
# block via SR_ATR_BUFFER above — they don't trigger a fade.
SR_STRONG_TOUCHES    = 2         # min touches/rejections for a level to be "strong" enough to fade
SR_FADE_ATR_MULT     = 0.5       # how close (in ATRs) price must be to the strong level to fade it
SR_FADE_SL_ATR_MULT  = 0.3       # stop placed this many ATRs beyond the level (buffer against noise)
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

# Remembers the last seen open side per coin, so we can detect "it just closed"
# (whether closed by TP, SL, trailing, or manually on Hyperliquid itself).
known_open = {}

# Remembers, per coin, the price/time of the most recent close — used by the
# cooldown + re-entry distance guard.
last_close = {}

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


def update_close_tracking(open_coins):
    """Compare currently open coins to what we last saw open. Any coin that
    was open before and isn't now just closed — record its close price/time
    so the cooldown + re-entry filter can use it. Works whether the close was
    our TP/SL trigger, trailing stop, or a manual close on Hyperliquid."""
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
            last_close[coin] = {
                "price": px,
                "time": datetime.utcnow(),
                "side": known_open[coin],
            }
    known_open = {c: ("LONG" if szi > 0 else "SHORT") for c, szi in open_coins.items()}


def blocked_by_cooldown_or_price(coin, price, atr_pct):
    """Returns a reason string if entry should be skipped, else None."""
    lc = last_close.get(coin)
    if not lc or lc["price"] <= 0:
        return None  # no known prior close, nothing to guard against
    hours_since = (datetime.utcnow() - lc["time"]).total_seconds() / 3600.0
    if hours_since < COOLDOWN_HOURS:
        return f"cooldown active ({hours_since:.2f}h < {COOLDOWN_HOURS}h since last close)"
    atr_abs = (atr_pct / 100.0) * price
    min_move_pct = REENTRY_MIN_PCT * price     # floor tied to the TP target
    min_move_atr = REENTRY_ATR_MULT * atr_abs  # scales up with volatility
    required_move = max(min_move_pct, min_move_atr)
    moved = abs(price - lc["price"])
    if moved < required_move:
        return (f"price hasn't moved enough since last close "
                f"({moved:.2f} < required {required_move:.2f}, "
                f"= max({REENTRY_MIN_PCT*100:.1f}% of price, {REENTRY_ATR_MULT}xATR))")
    return None


def blocked_by_level(side, price, atr_pct, resistance, support):
    """Returns a reason string if entry should be skipped because price is too
    close to an opposing support/resistance level, else None. If the level has
    already been broken (price past it), this does NOT block — breakouts are
    allowed through."""
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
    """If price is right at a level that has rejected price SR_STRONG_TOUCHES+
    times, plan a trade AGAINST that level — long off strong support, short off
    strong resistance — regardless of what the 3H trend signal says. Stop goes
    just beyond the level (capped at the normal SL_PCT so risk never exceeds a
    regular trade); target is the opposite level if it's closer than the
    normal TP_PCT, else the normal TP_PCT. Returns None if no strong level is
    close enough right now."""
    atr_abs = (atr_pct / 100.0) * price
    if atr_abs <= 0:
        return None

    if support and support > 0 and sup_touches >= SR_STRONG_TOUCHES and price > support:
        if (price - support) < SR_FADE_ATR_MULT * atr_abs:
            sl = support - SR_FADE_SL_ATR_MULT * atr_abs
            sl = max(sl, price * (1 - SL_PCT))              # never risk more than a normal trade
            if resistance and resistance > price:
                tp = min(resistance, price * (1 + TP_PCT))  # aim for the opposite level, capped
            else:
                tp = price * (1 + TP_PCT)
            return {"side": "LONG", "sl": sl, "tp": tp, "touches": sup_touches, "level": support}

    if resistance and resistance > 0 and res_touches >= SR_STRONG_TOUCHES and price < resistance:
        if (resistance - price) < SR_FADE_ATR_MULT * atr_abs:
            sl = resistance + SR_FADE_SL_ATR_MULT * atr_abs
            sl = min(sl, price * (1 + SL_PCT))
            if support and support < price:
                tp = max(support, price * (1 - TP_PCT))
            else:
                tp = price * (1 - TP_PCT)
            return {"side": "SHORT", "sl": sl, "tp": tp, "touches": res_touches, "level": resistance}

    return None


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


def collateral_for(equity, score=None):
    """Fixed collateral by conviction score: 5/5 -> $250, 4/5 -> $200, 3/5 -> $150.
    Fade trades (no vote score) use FADE_COLLATERAL. Capped at 95% of current
    equity so it never asks for more collateral than the account actually has,
    even if equity has dropped below the score's usual size."""
    base = SCORE_COLLATERAL.get(score, FADE_COLLATERAL)
    return round(min(base, equity * 0.95), 2)


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
            "symbol":      str(body["symbol"]).upper(),
            "price":       float(body["price"]),
            "ema20":       float(body["ema20"]),
            "ema50":       float(body["ema50"]),
            "ema100":      float(body["ema100"]),
            "rsi":         float(body["rsi"]),
            "macd_hist":   float(body["macd_hist"]),
            "vol_ratio":   float(body["vol_ratio"]),
            "atr_pct":     float(body["atr_pct"]),
            "resistance":  float(body.get("resistance", 0) or 0),
            "support":     float(body.get("support", 0) or 0),
            "res_touches": int(float(body.get("res_touches", 0) or 0)),
            "sup_touches": int(float(body.get("sup_touches", 0) or 0)),
        }
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"status": "error", "reason": f"bad payload: {e}"}), 200

    coin = sig["symbol"]
    if coin not in COINS:
        return jsonify({"status": "ignored", "reason": f"{coin} not enabled"}), 200

    # Volatility gate applies to EVERY trade — trend-follow or fade
    if sig["atr_pct"] > VOL_LIMITS.get(coin, 2.0):
        return jsonify({"status": "no_trade", "coin": coin,
                        "reason": "volatility too high"}), 200

    price = sig["price"]

    # 1) A strong, repeatedly-rejected level takes priority over the trend
    #    signal — fade it (long off strong support, short off strong resistance).
    fade = plan_fade_trade(coin, price, sig["atr_pct"],
                           sig["resistance"], sig["res_touches"],
                           sig["support"], sig["sup_touches"])

    if fade:
        side = fade["side"]
        score = None
        trade_type = "fade"
    else:
        side, score = decide(sig)
        if side == "NOTHING":
            return jsonify({"status": "no_trade", "coin": coin,
                            "reason": "filters not met"}), 200
        trade_type = "trend"

    equity, open_coins = get_equity_and_positions()
    update_close_tracking(open_coins)

    if coin in open_coins:
        # one position per coin — don't stack or flip, just wait for it to close
        current_side = "LONG" if open_coins[coin] > 0 else "SHORT"
        return jsonify({"status": "skipped", "coin": coin,
                        "reason": f"already {current_side} on this coin"}), 200

    guard_reason = blocked_by_cooldown_or_price(coin, price, sig["atr_pct"])
    if guard_reason:
        return jsonify({"status": "skipped", "coin": coin, "reason": guard_reason}), 200

    if trade_type == "trend":
        # only trend-follow trades get blocked by proximity to a level — a fade
        # trade is specifically ABOUT trading at the level, so this doesn't apply
        level_reason = blocked_by_level(side, price, sig["atr_pct"], sig["resistance"], sig["support"])
        if level_reason:
            return jsonify({"status": "skipped", "coin": coin, "reason": level_reason}), 200

    collateral = collateral_for(equity, score)
    notional   = collateral * LEVERAGE
    size       = round(notional / price, sz_decimals(coin))
    if size <= 0:
        return jsonify({"status": "error", "reason": "size rounded to 0 — raise POSITION_PCT"}), 200

    is_buy = side == "LONG"
    if trade_type == "fade":
        tp = round_px(coin, fade["tp"])
        sl = round_px(coin, fade["sl"])
    elif is_buy:
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
    known_open[coin] = side

    return jsonify({
        "status": "executed",
        "coin": coin,
        "side": side,
        "trade_type": trade_type,
        "score": score,
        "fade_level": fade["level"] if fade else None,
        "fade_touches": fade["touches"] if fade else None,
        "entry_price": price,
        "collateral_usd": collateral,
        "leverage": LEVERAGE,
        "exposure_usd": round(notional, 2),
        "size": size,
        "tp": tp,
        "sl": sl,
        "resistance": sig["resistance"],
        "support": sig["support"],
        "account_equity": round(equity, 2),
        "equity_used_for_sizing": round(min(equity, INITIAL_CAPITAL), 2),
        "time": datetime.utcnow().isoformat()
    }), 200


@app.route("/manage", methods=["GET"])
def manage():
    """Called every few minutes (by UptimeRobot). Runs the trailing take-profit:
    remembers each position's best profit, and closes it if it gives back too much.
    Also updates close-tracking so the cooldown/re-entry guard sees closes that
    happen via TP/SL triggers or a manual close on Hyperliquid itself."""
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

        # reflect any trailing closes we just did, then update close-tracking
        for c in [cl["coin"] for cl in closes]:
            open_now.discard(c)
            open_coins_now.pop(c, None)
        update_close_tracking(open_coins_now)

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
            "score_collateral": SCORE_COLLATERAL,
            "fade_collateral": FADE_COLLATERAL,
            "account_equity": round(equity, 2),
            "open_positions": open_coins,
            "tp_pct": TP_PCT,
            "sl_pct": SL_PCT,
            "cooldown_hours": COOLDOWN_HOURS,
            "reentry_atr_mult": REENTRY_ATR_MULT,
            "sr_atr_buffer": SR_ATR_BUFFER,
            "sr_strong_touches": SR_STRONG_TOUCHES,
            "last_close": {
                c: {"price": v["price"], "side": v["side"], "time": v["time"].isoformat()}
                for c, v in last_close.items()
            },
        })
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "ai-trend-bot"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
