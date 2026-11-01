"""
M1 live paper test (fake money). Runs once a day.

Rule (fixed in advance, not tuned): every 7 days rank the coins by their 30-day return and hold the top 3
with equal weight (long only, 1x). Cost 0.10% per unit of turnover.
Status of the idea: it passed the development stage of the lab only barely (see data/lab_dev_result.txt).
This forward test is the real evidence. State: data/m1_state.json
"""
import os
import json

import pandas as pd

import bot
import lab

UNIVERSE = lab.UNIVERSE
START, COST, TOP, LOOK, EVERY = 1000.0, 0.001, 3, 30, 7
STATE = os.path.join("data", "m1_state.json")


def load():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"start": START, "cash": START, "units": {}, "last_rebal": None, "last_day": None,
            "started": None, "rebalances": 0}


def save(st):
    os.makedirs("data", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def run(ex, st, now):
    day = now.strftime("%Y-%m-%d")
    if st["last_day"] == day:
        print("M1: already processed today")
        return False
    mom, px = {}, {}
    for u in UNIVERSE:
        s = f"{u}/USDT"
        try:
            d = bot.closed_only(bot.to_df(ex.fetch_ohlcv(s, "1d", limit=60), 86_400_000), now)
            if len(d) < LOOK + 1:
                continue
            mom[u] = float(d.close.iloc[-1] / d.close.iloc[-1 - LOOK] - 1)
            px[u] = float(ex.fetch_ticker(s)["last"])
        except Exception:
            continue
    missing = [u for u in st["units"] if u not in px]
    if missing or len(mom) < TOP:
        print("M1: price data missing for", missing or "universe", "- skipping today")
        return False

    equity = st["cash"] + sum(q * px[u] for u, q in st["units"].items())
    msgs = []
    if st["started"] is None:
        st["started"] = day
        msgs.append(f"🤖 M1 live paper test started: every 7 days hold the top {TOP} coins by 30-day return. "
                    f"Start ${st['start']:.0f}.")
    due = st["last_rebal"] is None or (pd.Timestamp(day) - pd.Timestamp(st["last_rebal"])).days >= EVERY
    if due:
        top = sorted(mom, key=mom.get, reverse=True)[:TOP]
        per = equity / TOP
        turnover = sum(abs((per if u in top else 0.0) - st["units"].get(u, 0.0) * px.get(u, 0.0))
                       for u in set(top) | set(st["units"]))
        equity -= COST * turnover
        per = equity / TOP
        old = set(st["units"])
        st["units"] = {u: per / px[u] for u in top}
        st["cash"], st["last_rebal"] = 0.0, day
        st["rebalances"] += 1
        msgs.append(f"🔄 M1 rebalance | BUY {', '.join(sorted(set(top) - old)) or '-'} | "
                    f"SELL {', '.join(sorted(old - set(top))) or '-'} | holding {', '.join(top)} "
                    f"| equity ${equity:.2f}")
    held = ", ".join(f"{u} ({mom[u] * 100:+.0f}% 30d)" for u in st["units"])
    msgs.append(f"📅 M1 {day} | equity ${equity:.2f} ({(equity / st['start'] - 1) * 100:+.1f}% since {st['started']}) "
                f"| holding {held}")
    for m in msgs:
        bot.notify(m)
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
