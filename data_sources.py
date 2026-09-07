"""Data collectors: Polymarket + crypto prices + news headlines."""
import time, json, os, re, html
import requests

HOME = os.path.expanduser("~")
STORE = os.path.join(HOME, "event-predictor", "data")
os.makedirs(STORE, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATAAPI = "https://data-api.polymarket.com"
BINANCE = "https://api.binance.com"


def get_markets(limit=200):
    """Active Polymarket markets with volume, prices, history tokens."""
    out, offset = [], 0
    while len(out) < limit:
        r = requests.get(f"{GAMMA}/markets", params={
            "active": "true", "closed": "false", "limit": 100,
            "offset": offset, "order": "volume24hr", "ascending": "false"},
            headers=UA, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        offset += 100
        time.sleep(0.4)
    return out[:limit]


def get_price_history(token_id, interval="1w", fidelity=60):
    """Historical YES-price series for one CLOB token."""
    r = requests.get(f"{CLOB}/prices-history", params={
        "market": token_id, "interval": interval, "fidelity": fidelity},
        headers=UA, timeout=30)
    if r.status_code != 200:
        return []
    return r.json().get("history", [])


def get_recent_trades(limit=500):
    """Live trades feed — smart money signal source."""
    r = requests.get(f"{DATAAPI}/trades", params={"limit": limit},
                     headers=UA, timeout=30)
    if r.status_code != 200:
        return []
    return r.json()


def get_crypto_prices(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")):
    out = {}
    for s in symbols:
        try:
            r = requests.get(f"{BINANCE}/api/v3/ticker/24hr",
                             params={"symbol": s}, headers=UA, timeout=15)
            d = r.json()
            out[s] = {"price": float(d["lastPrice"]),
                      "chg_pct": float(d["priceChangePercent"]),
                      "volume": float(d["quoteVolume"])}
        except Exception as e:
            out[s] = {"error": str(e)}
        time.sleep(0.2)
    return out


def get_crypto_history(symbol="BTCUSDT", days=30):
    """Daily klines for correlation analysis."""
    r = requests.get(f"{BINANCE}/api/v3/klines", params={
        "symbol": symbol, "interval": "1d", "limit": days},
        headers=UA, timeout=15)
    out = []
    for k in r.json():
        out.append({"t": k[0] // 1000, "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "vol": float(k[5])})
    return out


def get_news_rss():
    """Headlines from feeds that work without keys."""
    feeds = [
        ("cointelegraph", "https://cointelegraph.com/rss"),
        ("wsj_markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
        ("wsj_world", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ]
    items = []
    for name, url in feeds:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            # minimal XML parse: <item>...<title>..</title>...<pubDate>..</pubDate>
            for m in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:25]:
                t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", m, re.S)
                d = re.search(r"<pubDate>(.*?)</pubDate>", m, re.S)
                if t:
                    items.append({"feed": name,
                                  "title": html.unescape(t.group(1)).strip(),
                                  "date": d.group(1) if d else ""})
        except Exception:
            pass
        time.sleep(0.3)
    return items


def snapshot():
    """One full collection cycle, saved with timestamp."""
    ts = int(time.time())
    snap = {"ts": ts, "markets": get_markets(200),
            "crypto": get_crypto_prices(), "news": get_news_rss()}
    path = os.path.join(STORE, f"snap_{ts}.json")
    with open(path, "w") as f:
        json.dump(snap, f)
    # keep last 40
    snaps = sorted(f for f in os.listdir(STORE) if f.startswith("snap_"))
    for old in snaps[:-40]:
        os.remove(os.path.join(STORE, old))
    return snap, path


if __name__ == "__main__":
    s, p = snapshot()
    print(f"saved {p}: {len(s['markets'])} markets, "
          f"{len(s['news'])} headlines, crypto={list(s['crypto'])}")
