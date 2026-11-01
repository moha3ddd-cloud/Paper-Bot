"""
Strategy Lab (daily data, long-only, portfolio level) with a SEALED VAULT.

Rules enforced by this code:
  * `dev` mode cuts the last VAULT_DAYS days off the data BEFORE any candidate is computed
    (and asserts it), so development can never see the vault.
  * every dev run adds its candidates to data/lab_log.json (attempt counter) and the Sharpe bar rises
    with the number of attempts.
  * `vault` mode runs ONE chosen candidate on the vault period, once. A second run is refused.

Pre-registered candidates (parameters fixed in advance, not tuned):
  M1  cross-sectional momentum: weekly, rank 30-day return, hold top 3 equal weight
  M2  same, but only coins whose 30-day return is positive (else cash)
  M3  daily EMA50>EMA200 trend per coin, sized by volatility (target 50% annual vol per coin, cap 1x)
Benchmarks: EW_BUY_HOLD (equal weight, daily rebalanced), T2_EW (EMA50/200 trend, no vol sizing).

Known bias: the coin list is today's liquid coins, so survivorship bias flatters every result
(including benchmarks). Treat results as an upper bound.

Usage: python lab.py dev
       python lab.py vault --candidate M2
"""
import os
import sys
import json
import argparse

import numpy as np
import pandas as pd

import bot

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT",
            "LTC", "TRX", "ATOM", "UNI", "NEAR", "BCH", "ETC", "FIL", "APT", "ARB"]
DAY_MS = 86_400_000
DAYS = int(os.getenv("DAYS", "1900"))
VAULT_DAYS = 365
COST = 0.001            # per unit of turnover (fee + slippage)
WARMUP = 210            # bars skipped at the start so every candidate is warm
BASE_SHARPE = 0.8
LOG = os.path.join("data", "lab_log.json")


# ----------------------------------------------------------------- data
def load_panels(ex):
    o, c = {}, {}
    since = ex.milliseconds() - DAYS * DAY_MS
    now = pd.Timestamp.now(tz="UTC")
    for u in UNIVERSE:
        s = f"{u}/USDT"
        try:
            d = bot.closed_only(bot.to_df(bot.fetch_range(ex, s, "1d", since), DAY_MS), now)
        except Exception as e:
            print("skip", s, type(e).__name__, flush=True)
            continue
        if len(d) < 400:
            print("skip (short history)", s, len(d), flush=True)
            continue
        idx = d.open_time.dt.floor("D")
        o[u] = pd.Series(d.open.values, index=idx)
        c[u] = pd.Series(d.close.values, index=idx)
        print("loaded", u, len(d), flush=True)
    return pd.DataFrame(o).sort_index(), pd.DataFrame(c).sort_index()


# ----------------------------------------------------------------- candidates (weights decided at day close)
def w_xsmom(c, lookback=30, top=3, abs_filter=False, rebal=7, regime=False):
    mom = c / c.shift(lookback) - 1
    btc_ok = (c["BTC"] > c["BTC"].ewm(span=200, adjust=False).mean()) & (c["BTC"].notna().cumsum() >= 200)
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    cur = pd.Series(0.0, index=c.columns)
    for i, t in enumerate(c.index):
        if i >= lookback and i % rebal == 0:
            m = mom.loc[t].dropna().sort_values(ascending=False).head(top)
            if abs_filter:
                m = m[m > 0]
            cur = pd.Series(0.0, index=c.columns)
            if len(m) and not (regime and not btc_ok.loc[t]):
                cur[m.index] = 1.0 / top
        W.loc[t] = cur
    return W


def trend_pos(c, fast=50, slow=200):
    ef, es = c.ewm(span=fast, adjust=False).mean(), c.ewm(span=slow, adjust=False).mean()
    pos = ((ef > es) & c.notna()).astype(float)
    pos[c.notna().cumsum() < slow] = 0.0
    return pos


def w_trend(c, vol_target=None):
    pos = trend_pos(c)
    avail = c.notna().sum(axis=1).replace(0, np.nan)
    if vol_target:
        vol = c.pct_change().rolling(30).std() * np.sqrt(365)
        pos = pos * (vol_target / vol).clip(upper=1.0).fillna(0.0)
    return pos.div(avail, axis=0).fillna(0.0)


def w_ew(c):
    n = c.notna().sum(axis=1).replace(0, np.nan)
    return c.notna().astype(float).div(n, axis=0).fillna(0.0)


CANDIDATES = {
    "M1": lambda c: w_xsmom(c),
    "M2": lambda c: w_xsmom(c, abs_filter=True),
    "M3": lambda c: w_trend(c, vol_target=0.5),
    "M4": lambda c: w_xsmom(c, regime=True),      # M1 + cash whenever BTC is below its daily EMA200
}
BENCH = {"EW_BUY_HOLD": w_ew, "T2_EW": lambda c: w_trend(c)}


# ----------------------------------------------------------------- engine
def run(o, W):
    """Weights decided at the close of day t are executed at the open of day t+1 and held to the open of t+2."""
    ro = o.shift(-1) / o - 1
    held = W.shift(1)
    ret = (held * ro).sum(axis=1)
    turn = (held - held.shift(1)).abs().sum(axis=1)
    return (ret - COST * turn).iloc[:-1], turn.iloc[:-1]


