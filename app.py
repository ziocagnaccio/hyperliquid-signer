"""
AI Trend Bot — Hyperliquid (BTC / ETH) — BOT A ($480 wallet) — NO TRADINGVIEW
The bot downloads Hyperliquid candles and calculates the indicators itself.
UptimeRobot opens /manage every 5 minutes: that runs the trailing AND checks
if a new 3h candle has closed (if yes, it runs the strategy).
Telegram messages are sent directly by this app (🅰️).

Strategy (unchanged): 5 votes (EMA20/50, EMA50/100, MACD, RSI, volume), need 3.
Size:   $100 collateral; a full 5/5 score gets $150. 10x isolated.
Exits:  TP +4% | SL -3% | trailing (+1.5% then give back 0.7%).
Guard:  cooldown 2h + re-entry distance.
"""
import os
import time
import math
import threading
from datetime import datetime
import requests
from flask import Flask, jsonify
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

app = Flask(__name__)

# ========================= CONFIG — edit these =========================
INITIAL_CAPITAL = 480.0
LEVERAGE        = 10
TP_PCT          = 0.04            # take profit +4%
SL_PCT          = 0.03            # stop loss  -3%
COINS           = ["BTC", "ETH"]
VOL_LIMITS      = {"BTC": 1.3, "ETH": 1.6}   # skip if atr_pct above this
SCORE_TO_TRADE  = 3              # need 3 of 5 votes
SLIPPAGE        = 0.01

SCORE_COLLATERAL = {3: 100.0, 4: 100.0, 5: 150.0}   # 5/5 -> 150, otherwise 100

TRAIL_ACTIVATE  = 0.015          # arm once price moved +1.5% in your favor
TRAIL_GIVEBACK  = 0.007          # close if it gives back 0.7% from the best point

COOLDOWN_HOURS   = 2
REENTRY_MIN_PCT  = 0.01
REENTRY_ATR_MULT = 0.75

FILL_WAIT_TRIES   = 12
FILL_WAIT_SECONDS = 0.5
# =======================================================================

WALLET_KEY = os.environ["HL_PRIVATE_KEY"]
MAIN_ADDR  = os.environ["HL_WALLET_ADDR"]
# TELEGRAM_TOKEN and TELEGRAM_CHAT_ID can hold several values separated by commas.
# They are matched in order: 1st token -> 1st chat ID, 2nd token -> 2nd chat ID.
# With only one token, that token is used for every chat ID.
TG_TOKENS  = [t.strip() for t in os.environ.get("TELEGRAM_TOKEN", "").split(",") if t.strip()]
TG_CHATS   = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

_wallet  = Account.from_key(WALLET_KEY)
info     = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(_wallet, constants.MAINNET_API_URL, account_address=MAIN_ADDR)

LOCK = threading.Lock()
peaks = {}
known_open = {}
last_close = {}
last_results = {}


def tg(text):
    print("[TG]", text)
    if not TG_TOKENS or not TG_CHATS:
        return
    for n, chat in enumerate(TG_CHATS):
        token = TG_TOKENS[0] if len(TG_TOKENS) == 1 else (TG_TOKENS[n] if n < len(TG_TOKENS) else None)
        if not token:
            print(f"[TG] no token for chat {chat}")
            continue
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": "🅰️ BOT A\n" + text}, timeout=10)
        except Exception as e:
            print(f"[TG] failed: {e}")

# ===================== CANDLES + INDICATORS (no TradingView) =====================
# Hyperliquid has no 3h candles, so we download 1h candles and join them 3 by 3
# (00-03, 03-06, ... UTC). Formulas are the same as TradingView's.
HOUR_MS        = 3600 * 1000
CANDLE_MS      = 3 * HOUR_MS
HISTORY_DAYS   = 60           # enough history for EMA100 / ADX to settle
FRESH_MINUTES  = 20           # only act on a candle that closed in the last 20 min
_last_candle   = {}           # coin -> open time of the last 3h candle already checked


