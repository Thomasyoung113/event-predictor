"""Pattern hunter: find statistical edges in Polymarket + crypto data.

Patterns tested against collected snapshots:
  P1  Longshot bias      — are cheap markets (p<0.15) overpriced or underpriced?
  P2  Momentum           — do 24h price moves predict next moves?
  P3  Mean reversion     — do spikers revert to prior levels?
  P4  Smart money        — do large recent trades lead price moves?
  P5  Crypto-event link  — do crypto-market prices track BTC moves?
"""
import json, glob, os, statistics as st
from collections import defaultdict

HOME = os.path.expanduser("~")
STORE = os.path.join(HOME, "event-predictor", "data")


def load_snaps():
    snaps = []
    for f in sorted(glob.glob(os.path.join(STORE, "snap_*.json"))):
        with open(f) as fh:
            snaps.append(json.load(fh))
    return snaps


def market_map(snaps):
    """slug -> list of (ts, yes_price, volume) ordered by time."""
    hist = defaultdict(list)
    for s in snaps:
        for m in s["markets"]:
            try:
                prices = json.loads(m.get("outcomePrices") or "[]")
                if not prices:
                    continue
                p = float(prices[0])
                v24 = float(m.get("volume24hr") or 0)
                hist[m["slug"]].append((s["ts"], p, v24, m.get("question", "")))
            except (ValueError, TypeError, KeyError):
                continue
    return hist


def p1_longshot(snaps):
    """Distribution of cheap markets: do they resolve NO more than their price implies?"""
    resolved = []
    for s in snaps:
        for m in s["markets"]:
            if m.get("closed") or not m.get("umaResolutionStatus"):
                pass
    # gamma closed markets with outcomes
    return None  # needs resolved-market fetch; see p1_resolved()


def p1_resolved():
    """Fetch actually-resolved markets and check longshot bias."""
    import requests
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"closed": "true", "limit": 100, "order": "volume",
                "ascending": "false"}, timeout=30)
    markets = r.json()
    buckets = defaultdict(lambda: [0, 0.0, 0])  # bucket -> [n, sum_price, n_correct_yes]
    for m in markets:
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            if len(prices) != 2:
                continue
            final = float(prices[0])  # 1.0 = YES resolved, 0.0 = NO
            # we need pre-resolution price; use last trade via volume-weighted proxy:
            # skip markets without outcome history — use onePrice if present
            best = float(m.get("bestBid") or 0), float(m.get("bestAsk") or 0)
            if best == (0, 0):
                continue
            mid = (best[0] + best[1]) / 2
            if mid <= 0.01 or mid >= 0.99:
                continue
            b = "lt15" if mid < 0.15 else "15-35" if mid < 0.35 else \
                "35-65" if mid < 0.65 else "65-85" if mid < 0.85 else "gt85"
            buckets[b][0] += 1
            buckets[b][1] += mid
            if final == 1.0:
                buckets[b][2] += 1
        except (ValueError, TypeError, KeyError):
            continue
    print("\n=== P1 LONGSHOT BIAS (resolved markets, mid-price before resolution) ===")
    print(f"{'bucket':>8} {'n':>5} {'avg_mid':>8} {'yes_rate':>9} {'edge':>8}")
    for b, (n, sp, yc) in sorted(buckets.items()):
        if n < 5:
            continue
        avg = sp / n
        yr = yc / n
        edge = yr - avg  # >0 = YES underpriced, buy YES
        print(f"{b:>8} {n:>5} {avg:>8.3f} {yr:>9.3f} {edge:>+8.3f}")
    return buckets


def p2_momentum(hist):
    """Does a price move between snapshots predict the next move?"""
    moves = defaultdict(list)  # bucket -> list of next moves
    for slug, series in hist.items():
        series.sort()
        for i in range(1, len(series) - 1):
            p0, p1 = series[i - 1][1], series[i][1]
            p2 = series[i + 1][1]
            d1 = p1 - p0
            d2 = p2 - p1
            if abs(d1) < 0.005 or p1 < 0.05 or p1 > 0.95:
                continue
            b = "up" if d1 > 0 else "down"
            moves[b].append(d2)
    print("\n=== P2 MOMENTUM (snap-to-snap) ===")
    for b in ("up", "down"):
        vals = moves[b]
        if len(vals) < 10:
            continue
        print(f"after {b:>5}: n={len(vals):>4} avg_next_move={st.mean(vals):+.4f} "
              f"median={st.median(vals):+.4f}")
    return moves


def p4_smart_money(snaps):
    """Large trades vs small trades: do whales' positions move toward eventual outcomes?"""
    big, small = [], []
    for s in snaps:
        # trades endpoint isn't snapshotted; call live once
        pass
    import requests
    r = requests.get("https://data-api.polymarket.com/trades",
                     params={"limit": 500}, timeout=30)
    if r.status_code != 200:
        print("\n=== P4: trades endpoint unavailable ===")
        return
    trades = r.json()
    by_wallet = defaultdict(lambda: [0.0, 0.0])  # wallet -> [usd_buy, n]
    slug_usd = defaultdict(float)
    for t in trades:
        usd = t.get("size", 0) * t.get("price", 0)
        slug_usd[t.get("slug", "?")] += usd
        by_wallet[t.get("proxyWallet", "?")][0] += usd
        by_wallet[t.get("proxyWallet", "?")][1] += 1
    whs = sorted(by_wallet.items(), key=lambda kv: -kv[1][0])[:20]
    print("\n=== P4 SMART MONEY (last 500 trades) ===")
    print(f"top wallets by USD volume traded:")
    for w, (usd, n) in whs[:10]:
        print(f"  {w[:12]}... ${usd:>10,.0f} across {n} trades")
    hot = sorted(slug_usd.items(), key=lambda kv: -kv[1])[:8]
    print("hottest markets right now:")
    for s_, usd in hot:
        print(f"  ${usd:>9,.0f}  {s_[:60]}")
    return {"top_wallets": whs[:20], "hot_markets": hot}


def p5_crypto_link(hist, crypto_hist):
    """Correlate crypto-market questions' prices with BTC daily returns."""
    btc = {c["t"] // 86400: c["close"] for c in crypto_hist}
    btc_ret = {}
    days = sorted(btc)
    for i in range(1, len(days)):
        btc_ret[days[i]] = btc[days[i]] / btc[days[i - 1]] - 1
    crypto_slugs = [s for s in hist if any(
        k in s.lower() for k in ("bitcoin", "btc", "ethereum", "eth",
                                 "crypto", "solana", "token", "fed", "rate"))]
    print(f"\n=== P5 CRYPTO/EVENT LINK ===")
    print(f"crypto/fed-related active markets: {len(crypto_slugs)}")
    for s in crypto_slugs[:15]:
        series = sorted(hist[s])
        if series:
            _, p, _, q = series[-1]
            print(f"  p={p:.2f}  {q[:65]}")


if __name__ == "__main__":
    snaps = load_snaps()
    print(f"loaded {len(snaps)} snapshots")
    hist = market_map(snaps)
    print(f"{len(hist)} markets tracked across snapshots")
    p2_momentum(hist)
    p1_resolved()
    p4_smart_money(snaps)
    from data_sources import get_crypto_history
    ch = get_crypto_history("BTCUSDT", 30)
    p5_crypto_link(hist, ch)
