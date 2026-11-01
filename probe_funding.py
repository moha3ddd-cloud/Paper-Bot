"""Which exchanges give us long perpetual funding-rate history from a GitHub runner? (read-only probe)"""
import os
import ccxt

DAY_MS = 86_400_000
SYM = "BTC/USDT:USDT"
lines = []
for name in ["okx", "bitget", "gate", "bybit", "binance", "kucoinfutures", "mexc"]:
    try:
        ex = getattr(ccxt, name)({"enableRateLimit": True})
        t, rows = ex.milliseconds() - 1800 * DAY_MS, []
        for _ in range(80):
            b = ex.fetch_funding_rate_history(SYM, since=int(t), limit=100)
            if not b:
                break
            rows += b
            nt = b[-1]["timestamp"] + 1
            if nt <= t:
                break
            t = nt
        ts = sorted(r["timestamp"] for r in rows)
        if ts:
            days = (ts[-1] - ts[0]) / DAY_MS
            lines.append(f"{name}: {len(ts)} records, {days:.0f} days of history "
                         f"({ex.iso8601(ts[0])[:10]} -> {ex.iso8601(ts[-1])[:10]})")
        else:
            lines.append(f"{name}: no data returned")
    except Exception as e:
        lines.append(f"{name}: FAILED {type(e).__name__}: {str(e)[:110]}")
text = "\n".join(lines)
print(text)
os.makedirs("data", exist_ok=True)
with open("data/funding_probe.txt", "w") as f:
    f.write(text + "\n")
