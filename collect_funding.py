"""
Daily collector of perpetual funding-rate history (OKX only gives ~3 months, so we keep our own copy).
Appends new settlements to data/funding_history.csv (columns: ts_ms, symbol, rate). Safe to run repeatedly.
"""
import os
import csv

import bot

COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC"]
FILE = os.path.join("data", "funding_history.csv")
DAY_MS = 86_400_000


def load():
    rows = {}
    if os.path.exists(FILE):
        with open(FILE, newline="") as f:
            for r in csv.DictReader(f):
                rows[(int(r["ts_ms"]), r["symbol"])] = float(r["rate"])
    return rows


def main():
    ex = bot.make_exchange()
    rows = load()
    added = 0
    for u in COINS:
        sym = f"{u}/USDT:USDT"
        last = max((t for t, s in rows if s == u), default=None)
        t = (last + 1) if last else ex.milliseconds() - 100 * DAY_MS
        try:
            for _ in range(40):
                b = ex.fetch_funding_rate_history(sym, since=int(t), limit=100)
                if not b:
                    break
                for r in b:
                    k = (int(r["timestamp"]), u)
                    if k not in rows and r.get("fundingRate") is not None:
                        rows[k] = float(r["fundingRate"])
                        added += 1
                nt = int(b[-1]["timestamp"]) + 1
                if nt <= t:
                    break
                t = nt
        except Exception as e:
            print("skip", u, type(e).__name__, str(e)[:80])
    os.makedirs("data", exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "symbol", "rate"])
        for (ts, s), rate in sorted(rows.items()):
            w.writerow([ts, s, rate])
    os.replace(tmp, FILE)
    print(f"funding records added: {added} | total: {len(rows)}")


if __name__ == "__main__":
    main()