def fetch_3h_candles(coin):
    end = int(time.time() * 1000)
    start = end - HISTORY_DAYS * 24 * HOUR_MS
    raw = info.candles_snapshot(coin, "1h", start, end)
    groups = {}
    for c in raw:
        t = int(c["t"])
        groups.setdefault(t - (t % CANDLE_MS), []).append(c)
    out = []
    for g in sorted(groups):
        if g + CANDLE_MS > end - 30000:        # this 3h candle hasn't closed yet
            continue
        cs = sorted(groups[g], key=lambda c: int(c["t"]))
        out.append({"t": g,
                    "o": float(cs[0]["o"]), "c": float(cs[-1]["c"]),
                    "h": max(float(c["h"]) for c in cs), "l": min(float(c["l"]) for c in cs),
                    "v": sum(float(c["v"]) for c in cs)})
    return out


def _smooth(src, n, alpha):
    """EMA / RMA like TradingView: starts with a simple average of the first n values."""
    out, prev, buf = [None] * len(src), None, []
    for i, x in enumerate(src):
        if x is None:
            continue
        if prev is None:
            buf.append(x)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
        else:
            prev = alpha * x + (1 - alpha) * prev
            out[i] = prev
    return out


def ind_ema(src, n):
    return _smooth(src, n, 2.0 / (n + 1))


def ind_rma(src, n):
    return _smooth(src, n, 1.0 / n)


