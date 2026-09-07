"""Test patterns against RESOLVED markets (ground truth).

P1: longshot bias — cheap YES overpriced?
P6: 5-min BTC updown — does BTC's recent candle direction predict resolution?
    Compares implied prob (pre-resolution price) vs actual hit rate,
    conditioned on BTC momentum. Edge = hit rate - implied prob.
"""
import json, time, statistics as st
from collections import defaultdict
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com"
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13)"}


def fetch_closed(tag=None, limit=200):
    """Recently resolved markets. tag filters e.g. crypto."""
    out, offset = [], 0
    while len(out) < limit:
        params = {"closed": "true", "limit": 100, "offset": offset,
                  "order": "endDate", "ascending": "false"}
        r = requests.get(f"{GAMMA}/markets", params=params, headers=UA, timeout=30)
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        offset += 100
        time.sleep(0.3)
    return out[:limit]


def price_before_resolution(m, minutes_before=30, fidelity=10):
    """YES price N minutes before market end, from CLOB history."""
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if not tokens:
            return None
        end = m.get("endDate")
        if not end:
            return None
        # history covering last few hours
        r = requests.get(f"{CLOB}/prices-history", params={
            "market": tokens[0], "interval": "6h", "fidelity": fidelity},
            headers=UA, timeout=30)
        if r.status_code != 200:
            return None
        hist = r.json().get("history", [])
        if not hist:
            return None
        import datetime
        end_ts = int(datetime.datetime.fromisoformat(
            end.replace("Z", "+00:00")).timestamp())
        cutoff = end_ts - minutes_before * 60
        prior = [h["p"] for h in hist if h["t"] <= cutoff]
        return prior[-1] if prior else None
    except Exception:
        return None


def p1_longshot(markets):
    buckets = defaultdict(lambda: [0, 0.0, 0])
    for m in markets:
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            if len(prices) != 2:
                continue
            final = float(prices[0])  # 1 = YES won
            p = price_before_resolution(m, 30)
            if p is None or p <= 0.02 or p >= 0.98:
                continue
            b = "lt15" if p < 0.15 else "15-35" if p < 0.35 else \
                "35-65" if p < 0.65 else "65-85" if p < 0.85 else "gt85"
            buckets[b][0] += 1
            buckets[b][1] += p
            buckets[b][2] += 1 if final == 1.0 else 0
        except Exception:
            continue
    print("=== P1 LONGSHOT BIAS (price 30min before resolution vs outcome) ===")
    print(f"{'bucket':>8} {'n':>4} {'implied':>8} {'hit_rate':>9} {'edge':>8}")
    rows = {}
    for b, (n, sp, yc) in sorted(buckets.items()):
        if n < 8:
            continue
        imp, hit = sp / n, yc / n
        rows[b] = {"n": n, "implied": imp, "hit": hit, "edge": hit - imp}
        print(f"{b:>8} {n:>4} {imp:>8.3f} {hit:>9.3f} {hit-imp:>+8.3f}")
    return rows


def p6_updown_btc(markets):
    """5-min BTC updown markets: does pre-market BTC candle direction match outcome?
    Market slug like btc-updown-5m-<ts>. Up resolves YES if BTC at end >= at start."""
    import re, datetime
    rows = defaultdict(lambda: [0, 0])  # signal -> [n, correct]
    btc_cache = {}
    tested = 0
    for m in markets:
        slug = m.get("slug", "")
        mt = re.match(r"btc-updown-5m-(\d+)", slug)
        if not mt:
            continue
        start_ts = int(mt.group(1))
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            if len(prices) != 2:
                continue
            up_won = float(prices[0]) == 1.0
        except Exception:
            continue
        # BTC 1m candles around the window
        try:
            if start_ts not in btc_cache:
                r = requests.get(f"{BINANCE}/api/v3/klines", params={
                    "symbol": "BTCUSDT", "interval": "1m",
                    "startTime": (start_ts - 600) * 1000,
                    "endTime": (start_ts + 600) * 1000, "limit": 25},
                    headers=UA, timeout=15)
                btc_cache[start_ts] = r.json()
            kl = btc_cache[start_ts]
            if len(kl) < 12:
                continue
            # trend before window: last 10 candles before start
            closes = [float(k[4]) for k in kl if int(k[0]) // 1000 < start_ts]
            if len(closes) < 5:
                continue
            trend = closes[-1] - closes[0]
            sig = "up" if trend > 0 else "down"
            tested += 1
            rows[sig][0] += 1
            if (sig == "up") == up_won:
                rows[sig][1] += 1
        except Exception:
            continue
        time.sleep(0.15)
    print(f"\n=== P6 BTC 5-MIN UPDOWN (n={tested} resolved markets) ===")
    print("signal = BTC 10-min trend before window; correct = trend matched outcome")
    for sig, (n, c) in sorted(rows.items()):
        if n < 10:
            continue
        print(f"  trend {sig:>4}: n={n:>4} matched={c/n:.3f}  "
              f"({'momentum' if c/n > 0.5 else 'reversal' if c/n < 0.45 else 'coin-flip'})")
    return dict(rows)


if __name__ == "__main__":
    print("fetching recently resolved markets...")
    mkts = fetch_closed(limit=200)
    print(f"{len(mkts)} resolved markets fetched")
    updown = [m for m in mkts if m.get("slug", "").startswith("btc-updown")]
    other = [m for m in mkts if not m.get("slug", "").startswith(("btc-updown", "eth-updown", "xrp-updown", "sol-updown"))]
    print(f"{len(updown)} 5-min updown, {len(other)} regular")
    p1_longshot(other)
    p6_updown_btc(updown)
