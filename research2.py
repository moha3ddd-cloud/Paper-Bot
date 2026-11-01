"""
Slow trend-following research (no parameter tuning, textbook parameters fixed in advance).

Pre-registered (written BEFORE looking at results):
  * 5 liquid symbols, DAYS of history (default ~5 years), equal-weight portfolio, 1x leverage.
  * Chronological split: first 60% = train, last 40% = test.
  * 3 ideas, parameters NOT tuned:
      T1  4h Donchian breakout 55 / exit 20, long + short   (classic "turtle")
      T2  daily EMA50 > EMA200 -> long, else flat           (long only)
      T3  daily 90-day momentum > 0 -> long, else flat      (long only)
  * Benchmark: buy & hold of the same portfolio.
  * Costs: 0.10% per unit of position change (fee + slippage). Perpetual funding is NOT modelled.
  * An idea is "promising" only if on the TEST period (net of costs):
      Sharpe >= 0.7, total return > 0, >= 20 trades (whole period), train Sharpe > 0, and it beats buy & hold
      on Sharpe OR has a max drawdown below 70% of buy & hold's.

Usage: python research2.py   (env: EXCHANGE=okx, DAYS=1800)
"""
import os
import numpy as np
import pandas as pd

import bot

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
DAYS = int(os.getenv("DAYS", "1800"))
SPLIT = 0.6
COST = 0.001
DAY_MS = 86_400_000
TF_MS = {"4h": 14_400_000, "1d": 86_400_000}


def pos_donchian(df, n_in=55, n_out=20, short=True):
    h, l, c = df.high, df.low, df.close.values
    hh = h.rolling(n_in).max().shift(1).values
    ll = l.rolling(n_in).min().shift(1).values
    xl = l.rolling(n_out).min().shift(1).values
    xh = h.rolling(n_out).max().shift(1).values
    p, cur = np.zeros(len(c)), 0
    for i in range(len(c)):
        if cur == 0:
            if c[i] > hh[i]:
                cur = 1
            elif short and c[i] < ll[i]:
                cur = -1
        elif cur == 1:
            if c[i] < xl[i]:
                cur = -1 if (short and c[i] < ll[i]) else 0
        else:
            if c[i] > xh[i]:
                cur = 1 if c[i] > hh[i] else 0
        p[i] = cur
    return p


def pos_ema(df, fast=50, slow=200):
    ef, es = bot.ema(df.close, fast), bot.ema(df.close, slow)
    p = (ef > es).astype(float).to_numpy(copy=True)
    p[:slow] = 0.0
    return p


def pos_mom(df, n=90):
    return (df.close / df.close.shift(n) - 1 > 0).astype(float).values


IDEAS = {
    "T1_donchian_4h": ("4h", pos_donchian),
    "T2_ema50_200_1d": ("1d", pos_ema),
    "T3_momentum90_1d": ("1d", pos_mom),
    "BUY_AND_HOLD": ("1d", lambda df: np.ones(len(df))),
}


def simulate_bars(df, pos):
    """Position decided at the close of bar i-1 is held during bar i (entry/exit at bar open)."""
    o, c = df.open.values, df.close.values
    held = np.concatenate([[0.0], pos[:-1]])
    prev = np.concatenate([[0.0], held[:-1]])
    gap = np.zeros(len(c))
    gap[1:] = o[1:] / c[:-1] - 1
    ret = prev * gap + held * (c / o - 1) - COST * np.abs(held - prev)
    trades = int(np.sum((held != prev) & (held != 0)))
    s = pd.Series(ret, index=df.open_time)
    daily = (1 + s).groupby(s.index.floor("D")).prod() - 1
    return daily, trades