def ind_sma(src, n):
    out = [None] * len(src)
    for i in range(n - 1, len(src)):
        w = src[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / n
    return out


def compute_signal(coin):
    """Builds the same data TradingView used to send, from Hyperliquid candles."""
    k = fetch_3h_candles(coin)
    if len(k) < 150:
        raise ValueError(f"not enough candles ({len(k)})")
    o = [x["o"] for x in k]; h = [x["h"] for x in k]; l = [x["l"] for x in k]
    c = [x["c"] for x in k]; v = [x["v"] for x in k]
    n = len(c)

    ema20, ema50, ema100 = ind_ema(c, 20), ind_ema(c, 50), ind_ema(c, 100)

    # RSI 14
    ups = [None] + [max(c[i] - c[i - 1], 0.0) for i in range(1, n)]
    dns = [None] + [max(c[i - 1] - c[i], 0.0) for i in range(1, n)]
    ru, rd = ind_rma(ups, 14), ind_rma(dns, 14)
    rsi = 100.0 if rd[-1] == 0 else (0.0 if ru[-1] == 0 else 100 - 100 / (1 + ru[-1] / rd[-1]))

    # MACD 12 26 9
    e12, e26 = ind_ema(c, 12), ind_ema(c, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ind_ema(macd, 9)
    hist = macd[-1] - sig[-1]

    def cross_up(i):
        return macd[i] > sig[i] and macd[i - 1] <= sig[i - 1]

    def cross_dn(i):
        return macd[i] < sig[i] and macd[i - 1] >= sig[i - 1]

    # ATR 14 + ADX 14
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = ind_rma(tr, 14)
    pdm, mdm = [None], [None]
    for i in range(1, n):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    trr = ind_rma([None] + tr[1:], 14)
    pr, mr = ind_rma(pdm, 14), ind_rma(mdm, 14)
    dx = []
    for i in range(n):
        if trr[i] is None or pr[i] is None or mr[i] is None or trr[i] == 0:
            dx.append(None)
            continue
        p, m = 100 * pr[i] / trr[i], 100 * mr[i] / trr[i]
        s = p + m
        dx.append(abs(p - m) / (s if s != 0 else 1))
    adx = 100 * ind_rma(dx, 14)[-1]

    vavg = ind_sma(v, 20)[-1]
    return {
        "symbol": coin, "candle_time": k[-1]["t"],
        "price": c[-1], "high": h[-1], "low": l[-1],
        "ema20": ema20[-1], "ema50": ema50[-1], "ema100": ema100[-1],
        "rsi": rsi, "macd_hist": hist,
        "macd_up2": cross_up(n - 1) or cross_up(n - 2),
        "macd_dn2": cross_dn(n - 1) or cross_dn(n - 2),
        "adx": adx, "atr": atr[-1], "atr_pct": atr[-1] / c[-1] * 100,
        "vol_ratio": (v[-1] / vavg) if vavg else 0.0,
    }


def candle_clock(handler):
    """Called every 5 min: if a new 3h candle closed, run the strategy on it."""
    results = {}
    now = int(time.time() * 1000)
    for coin in COINS:
        try:
            s = compute_signal(coin)
        except Exception as e:
            results[coin] = f"candle error: {e}"
            continue
        ct = s["candle_time"]
        if _last_candle.get(coin) is not None and ct <= _last_candle[coin]:
            results[coin] = "waiting for the next 3h candle"
            continue
        _last_candle[coin] = ct
        if now - (ct + CANDLE_MS) > FRESH_MINUTES * 60 * 1000:
            results[coin] = "candle too old, waiting for the next one"
            continue
        try:
            results[coin] = handler(s)
        except Exception as e:
            results[coin] = f"error: {e}"
    return results
# ================================================================================


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
        if coin in known_open and coin not in open_coins:
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
        return f"price hasn't moved enough since last close ({moved:.2f} < {required_move:.2f})"
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
    return round(px, max(0, min(sig, max_dec)))


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


def collateral_for(equity, score):
    base = SCORE_COLLATERAL.get(score, 100.0)
    return round(min(base, equity * 0.95), 2)


def cancel_coin_orders(coin):
    try:
        for o in info.open_orders(MAIN_ADDR):
            if o.get("coin") == coin:
                exchange.cancel(coin, o["oid"])
    except Exception as e:
        print(f"[cancel] {coin}: {e}")


def close_position(coin):
    exchange.market_close(coin)
    cancel_coin_orders(coin)


def wait_for_fill(coin, side):
    want_long = side == "LONG"
    for _ in range(FILL_WAIT_TRIES):
        time.sleep(FILL_WAIT_SECONDS)
        _, oc = get_equity_and_positions()
        if coin in oc:
            szi = oc[coin]
            if (szi > 0) == want_long and abs(szi) > 0:
                return abs(szi)
    return 0.0

# --------------------------- strategy ---------------------------

def handle_signal(sig):
    coin = sig["symbol"]
    side, score = decide(sig)
    if side == "NOTHING":
        return {"status": "no_trade", "coin": coin, "reason": "filters not met"}

    equity, open_coins = get_equity_and_positions()
    update_close_tracking(open_coins)

    if coin in open_coins:
        current_side = "LONG" if open_coins[coin] > 0 else "SHORT"
        return {"status": "skipped", "coin": coin, "reason": f"already {current_side} on this coin"}

    guard = blocked_by_cooldown_or_price(coin, sig["price"], sig["atr_pct"])
    if guard:
        return {"status": "skipped", "coin": coin, "reason": guard}

    price      = sig["price"]
    collateral = collateral_for(equity, score)
    notional   = collateral * LEVERAGE
    size       = round(notional / price, sz_decimals(coin))
    if size <= 0:
        return {"status": "error", "reason": "size rounded to 0"}

    is_buy = side == "LONG"
    if is_buy:
        tp, sl = round_px(coin, price * (1 + TP_PCT)), round_px(coin, price * (1 - SL_PCT))
    else:
        tp, sl = round_px(coin, price * (1 - TP_PCT)), round_px(coin, price * (1 + SL_PCT))

    try:
        cancel_coin_orders(coin)
        exchange.update_leverage(LEVERAGE, coin, is_cross=False)
        exchange.market_open(coin, is_buy, size, None, SLIPPAGE)
        filled = wait_for_fill(coin, side)
        if filled <= 0:
            tg(f"❗ {coin} {side}: entry not confirmed filled, no TP/SL attached. Check Hyperliquid.")
            return {"status": "error", "coin": coin, "reason": "entry not confirmed filled"}
        exchange.order(coin, not is_buy, filled, tp,
                       {"trigger": {"triggerPx": tp, "isMarket": False, "tpsl": "tp"}}, reduce_only=True)
        exchange.order(coin, not is_buy, filled, sl,
                       {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
    except Exception as e:
        tg(f"❗ {coin} {side}: error while opening: {e}")
        return {"status": "error", "coin": coin, "reason": str(e)}

    peaks.pop(coin, None)
    known_open[coin] = side
    tg(f"🚀 {coin} {side} opened @ {price}\n"
       f"Score {score}/5, collateral ${collateral} ({LEVERAGE}x)\n"
       f"TP {tp} | SL {sl}")
    return {"status": "executed", "coin": coin, "side": side, "score": score,
            "entry_price": price, "collateral_usd": collateral, "tp": tp, "sl": sl}

# --------------------------- routes ---------------------------

@app.route("/manage", methods=["GET"])
def manage():
    """Every 5 min (UptimeRobot): trailing + check for a new 3h candle."""
    with LOCK:
        closes = []
        try:
            s = info.user_state(MAIN_ADDR)
            mids = info.all_mids()
            open_now, open_coins_now = set(), {}
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
                        close_position(coin)
                        peaks.pop(coin, None)
                        closes.append(coin)
                        tg(f"✅ {coin} closed by trailing at {profit * 100:+.2f}% "
                           f"(best was {peak * 100:+.2f}%)")
                    except Exception as e:
                        tg(f"❗ {coin}: trailing close failed: {e}")
            for c in closes:
                open_now.discard(c)
                open_coins_now.pop(c, None)
            update_close_tracking(open_coins_now)
            for c in list(peaks.keys()):
                if c not in open_now:
                    peaks.pop(c, None)
        except Exception as e:
            print(f"[manage] trailing error: {e}")

        results = candle_clock(handle_signal)
        last_results.update({c: {"result": r, "checked": datetime.utcnow().isoformat()}
                             for c, r in results.items()})
        return jsonify({"status": "managed", "trailing_closes": closes, "candles": results}), 200


@app.route("/signals", methods=["GET"])
def signals():
    """See what the bot calculates right now (no trading)."""
    out = {}
    for coin in COINS:
        try:
            s = compute_signal(coin)
            side, score = decide(s)
            s["candle_time"] = datetime.utcfromtimestamp(s["candle_time"] / 1000).strftime("%Y-%m-%d %H:%M UTC")
            out[coin] = {"decision": side, "score": score,
                         **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}}
        except Exception as e:
            out[coin] = {"error": str(e)}
    return jsonify(out)


@app.route("/status", methods=["GET"])
def status():
    try:
        equity, open_coins = get_equity_and_positions()
        return jsonify({
            "status": "running", "bot": "A (current strategy, no TradingView)", "coins": COINS,
            "leverage": f"{LEVERAGE}x isolated", "initial_capital": INITIAL_CAPITAL,
            "score_collateral": SCORE_COLLATERAL, "account_equity": round(equity, 2),
            "open_positions": open_coins, "last_checks": last_results,
            "last_close": {c: {"price": v["price"], "side": v["side"], "time": v["time"].isoformat()}
                           for c, v in last_close.items()},
        })
    except Exception as e:
        return jsonify({"status": "error", "reason": str(e)}), 200


@app.route("/test", methods=["GET"])
def test_telegram():
    """Sends a test message to Telegram, and shows which wallet this bot uses."""
    try:
        equity = float(info.user_state(MAIN_ADDR)["marginSummary"]["accountValue"])
    except Exception:
        equity = -1
    tg(f"👋 Test message. I'm Bot A (current strategy).\n"
       f"Wallet: {MAIN_ADDR[:6]}...{MAIN_ADDR[-4:]}\nBalance: ${equity:.2f}")
    return jsonify({"status": "test sent", "wallet": MAIN_ADDR, "equity": round(equity, 2)})


@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "service": "trend-bot-A"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
