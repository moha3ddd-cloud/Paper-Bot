"""
Out-of-sample strategy research (no parameter tuning).

Pre-registered rules (written BEFORE looking at results):
  * 5 liquid symbols, DAYS of history, chronological split: first 65% = train, last 35% = test.
  * 3 ideas only, fixed in advance (limits the multiple-comparison problem):
      A  current strategy (15m pullback)               <- baseline
      B  same pullback logic on 1h candles             <- costs are a smaller share of each move
      C  1h Donchian-20 breakout with the same trend filters
  * A variant is only "promising" if on the TEST period: >= 50 trades, profit factor >= 1.3,
    average R > 0, the 95% bootstrap interval of average R mostly positive, and PF > 1 on train too.
  * Each variant is also run with zero costs, to see whether costs or the idea itself is the problem.

Usage: python research.py   (env: EXCHANGE=okx, DAYS=365)
"""
import os
import numpy as np
import pandas as pd

import bot

SYMS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
DAYS = int(os.getenv("DAYS", "365"))
SPLIT = 0.65
DAY_MS = 86_400_000
VARIANTS = {"A_pullback_15m": 16, "B_pullback_1h": 48, "C_breakout_1h": 48}   # name -> max hold hours


def load(ex, since):
    raw = {}
    for s in SYMS:
        print("download", s, flush=True)
        raw[s] = dict(
            m15=bot.to_df(bot.fetch_range(ex, s, "15m", since - 3 * DAY_MS), 900_000),
            h1=bot.to_df(bot.fetch_range(ex, s, "1h", since - 10 * DAY_MS), 3_600_000),
            h4=bot.to_df(bot.fetch_range(ex, s, "4h", since - 45 * DAY_MS), 14_400_000),
        )
    return raw


def make(variant, sym, raw):
    r, btc = raw[sym], raw["BTC/USDT"]
    if variant == "A_pullback_15m":
        return bot.build(r["m15"], r["h4"], btc["h1"])
    d = bot.build(r["h1"], r["h4"], btc["h4"])          # BTC trend filter on 4h for 1h entries
    if variant == "B_pullback_1h":
        return d
    hh = d.high.rolling(20).max().shift(1)
    ll = d.low.rolling(20).min().shift(1)
    atrp = d.atr / d.close
    ok = (atrp > 0.0015) & (atrp < 0.03) & (d.volume > d.volma)
    d["long_sig"] = d.up4 & d.bup & ok & (d.close > hh)
    d["short_sig"] = d.down4 & d.bdown & ok & (d.close < ll)
    return d


def pooled(items, rng):
    tr = [t for _, acc, _ in items for t in acc.closed]
    if not tr:
        return None
    pnl = np.array([t["pnl"] for t in tr])
    R = np.array([t["R"] for t in tr])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    boots = rng.choice(R, size=(2000, len(R))).mean(axis=1)
    per_sym_pf = []
    for _, acc, _ in items:
        p = np.array([t["pnl"] for t in acc.closed])
        if len(p):
            g, l = p[p > 0].sum(), -p[p < 0].sum()
            per_sym_pf.append(g / l if l > 0 else 9.99)
    dd = 0.0
    for _, _, curve in items:
        c = np.array(curve)
        if len(c):
            dd = max(dd, ((np.maximum.accumulate(c) - c) / np.maximum.accumulate(c)).max() * 100)
    return dict(n=len(R), win=float(np.mean(pnl > 0) * 100), avgR=float(R.mean()),
                pf=float(gp / gl) if gl > 0 else 9.99, lo=float(np.percentile(boots, 2.5)),
                hi=float(np.percentile(boots, 97.5)), dd=float(dd),
                pos=sum(1 for x in per_sym_pf if x > 1), nsym=len(per_sym_pf))


def line(label, s):
    if not s:
        return f"  {label:6s} no trades"
    return (f"  {label:6s} trades {s['n']:4d} | win {s['win']:5.1f}% | avgR {s['avgR']:+.3f} "
            f"[{s['lo']:+.2f},{s['hi']:+.2f}] | PF {s['pf']:4.2f} | maxDD {s['dd']:4.1f}% | "
            f"symbols PF>1: {s['pos']}/{s['nsym']}")


def run(variant, raw, t0, tcut, t1, rng):
    out = {}
    for name, (a, b) in {"train": (t0, tcut), "test": (tcut, t1)}.items():
        items = []
        for s in SYMS:
            d = make(variant, s, raw)
            seg = d[(d.open_time >= a) & (d.open_time < b)].reset_index(drop=True)
            if len(seg) < 100:
                continue
            acc, curve = bot.simulate(s, seg)
            items.append((s, acc, curve))
        out[name] = pooled(items, rng)
    return out


def main():
    ex = bot.make_exchange()
    now_ms = ex.milliseconds()
    since = now_ms - DAYS * DAY_MS
    raw = load(ex, since)
    t0 = pd.Timestamp(since, unit="ms", tz="UTC")
    t1 = pd.Timestamp(now_ms, unit="ms", tz="UTC")
    tcut = t0 + (t1 - t0) * SPLIT
    rng = np.random.default_rng(42)
    base = dict(bot.CFG)
    L = [f"RESEARCH {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} | train until {tcut:%Y-%m-%d} | exchange {bot.CFG['exchange']}",
         f"symbols: {', '.join(s.split('/')[0] for s in SYMS)} | costs: fee {base['fee'] * 100:.2f}%/side + slip {base['slip'] * 100:.2f}%",
         "avgR [95% bootstrap interval]. PF = profit factor.", ""]
    verdict = {}
    for v, hold in VARIANTS.items():
        L.append(v)
        for tag, fee, slip in (("NET", base["fee"], base["slip"]), ("GROSS (no costs)", 0.0, 0.0)):
            bot.CFG.update(fee=fee, slip=slip, max_hold_h=hold)
            r = run(v, raw, t0, tcut, t1, rng)
            L.append(f" {tag}")
            L.append(line("train", r["train"]))
            L.append(line("test", r["test"]))
            if tag == "NET":
                verdict[v] = r
        bot.CFG.update(base)
        L.append("")
    L.append("PRE-REGISTERED CHECK (net, test period):")
    for v, r in verdict.items():
        te, tr = r["test"], r["train"]
        if not te or not tr:
            L.append(f"  {v}: not enough trades -> FAIL")
            continue
        checks = [("test trades >= 50", te["n"] >= 50), ("test PF >= 1.3", te["pf"] >= 1.3),
                  ("test avgR > 0", te["avgR"] > 0), ("CI low > -0.05", te["lo"] > -0.05),
                  ("train PF > 1", tr["pf"] > 1)]
        L.append(f"  {v}: {'PASS' if all(c for _, c in checks) else 'FAIL'}  "
                 + ", ".join(f"{n}={'ok' if c else 'NO'}" for n, c in checks))
    text = "\n".join(L)
    print(text)
    os.makedirs("data", exist_ok=True)
    with open("data/research_result.txt", "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