def stats(r):
    r = r.dropna()
    if len(r) < 60:
        return None
    eq = (1 + r).cumprod()
    sd = r.std()
    return dict(tot=(eq.iloc[-1] - 1) * 100, sh=float(r.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
                dd=float((1 - eq / eq.cummax()).max() * 100), yrs=len(r) / 365)


def fmt(label, s, trades=None):
    if not s:
        return f"  {label:6s} not enough data"
    t = f" | trades {trades:4d}" if trades is not None else ""
    return f"  {label:6s} return {s['tot']:+8.1f}% | Sharpe {s['sh']:+5.2f} | maxDD {s['dd']:5.1f}%{t}"


def main():
    ex = bot.make_exchange()
    now_ms = ex.milliseconds()
    since = now_ms - DAYS * DAY_MS
    data = {}
    for s in SYMS:
        print("download", s, flush=True)
        data[s] = {tf: bot.to_df(bot.fetch_range(ex, s, tf, since - 60 * DAY_MS), ms) for tf, ms in TF_MS.items()}
        data[s] = {tf: d for tf, d in data[s].items() if len(d) > 300}

    daily, trades = {}, {}
    for name, (tf, fn) in IDEAS.items():
        cols, tr = {}, 0
        for s in SYMS:
            d = data[s].get(tf)
            if d is None or len(d) < 300:
                continue
            r, n = simulate_bars(d, fn(d))
            cols[s] = r
            tr += n
        daily[name] = pd.DataFrame(cols)
        trades[name] = tr

    port = {k: v.mean(axis=1, skipna=True) for k, v in daily.items()}
    idx = port["BUY_AND_HOLD"].index
    idx = idx[idx >= pd.Timestamp(since, unit="ms", tz="UTC")]
    t0, t1 = idx.min(), idx.max()
    tcut = t0 + (t1 - t0) * SPLIT
    sl = {"train": lambda x: x[(x.index >= t0) & (x.index < tcut)],
          "test": lambda x: x[(x.index >= tcut) & (x.index <= t1)],
          "all": lambda x: x[(x.index >= t0) & (x.index <= t1)]}

    L = [f"SLOW RESEARCH {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} | train until {tcut:%Y-%m-%d} | exchange {bot.CFG['exchange']}",
         f"portfolio = equal weight of {', '.join(s.split('/')[0] for s in SYMS)} | cost {COST * 100:.2f}% per side | funding not modelled", ""]
    res = {}
    for name in IDEAS:
        L.append(name)
        res[name] = {k: stats(f(port[name])) for k, f in sl.items()}
        for k in ("train", "test", "all"):
            L.append(fmt(k, res[name][k], trades[name] if k == "all" else None))
        pos_syms = sum(1 for s in daily[name].columns
                       if (st := stats(sl["test"](daily[name][s]))) and st["sh"] > 0)
        L.append(f"  symbols with test Sharpe > 0: {pos_syms}/{daily[name].shape[1]}")
        yr = port[name][(port[name].index >= t0)].groupby(lambda d: d.year).apply(lambda x: (1 + x).prod() - 1)
        L.append("  by year: " + " | ".join(f"{y}: {v * 100:+.0f}%" for y, v in yr.items()))
        L.append("")
    te = res["BUY_AND_HOLD"]["test"]
    if te:
        L.append(f"Uncertainty: test period is {te['yrs']:.1f} years, so a Sharpe estimate has a standard error of roughly "
                 f"{1 / np.sqrt(te['yrs']):.2f}. Differences smaller than that are noise.")
        L.append("")
    L.append("PRE-REGISTERED CHECK (net, test period, vs buy & hold):")
    bh = res["BUY_AND_HOLD"]
    for name in ("T1_donchian_4h", "T2_ema50_200_1d", "T3_momentum90_1d"):
        a, tr = res[name]["test"], res[name]["train"]
        if not a or not tr or not bh["test"]:
            L.append(f"  {name}: not enough data -> FAIL")
            continue
        # trades counted over the whole period; test share is not separated, so require >= 20 overall
        checks = [("Sharpe>=0.7", a["sh"] >= 0.7), ("return>0", a["tot"] > 0),
                  ("trades>=20", trades[name] >= 20), ("train Sharpe>0", tr["sh"] > 0),
                  ("beats B&H (Sharpe or DD<70%)", a["sh"] > bh["test"]["sh"] or a["dd"] < 0.7 * bh["test"]["dd"])]
        L.append(f"  {name}: {'PASS' if all(c for _, c in checks) else 'FAIL'}  "
                 + ", ".join(f"{n}={'ok' if c else 'NO'}" for n, c in checks))
    text = "\n".join(L)
    print(text)
    os.makedirs("data", exist_ok=True)
    with open("data/research2_result.txt", "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
