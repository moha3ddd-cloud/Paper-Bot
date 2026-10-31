"""
Trend Pullback Bot (BTC / ETH / SOL) - OKX public data, PAPER trading only (fake money).

Modes:
    python bot.py backtest --days 180   history test (the daily assessment is NOT part of it:
                                        funding / order book / news have no history here)
    python bot.py once                  one pass: use with GitHub Actions cron
    python bot.py live                  endless loop: use on a laptop / VPS

Env vars: EXCHANGE (default okx), TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import json
import time
import argparse
import csv
import traceback

import ccxt
import numpy as np
import pandas as pd
import requests

CFG = dict(
    symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    exchange=os.getenv("EXCHANGE", "okx"),
    start_balance=1000.0,        # fake money
    risk_pct=0.0075,             # 0.75% of balance per trade (before the daily-decision multiplier)
    weekend_risk_mult=0.5,       # half risk on Sat/Sun
    fee=0.0005,                  # 0.05% per side (assumes perpetual-futures taker fee; spot taker is 0.10%)
    slip=0.0002,                 # 0.02% slippage on market fills
    max_cost_ratio=0.25,         # skip setup if round-trip costs > 25% of the stop distance
    max_leverage=2.0,            # notional cap = balance * 2
    max_positions=2,
    max_trades_day=3,
    max_consec_losses=2,
    daily_loss_limit=0.02,
    max_hold_h=16,
    max_signal_age_min=25,       # ignore a signal whose 15m candle closed longer ago than this
    data_dir="data",
    events_file="events.txt",
    loop_sleep=20,
)
CFG["state_file"] = os.path.join(CFG["data_dir"], "paper_state.json")
CFG["trades_file"] = os.path.join(CFG["data_dir"], "paper_trades.csv")

# daily decision -> risk multiplier (0 = no new entries)
RISK_MULT = {"TRADE": 1.0, "SELECTIVE TRADE": 0.75, "HIGH CAUTION": 0.5, "NO TRADE": 0.0}


# ----------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ----------------------------------------------------------------- data
def to_df(raw, tf_ms):
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["open_time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    df["close_time"] = df.open_time + pd.Timedelta(milliseconds=tf_ms)
    return df


def closed_only(df, now):
    return df[df.close_time <= now].reset_index(drop=True)


def fetch_range(ex, sym, tf, since_ms):
    out, last = [], None
    step = ex.parse_timeframe(tf) * 1000
    now = ex.milliseconds()
    while since_ms < now:
        b = ex.fetch_ohlcv(sym, tf, since=int(since_ms), limit=100)
        if not b:
            break
        if last is not None and b[-1][0] <= last:   # no progress -> stop (avoids endless loop)
            break
        last = b[-1][0]
        out += b
        since_ms = last + step
    return out


def make_exchange():
    return getattr(ccxt, CFG["exchange"])({"enableRateLimit": True})


# ----------------------------------------------------------------- strategy
def build(d15, d4, db1):
    """Indicators + entry signals on 15m data. Higher timeframes are merged by their CLOSE
    time, so there is no look-ahead."""
    h4 = d4.copy()
    h4["e50"], h4["e200"] = ema(h4.close, 50), ema(h4.close, 200)
    h4["up4"] = (h4.close > h4.e200) & (h4.e50 > h4.e200)
    h4["down4"] = (h4.close < h4.e200) & (h4.e50 < h4.e200)
    h4 = h4[["close_time", "up4", "down4"]].rename(columns={"close_time": "t"})

    b = db1.copy()
    b["e50"] = ema(b.close, 50)
    b["bup"], b["bdown"] = b.close > b.e50, b.close < b.e50
    b = b[["close_time", "bup", "bdown"]].rename(columns={"close_time": "t"})

    d = d15.copy()
    d["t"] = d.close_time
    d = pd.merge_asof(d.sort_values("t"), h4.sort_values("t"), on="t", direction="backward")
    d = pd.merge_asof(d, b.sort_values("t"), on="t", direction="backward")
    for c in ("up4", "down4", "bup", "bdown"):
        d[c] = d[c].fillna(False).astype(bool)

    d["ema20"], d["ema50"] = ema(d.close, 20), ema(d.close, 50)
    d["rsi"] = rsi(d.close)
    d["atr"] = atr(d)
    d["volma"] = d.volume.rolling(20).mean()
    d["swing_low"] = d.low.rolling(10).min()
    d["swing_high"] = d.high.rolling(10).max()
    d["rsi_min"] = d.rsi.rolling(8).min()
    d["rsi_max"] = d.rsi.rolling(8).max()
    d["touch_dn"] = d.low.rolling(8).min() <= d.ema20
    d["touch_up"] = d.high.rolling(8).max() >= d.ema20
    atrp = d.atr / d.close
    vol_ok = (atrp > 0.0015) & (atrp < 0.015)

    d["long_sig"] = (d.up4 & d.bup & vol_ok & (d.rsi_min < 42) & d.touch_dn
                     & (d.rsi > 50) & (d.rsi.shift(1) <= 50)
                     & (d.close > d.open) & (d.close > d.ema20) & (d.close > d.ema50)
                     & (d.volume > d.volma))
    d["short_sig"] = (d.down4 & d.bdown & vol_ok & (d.rsi_max > 58) & d.touch_up
                      & (d.rsi < 50) & (d.rsi.shift(1) >= 50)
                      & (d.close < d.open) & (d.close < d.ema20) & (d.close < d.ema50)
                      & (d.volume > d.volma))
    return d


def make_plan(row, side, fill):
    a = float(row.atr)
    fill = float(fill)
    if side == "long":
        sl = min(float(row.swing_low) - 0.1 * a, fill - 1.5 * a)
        dist, d = fill - sl, 1
    else:
        sl = max(float(row.swing_high) + 0.1 * a, fill + 1.5 * a)
        dist, d = sl - fill, -1
    if dist <= 0 or dist > 3 * a:
        return None
    # round-trip costs must stay below max_cost_ratio of the risk distance
    if 2 * (CFG["fee"] + CFG["slip"]) * fill / dist > CFG["max_cost_ratio"]:
        return None
    return dict(side=side, dir=d, entry=fill, sl=sl, dist=dist,
                tp1=fill + d * dist, tp2=fill + d * 2 * dist, tp3=fill + d * 3 * dist)


def risk_for(ts):
    return CFG["risk_pct"] * (CFG["weekend_risk_mult"] if ts.weekday() >= 5 else 1.0)


def ema_at(d, t):
    """EMA20 of the last 15m candle that was already CLOSED at time t (no look-ahead)."""
    ct = d.close_time.dt.tz_convert("UTC").dt.tz_localize(None).values
    i = np.searchsorted(ct, np.datetime64(t.tz_convert("UTC").tz_localize(None)), side="right") - 1
    return float(d.ema20.iloc[i]) if i >= 0 else float("nan")


# ----------------------------------------------------------------- paper account
class Account:
    def __init__(self, balance):
        self.balance = balance
        self.start = balance
        self.positions = {}
        self.closed = []
        self.day = None
        self.day_start = balance
        self.day_trades = 0
        self.consec = 0
        self.outbox = []
        self.now_iso = ""
        self.meta = {}          # last_sig, last_1m, last_decision, report_day, logged

    def roll(self, ts):
        d = ts.strftime("%Y-%m-%d")
        if d != self.day:
            first = self.day is None
            self.day, self.day_start = d, self.balance
            self.day_trades, self.consec = 0, 0
            return not first
        return False

    def can_trade(self):
        if self.day_trades >= CFG["max_trades_day"]:
            return False, "max trades per day reached"
        if self.consec >= CFG["max_consec_losses"]:
            return False, "2 consecutive losses - done for today"
        if self.balance - self.day_start <= -CFG["daily_loss_limit"] * self.day_start:
            return False, "daily loss limit reached"
        return True, ""

    def open(self, sym, plan, risk_pct, ts):
        qty = self.balance * risk_pct / plan["dist"]
        qty = min(qty, self.balance * CFG["max_leverage"] / plan["entry"])
        fee = qty * plan["entry"] * CFG["fee"]
        self.balance -= fee
        pos = dict(plan, symbol=sym, qty=qty, qty_left=qty, stage=0, pnl=-fee,
                   risk_amt=qty * plan["dist"], open_ts=ts.timestamp())
        self.positions[sym] = pos
        self.day_trades += 1
        return pos

    def _exit(self, pos, qty, price, reason, market):
        d = pos["dir"]
        if market:
            price *= (1 - d * CFG["slip"])
        pnl = (price - pos["entry"]) * qty * d
        fee = qty * price * CFG["fee"]
        self.balance += pnl - fee
        pos["pnl"] += pnl - fee
        pos["qty_left"] -= qty
        sym = pos["symbol"]
        if pos["qty_left"] <= pos["qty"] * 1e-9:
            r = pos["pnl"] / pos["risk_amt"]
            self.consec = self.consec + 1 if pos["pnl"] < 0 else 0
            rec = dict(time=self.now_iso, symbol=sym, side=pos["side"], entry=round(pos["entry"], 6),
                       pnl=round(pos["pnl"], 4), R=round(r, 3), reason=reason)
            self.closed.append(rec)
            del self.positions[sym]
            self.outbox.append(
                f"{'✅' if pos['pnl'] > 0 else '❌'} PAPER EXIT {pos['side'].upper()} {sym} ({reason})\n"
                f"Exit ~{price:.4f} | Trade PnL ${pos['pnl']:.2f} ({r:+.2f}R)\n"
                f"Balance ${self.balance:.2f}")
        else:
            self.outbox.append(f"🎯 {reason} {sym}: closed part @ {price:.4f} | SL now {pos['sl']:.4f}")

    def on_candle(self, sym, c):
        """c: dict(ts, high, low, close, ema20). Conservative: SL is checked before targets and
        only one target stage can advance per candle."""
        pos = self.positions.get(sym)
        if not pos:
            return []
        self.now_iso = c["ts"].isoformat()
        d, hi, lo = pos["dir"], c["high"], c["low"]
        if (lo <= pos["sl"]) if d == 1 else (hi >= pos["sl"]):
            self._exit(pos, pos["qty_left"], pos["sl"], "SL" if pos["stage"] == 0 else "Stop(BE/Trail)", True)
            return self._drain()

        def reached(p):
            return hi >= p if d == 1 else lo <= p

        if pos["stage"] == 0 and reached(pos["tp1"]):
            pos["stage"], pos["sl"] = 1, pos["entry"]
            self._exit(pos, pos["qty"] * 0.5, pos["tp1"], "TP1", False)
        elif pos["stage"] == 1 and reached(pos["tp2"]):
            pos["stage"] = 2
            self._exit(pos, pos["qty"] * 0.25, pos["tp2"], "TP2", False)
        elif pos["stage"] == 2 and reached(pos["tp3"]):
            self._exit(pos, pos["qty_left"], pos["tp3"], "TP3", False)

        if sym in self.positions:
            pos = self.positions[sym]
            if c["ts"].timestamp() - pos["open_ts"] > CFG["max_hold_h"] * 3600:
                self._exit(pos, pos["qty_left"], c["close"], "TimeStop", True)
            elif pos["stage"] == 2 and c.get("ema20") == c.get("ema20"):  # not NaN
                e = float(c["ema20"])
                pos["sl"] = max(pos["sl"], e) if d == 1 else min(pos["sl"], e)
        return self._drain()

    def _drain(self):
        m, self.outbox = self.outbox, []
        return m


def fmt_entry(sym, pos, risk_pct, balance, decision):
    e = "🟢" if pos["side"] == "long" else "🔴"
    return (f"{e} PAPER ENTRY {pos['side'].upper()} {sym}  [{decision}]\n"
            f"Entry {pos['entry']:.4f}\nSL {pos['sl']:.4f}\n"
            f"TP1 {pos['tp1']:.4f} (50%) | TP2 {pos['tp2']:.4f} (25%) | TP3 {pos['tp3']:.4f} (rest)\n"
            f"Risk {risk_pct * 100:.2f}% = ${pos['risk_amt']:.2f} | Size {pos['qty']:.5f}\n"
            f"Balance ${balance:.2f}")


# ----------------------------------------------------------------- telegram / state
def notify(text):
    print(text, flush=True)
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=10)
    except Exception as e:
        print("telegram error:", e)


def load_state():
    a = Account(CFG["start_balance"])
    if os.path.exists(CFG["state_file"]):
        with open(CFG["state_file"]) as f:
            a.__dict__.update(json.load(f))
        a.outbox = []
    else:
        a.meta["fresh"] = True
    return a


def save_state(a):
    os.makedirs(os.path.dirname(CFG["state_file"]) or ".", exist_ok=True)
    a.meta.pop("fresh", None)
    tmp = CFG["state_file"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a.__dict__, f, default=float)
    os.replace(tmp, CFG["state_file"])


def flush_trades(a):
    n = a.meta.get("logged", 0)
    if len(a.closed) <= n:
        return
    os.makedirs(os.path.dirname(CFG["trades_file"]) or ".", exist_ok=True)
    new = not os.path.exists(CFG["trades_file"])
    with open(CFG["trades_file"], "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "symbol", "side", "entry", "pnl", "R", "reason"])
        if new:
            w.writeheader()
        w.writerows(a.closed[n:])
    a.meta["logged"] = len(a.closed)


# ----------------------------------------------------------------- daily trading assessment
def trend_label(df, pts):
    c = df.close
    if len(c) < 200:
        return None, "not enough candles"
    p, e50, e200 = c.iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
    if p > e50 > e200:
        return pts, "UP"
    if p < e50 < e200:
        return pts, "DOWN"
    if p > e200 and e50 > e200:
        return pts / 2, "weak UP"
    if p < e200 and e50 < e200:
        return pts / 2, "weak DOWN"
    return 0, "MIXED"


def load_events(now):
    """events.txt: one UTC datetime per line (high-impact news), plus a 'checked: YYYY-MM-DD' line.
    Counts as available data only if it was reviewed in the last 7 days."""
    p = CFG["events_file"]
    if not os.path.exists(p):
        return None, "events.txt missing"
    checked, ev = None, []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            if line.lower().startswith("checked:"):
                try:
                    checked = pd.Timestamp(line.split(":", 1)[1].strip(), tz="UTC")
                except Exception:
                    pass
                continue
            try:
                t = pd.Timestamp(line)
                ev.append(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"))
            except Exception:
                pass
    if checked is None or (now - checked) > pd.Timedelta(days=7):
        return None, "events.txt not reviewed in the last 7 days"
    return ev, ""


def assess(ex, now, data, b1):
    """Score the day out of 100 using only data that is really available.
    A component whose data is missing is NOT scored (no free points): the score is then
    normalised over the available points, flagged INCOMPLETE, and can never reach TRADE."""
    comps, notes, blocks, best = {}, {}, [], []

    def put(name, mx, val, note):
        comps[name] = (val, mx)
        notes[name] = note

    btc = data["BTC/USDT"]

    # 1) Market Structure (20): BTC 1D + 4H trend
    try:
        d1 = closed_only(to_df(ex.fetch_ohlcv("BTC/USDT", "1d", limit=300), 86_400_000), now)
        s1, l1 = trend_label(d1, 10)
        s4, l4 = trend_label(btc["d4"], 10)
        if s1 is None or s4 is None:
            raise ValueError("not enough candles")
        put("Market Structure", 20, s1 + s4, f"BTC 1D {l1} / 4H {l4}")
        if s1 + s4 == 0:
            blocks.append("no clear BTC trend (strategy is trend-only)")
    except Exception as e:
        put("Market Structure", 20, None, f"unavailable ({type(e).__name__})")

    # 2) Volatility (10): BTC 15m ATR as % of price, last 24 candles
    try:
        a = float((btc["d"].atr / btc["d"].close).tail(24).mean())
        if not np.isfinite(a):
            raise ValueError("nan")
        v = 10 if 0.0015 <= a <= 0.012 else 5 if (0.001 <= a < 0.0015 or 0.012 < a <= 0.02) else 0
        put("Volatility", 10, v, f"BTC 15m ATR {a * 100:.2f}%")
    except Exception as e:
        put("Volatility", 10, None, f"unavailable ({type(e).__name__})")

    # 3) Liquidity (10): average top-of-book spread
    try:
        sp = []
        for s in CFG["symbols"]:
            ob = ex.fetch_order_book(s, limit=5)
            bid, ask = ob["bids"][0][0], ob["asks"][0][0]
            sp.append((ask - bid) / ((ask + bid) / 2) * 1e4)
        avg = float(np.mean(sp))
        put("Liquidity", 10, 10 if avg <= 2 else 6 if avg <= 5 else 0, f"avg spread {avg:.1f} bps")
        if avg > 5:
            blocks.append("spread too high")
    except Exception as e:
        put("Liquidity", 10, None, f"unavailable ({type(e).__name__})")

    # 4) Trading Volume (10): BTC last 24h vs average of the previous 7 days
    try:
        if len(b1) < 24 * 8:
            raise ValueError("not enough candles")
        v24 = float(b1.volume.tail(24).sum())
        base = float(b1.volume.iloc[:-24].tail(24 * 7).sum() / 7)
        if base <= 0:
            raise ValueError("no volume")
        ratio = v24 / base
        put("Trading Volume", 10, 10 if ratio >= 0.8 else 5 if ratio >= 0.5 else 0,
            f"BTC 24h volume = {ratio:.2f}x of 7d average")
    except Exception as e:
        put("Trading Volume", 10, None, f"unavailable ({type(e).__name__})")

    # 5) Technical Opportunities (20): symbols aligned with the strategy filters / in pullback
    try:
        pts, parts = 0, []
        for s in CFG["symbols"]:
            r = data[s]["d"].iloc[-1]
            lg, st = bool(r.up4 and r.bup), bool(r.down4 and r.bdown)
            p = 4 if (lg or st) else 0
            if (lg and r.rsi_min < 42 and r.touch_dn) or (st and r.rsi_max > 58 and r.touch_up):
                p += 3
            pts += p
            name = s.split("/")[0]
            parts.append(f"{name} {'pullback' if p == 7 else 'aligned' if p == 4 else '-'}")
            if p:
                best.append(f"{name} ({'pullback forming' if p == 7 else 'trend aligned'})")
        put("Technical Opportunities", 20, min(20, pts), " | ".join(parts))
    except Exception as e:
        put("Technical Opportunities", 20, None, f"unavailable ({type(e).__name__})")

    # 6) Derivatives & Sentiment (10): perpetual funding rates (open interest NOT included)
    try:
        fr = []
        for s in CFG["symbols"]:
            try:
                fr.append(abs(float(ex.fetch_funding_rate(s.replace("/USDT", "/USDT:USDT"))["fundingRate"])))
            except Exception:
                pass
        if not fr:
            raise ValueError("no funding data")
        m = max(fr)
        put("Derivatives & Sentiment", 10, 10 if m <= 0.0002 else 5 if m <= 0.0005 else 0,
            f"max |funding| {m * 100:.3f}% (open interest not included)")
    except Exception as e:
        put("Derivatives & Sentiment", 10, None, f"unavailable ({type(e).__name__})")

    # 7) News & Economic Risk (10): from events.txt that YOU maintain
    ev, why = load_events(now)
    if ev is None:
        put("News & Economic Risk", 10, None, why)
    else:
        near = [t for t in ev if abs((t - now).total_seconds()) <= 3600]
        soon = [t for t in ev if 0 < (t - now).total_seconds() <= 6 * 3600]
        if near:
            put("News & Economic Risk", 10, 0, f"high-impact event within 60 min ({near[0]:%H:%M} UTC)")
            blocks.append("high-impact news within 60 minutes")
        elif soon:
            put("News & Economic Risk", 10, 5, f"event today at {soon[0]:%H:%M} UTC")
        else:
            put("News & Economic Risk", 10, 10, "no listed event in the next 6h")

    core = [n for n in ("Market Structure", "Volatility", "Liquidity") if comps[n][0] is None]
    if core:
        blocks.append("core live data unavailable: " + ", ".join(core))

    earned = sum(v for v, _ in comps.values() if v is not None)
    avail = sum(m for v, m in comps.values() if v is not None)
    missing = [n for n, (v, _) in comps.items() if v is None]
    pct = earned / avail * 100 if avail else 0.0

    if blocks or avail < 50:
        dec = "NO TRADE"
    elif pct >= 80:
        dec = "TRADE"
    elif pct >= 65:
        dec = "SELECTIVE TRADE"
    elif pct >= 50:
        dec = "HIGH CAUTION"
    else:
        dec = "NO TRADE"
    if missing and dec == "TRADE":
        dec = "SELECTIVE TRADE"      # incomplete data can never give a full green light

    return dict(comps=comps, notes=notes, blocks=blocks, best=best, earned=earned, avail=avail,
                pct=pct, missing=missing, incomplete=bool(missing), decision=dec)


def session_name(now):
    h = now.hour
    s = "Asia" if h < 7 else "Europe" if h < 13 else "US" if h < 21 else "Late US / low liquidity"
    return s + (" (weekend)" if now.weekday() >= 5 else "")


def fmt_report(a, now):
    L = ["📋 DAILY TRADING REPORT",
         f"Date: {now:%Y-%m-%d %H:%M} UTC | Session: {session_name(now)}"]
    for n, (v, mx) in a["comps"].items():
        L.append(f"• {n}: {'n/a' if v is None else format(v, 'g')}/{mx} - {a['notes'][n]}")
    if a["avail"]:
        L.append(f"Daily Trading Score: {a['earned']:g}/{a['avail']:g} = {a['pct']:.0f}/100"
                 + (" (INCOMPLETE)" if a["incomplete"] else ""))
    L.append(f"Final Decision: {a['decision']} | risk x{RISK_MULT[a['decision']]}")
    if a["missing"]:
        L.append("Not scored (missing data): " + ", ".join(a["missing"]) + " - TRADE is capped at SELECTIVE")
    if a["blocks"]:
        L.append("Mandatory blocks: " + "; ".join(a["blocks"]))
    L.append("Best Opportunities: " + (", ".join(a["best"]) if a["best"] else "none right now"))
    warn = [n for n, (v, mx) in a["comps"].items() if v is not None and v < mx / 2]
    if now.weekday() >= 5:
        warn.append("weekend liquidity")
    L.append("Risk Warning: " + (", ".join(warn) if warn else "no major flags (not a guarantee)"))
    L.append("Conditions: entry only on a closed 15m signal passing all filters; stop after 2 losses or -2% day.")
    return "\n".join(L)


# ----------------------------------------------------------------- one pass of the live logic
def manage(ex, acc, sym, d, now, last_1m):
    """Replay every 1m candle since the last check against the open position."""
    pos = acc.positions[sym]
    lm = max(last_1m.get(sym, 0), pos["open_ts"] * 1000)
    for _ in range(12):
        raw = ex.fetch_ohlcv(sym, "1m", since=int(lm) + 1, limit=300)
        if not raw:
            break
        m1 = closed_only(to_df(raw, 60_000), now)
        m1 = m1[m1.ts > lm]
        if m1.empty:
            break
        for _, c in m1.iterrows():
            lm = int(c.ts)
            last_1m[sym] = lm
            msgs = acc.on_candle(sym, dict(ts=c.open_time, high=float(c.high), low=float(c.low),
                                           close=float(c.close), ema20=ema_at(d, c.close_time)))
            for m in msgs:
                notify(m)
            if sym not in acc.positions:
                return


def step(ex, acc, now):
    meta = acc.meta
    last_sig = meta.setdefault("last_sig", {})
    last_1m = meta.setdefault("last_1m", {})
    if acc.roll(now):
        notify(f"📊 Daily summary | Balance ${acc.balance:.2f} "
               f"({(acc.balance / acc.start - 1) * 100:+.2f}% total) | closed trades: {len(acc.closed)}")

    b1 = closed_only(to_df(ex.fetch_ohlcv("BTC/USDT", "1h", limit=300), 3_600_000), now)
    data = {}
    for sym in CFG["symbols"]:
        d15 = closed_only(to_df(ex.fetch_ohlcv(sym, "15m", limit=300), 900_000), now)
        d4 = closed_only(to_df(ex.fetch_ohlcv(sym, "4h", limit=300), 14_400_000), now)
        data[sym] = dict(d4=d4, d=build(d15, d4, b1))

    # 1) open positions are always managed, whatever the daily decision is
    for sym in list(acc.positions):
        manage(ex, acc, sym, data[sym]["d"], now, last_1m)

    # 2) daily assessment (re-evaluated every pass; full report once per UTC day)
    try:
        a = assess(ex, now, data, b1)
    except Exception as e:
        a = dict(comps={}, notes={}, blocks=[f"assessment failed: {type(e).__name__}"], best=[], earned=0,
                 avail=0, pct=0.0, missing=[], incomplete=True, decision="NO TRADE")
    day = now.strftime("%Y-%m-%d")
    reason = "; ".join(a["blocks"]) or f"score {a['pct']:.0f}/100"
    if meta.get("report_day") != day:
        notify(fmt_report(a, now))
        meta["report_day"] = day
    elif meta.get("last_decision") not in (None, a["decision"]):
        notify(f"🔄 Decision changed: {meta['last_decision']} -> {a['decision']} ({reason})")
    meta["last_decision"] = a["decision"]
    mult = RISK_MULT[a["decision"]]

    # 3) new entries: one look per closed 15m candle
    for sym in CFG["symbols"]:
        d = data[sym]["d"]
        if len(d) < 60:
            continue
        row = d.iloc[-1]
        ts = int(row.ts)
        if last_sig.get(sym) == ts:
            continue
        last_sig[sym] = ts
        side = "long" if row.long_sig else ("short" if row.short_sig else None)
        if not side or sym in acc.positions or len(acc.positions) >= CFG["max_positions"]:
            continue
        tag = f"Signal {side.upper()} {sym} skipped:"
        if now - row.close_time > pd.Timedelta(minutes=CFG["max_signal_age_min"]):
            notify(f"⏸ {tag} signal is stale (late run)")
            continue
        if mult == 0:
            notify(f"⏸ {tag} today is NO TRADE ({reason})")
            continue
        ok, why = acc.can_trade()
        if not ok:
            notify(f"⏸ {tag} {why}")
            continue
        px = float(ex.fetch_ticker(sym)["last"])
        if abs(px - float(row.close)) > 0.3 * float(row.atr):
            notify(f"⏸ {tag} price already ran away")
            continue
        fill = px * (1 + (1 if side == "long" else -1) * CFG["slip"])
        plan = make_plan(row, side, fill)
        if not plan:
            notify(f"⏸ {tag} stop too tight for the trading costs")
            continue
        rp = risk_for(now) * mult
        pos = acc.open(sym, plan, rp, now)
        last_1m[sym] = int(now.timestamp() * 1000)
        notify(fmt_entry(sym, pos, rp, acc.balance, a["decision"]))


def once():
    os.makedirs(CFG["data_dir"], exist_ok=True)
    ex = make_exchange()
    acc = load_state()
    if acc.meta.get("fresh"):
        notify(f"🤖 Bot started (PAPER, {CFG['exchange']}) | Balance ${acc.balance:.2f}")
    try:
        step(ex, acc, pd.Timestamp.now(tz="UTC"))
    finally:
        flush_trades(acc)
        save_state(acc)


def live():
    os.makedirs(CFG["data_dir"], exist_ok=True)
    ex = make_exchange()
    acc = load_state()
    notify(f"🤖 Bot started (PAPER, {CFG['exchange']}) | Balance ${acc.balance:.2f} | {', '.join(CFG['symbols'])}")
    while True:
        try:
            step(ex, acc, pd.Timestamp.now(tz="UTC"))
        except Exception:
            traceback.print_exc()
            time.sleep(30)
        finally:
            flush_trades(acc)
            save_state(acc)
        time.sleep(CFG["loop_sleep"])


# ----------------------------------------------------------------- backtest
def simulate(sym, d):
    acc = Account(CFG["start_balance"])
    pending, curve, prev_e = None, [], float("nan")
    for _, r in d.iterrows():
        acc.roll(r.open_time)
        if pending and sym not in acc.positions:
            side, srow = pending
            fill = float(r.open) * (1 + (1 if side == "long" else -1) * CFG["slip"])
            plan = make_plan(srow, side, fill)
            if plan and acc.can_trade()[0]:
                acc.open(sym, plan, risk_for(r.open_time), r.open_time)
        pending = None
        acc.on_candle(sym, dict(ts=r.open_time, high=r.high, low=r.low, close=r.close, ema20=prev_e))
        acc.outbox.clear()
        prev_e = r.ema20
        curve.append(acc.balance)
        if sym not in acc.positions and (r.long_sig or r.short_sig):
            pending = ("long" if r.long_sig else "short", r)
    return acc, curve


def stats(name, acc, curve):
    tr = acc.closed
    if not tr:
        print(f"{name}: no trades")
        return
    pnl = np.array([t["pnl"] for t in tr])
    R = np.array([t["R"] for t in tr])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    pf = gp / gl if gl > 0 else float("inf")
    c = np.array(curve)
    dd = ((np.maximum.accumulate(c) - c) / np.maximum.accumulate(c)).max() * 100
    print(f"{name:10s} trades {len(tr):4d} | win {np.mean(pnl > 0) * 100:5.1f}% | avgR {R.mean():+.2f} | "
          f"PF {pf:4.2f} | return {(acc.balance / acc.start - 1) * 100:+7.2f}% | maxDD {dd:5.2f}%")


def backtest(days):
    ex = make_exchange()
    day = 86_400_000
    since = ex.milliseconds() - days * day
    print(f"Downloading data from {CFG['exchange']} ({days} days)... this takes a few minutes")
    b1 = to_df(fetch_range(ex, "BTC/USDT", "1h", since - 10 * day), 3_600_000)
    allrecs = []
    for sym in CFG["symbols"]:
        d15 = to_df(fetch_range(ex, sym, "15m", since - 3 * day), 900_000)
        d4 = to_df(fetch_range(ex, sym, "4h", since - 45 * day), 14_400_000)
        d = build(d15, d4, b1)
        d = d[d.open_time >= pd.Timestamp(since, unit="ms", tz="UTC")].reset_index(drop=True)
        acc, curve = simulate(sym, d)
        stats(sym, acc, curve)
        allrecs += acc.closed
    if allrecs:
        with open("backtest_trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(allrecs[0].keys()))
            w.writeheader()
            w.writerows(allrecs)
        print("Saved backtest_trades.csv")
    print("Each symbol runs on its own 1000$ account with fees + slippage. The daily assessment is not "
          "included (no history for it). Results are NOT a promise.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "once", "backtest"])
    ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args()
    if a.mode == "live":
        live()
    elif a.mode == "once":
        once()
    else:
        backtest(a.days)
