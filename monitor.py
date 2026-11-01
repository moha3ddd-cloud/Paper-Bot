"""
Daily trend monitor (PAPER, fake money). Runs once a day.

Rule (fixed in advance, not tuned): for each symbol, on the DAILY chart
    EMA50 > EMA200  -> hold the coin (long only)      else -> cash
Each symbol has its own sleeve (start balance / number of symbols). Entries and exits happen at the
current price right after the daily close, with 0.10% cost per side.

Sends Telegram only when a position changes, plus one short daily status line.
State: data/trend_state.json
"""
import os
import json

import pandas as pd

import bot

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
START = 1000.0
COST = 0.001
STATE = os.path.join("data", "trend_state.json")


def load():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"start": START, "sleeves": {s: {"cash": START / len(SYMS), "units": 0.0, "entry": None, "since": None}
                                        for s in SYMS},
            "last_day": None, "trades": [], "started": None}


def save(st):
    os.makedirs("data", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def run(ex, st, now):
    day = now.strftime("%Y-%m-%d")
    if st["last_day"] == day:
        print("already processed today")
        return False
    msgs, rows = [], []
    for s in SYMS:
        sl = st["sleeves"][s]
        d = bot.closed_only(bot.to_df(ex.fetch_ohlcv(s, "1d", limit=300), 86_400_000), now)
        name = s.split("/")[0]
        if len(d) < 210:
            rows.append(f"{name}: not enough data")
            continue
        ef, es = bot.ema(d.close, 50).iloc[-1], bot.ema(d.close, 200).iloc[-1]
        want = bool(ef > es)
        px = float(ex.fetch_ticker(s)["last"])
        if want and sl["units"] == 0:
            sl["units"] = sl["cash"] * (1 - COST) / px
            sl["cash"], sl["entry"], sl["since"] = 0.0, px, day
            msgs.append(f"🟢 PAPER BUY {name} @ {px:.4f} (EMA50 above EMA200)")
        elif not want and sl["units"] > 0:
            val = sl["units"] * px * (1 - COST)
            pnl = (px / sl["entry"] - 1) * 100
            st["trades"].append(dict(symbol=s, entry=sl["entry"], exit=px, since=sl["since"], until=day,
                                     pct=round(pnl, 2)))
            msgs.append(f"🔴 PAPER SELL {name} @ {px:.4f} (EMA50 below EMA200) | trade {pnl:+.1f}% "
                        f"| held since {sl['since']}")
            sl["cash"], sl["units"], sl["entry"], sl["since"] = val, 0.0, None, None
        gap = (ef / es - 1) * 100
        rows.append(f"{name}: {'IN' if sl['units'] > 0 else 'cash'} (EMA gap {gap:+.1f}%)")
    eq = 0.0
    for s in SYMS:
        sl = st["sleeves"][s]
        eq += sl["cash"] + (sl["units"] * float(ex.fetch_ticker(s)["last"]) if sl["units"] > 0 else 0.0)
    if st["started"] is None:
        st["started"] = day
        msgs.insert(0, f"🤖 Daily trend monitor started (PAPER). Start balance ${st['start']:.0f}, "
                       f"{len(SYMS)} coins, rule: daily EMA50 > EMA200 = hold, else cash.")
    for m in msgs:
        bot.notify(m)
    bot.notify(f"📅 {day} | equity ${eq:.2f} ({(eq / st['start'] - 1) * 100:+.1f}% since {st['started']}) | "
               + " | ".join(rows))
    st["last_day"] = day
    return True


def main():
    ex = bot.make_exchange()
    st = load()
    try:
        run(ex, st, pd.Timestamp.now(tz="UTC"))
    finally:
        save(st)


if __name__ == "__main__":
    main()