def stats(r):
    r = r.dropna()
    if len(r) < 90:
        return None
    eq = (1 + r).cumprod()
    sd = r.std()
    return dict(tot=(eq.iloc[-1] - 1) * 100, sh=float(r.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
                dd=float((1 - eq / eq.cummax()).max() * 100), yrs=len(r) / 365)


def fmt(label, s, extra=""):
    if not s:
        return f"  {label:9s} not enough data"
    return f"  {label:9s} return {s['tot']:+9.1f}% | Sharpe {s['sh']:+5.2f} | maxDD {s['dd']:5.1f}%{extra}"


# ----------------------------------------------------------------- log
def load_log():
    if os.path.exists(LOG):
        with open(LOG) as f:
            return json.load(f)
    return {"attempts": 0, "runs": [], "vault_used": None}


def vault_start_of(lg):
    """The vault start date is FIXED (not 'last day - 365'), so vault days can never leak into development.
    New data that arrives after sealing simply extends the vault."""
    return pd.Timestamp(lg.setdefault("vault_start", "2025-10-31"), tz="UTC")


def save_log(lg):
    os.makedirs("data", exist_ok=True)
    with open(LOG, "w") as f:
        json.dump(lg, f, indent=1)


def write_out(name, text):
    print(text)
    os.makedirs("data", exist_ok=True)
    with open(os.path.join("data", name), "w") as f:
        f.write(text + "\n")


# ----------------------------------------------------------------- modes
def dev(ex, only=""):
    lg = load_log()
    vault_start = vault_start_of(lg)
    o, c = load_panels(ex)
    o, c = o[o.index < vault_start], c[c.index < vault_start]
    assert c.index.max() < vault_start, "vault leak"          # development can never see the vault
    new = [only] if only else list(CANDIDATES)                 # only NEW candidates count as attempts
    assert all(n in CANDIDATES for n in new), "unknown candidate"
    lg["attempts"] += len(new)
    lg["runs"].append({"when": str(pd.Timestamp.now(tz="UTC")), "candidates": new})
    req = BASE_SHARPE + 0.05 * max(0, lg["attempts"] - 3)      # the first 3 candidates were the pre-registered batch

    res, turns = {}, {}
    for name, fn in {**CANDIDATES, **BENCH}.items():
        r, t = run(o, fn(c))
        res[name], turns[name] = r.iloc[WARMUP:], t.iloc[WARMUP:]
    idx = res["EW_BUY_HOLD"].index
    mid = idx[len(idx) // 2]
    t0, t1 = idx.min(), idx.max()
    L = [f"LAB DEV {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} | vault (sealed) starts {vault_start:%Y-%m-%d} | coins: {c.shape[1]} "
         f"({', '.join(c.columns)})",
         f"cost {COST * 100:.2f}% per unit turnover | attempts so far: {lg['attempts']} | required dev Sharpe: {req:.2f}",
         "survivorship bias: coin list is today's liquid coins -> results are an upper bound", ""]
    verdict = {}
    for name in list(CANDIDATES) + list(BENCH):
        r = res[name]
        a, h1, h2 = stats(r), stats(r[r.index < mid]), stats(r[r.index >= mid])
        L.append(name)
        L.append(fmt("all dev", a, f" | avg daily turnover {turns[name].mean() * 100:.1f}%"))
        L.append(fmt("1st half", h1))
        L.append(fmt("2nd half", h2))
        L.append("")
        verdict[name] = (a, h1, h2)
    L.append(f"DEV CHECK (candidates in this run: {', '.join(new)}; the others are shown for reference):")
    bh = verdict["EW_BUY_HOLD"][0]
    for name in new:
        a, h1, h2 = verdict[name]
        checks = [(f"Sharpe>={req:.2f}", a["sh"] >= req), ("both halves Sharpe>=0.3", h1["sh"] >= 0.3 and h2["sh"] >= 0.3),
                  ("beats buy&hold Sharpe", a["sh"] > bh["sh"])]
        L.append(f"  {name}: {'ELIGIBLE FOR VAULT' if all(x for _, x in checks) else 'FAIL'}  "
                 + ", ".join(f"{n}={'ok' if x else 'NO'}" for n, x in checks))
    write_out("lab_dev_result.txt", "\n".join(L))
    save_log(lg)


def vault(ex, cand):
    lg = load_log()
    if lg.get("vault_used"):
        print("REFUSED: the vault was already used:", lg["vault_used"])
        sys.exit(1)
    if cand not in CANDIDATES:
        print("unknown candidate", cand)
        sys.exit(1)
    o, c = load_panels(ex)
    vault_start = vault_start_of(lg)
    L = [f"LAB VAULT (one shot) candidate {cand} | {vault_start:%Y-%m-%d} -> {c.index.max():%Y-%m-%d} | "
         f"attempts before this: {lg['attempts']}", ""]
    out = {}
    for name, fn in {cand: CANDIDATES[cand], **BENCH}.items():
        r, t = run(o, fn(c))
        r = r[r.index >= vault_start]
        out[name] = stats(r)
        L.append(name)
        L.append(fmt("vault", out[name], f" | avg daily turnover {t[t.index >= vault_start].mean() * 100:.1f}%"))
    s = out[cand]
    L += ["", f"Uncertainty: 1 year of data -> Sharpe standard error about {1 / np.sqrt(s['yrs']):.2f}" if s else ""]
    write_out("lab_vault_result.txt", "\n".join(L))
    lg["vault_used"] = {"candidate": cand, "when": str(pd.Timestamp.now(tz="UTC")),
                        "sharpe": s["sh"] if s else None}
    save_log(lg)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["dev", "vault"])
    ap.add_argument("--candidate", default="")
    ap.add_argument("--only", default="", help="dev mode: run only this NEW candidate (counts as 1 attempt)")
    a = ap.parse_args(argv)
    ex = bot.make_exchange()
    dev(ex, a.only) if a.mode == "dev" else vault(ex, a.candidate)


if __name__ == "__main__":
    main()
