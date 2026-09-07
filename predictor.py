"""Distance-to-strike predictor.

Scans active hourly crypto strike markets (bitcoin/ethereum-above/below-N).
At each scan: compute spot distance from strike in ATR units using 15m
candles. Signals NO on strikes > threshold ATR from spot. Paper-trades:
logs entries, resolves at market close, tracks P&L.

Usage:
  python3 predictor.py scan        # one scan cycle (cron-friendly)
  python3 predictor.py resolve     # resolve matured paper trades
  python3 predictor.py report      # P&L report
"""
import sys, json, os, time, re, datetime
import requests

D = os.path.dirname(os.path.abspath(__file__))
POS = os.path.join(D, "positions.json")
GAMMA = "https://gamma-api.polymarket.com"
KRAKEN = "https://api.kraken.com/0/public"
UA = {"User-Agent": "Mozilla/5.0"}

THRESH = 1.0     # ATR distance beyond which strike is "dead" -> NO
T_MIN = 20       # scan window: markets closing in 20-40 min
STAKE = 10.0     # USD per NO share buy (paper)
FEE = 0.02       # est. cost/slippage per share (2c)

SLUG = re.compile(r"(bitcoin|ethereum|solana|xrp)-(above|below)-([\d.]+)-on-")
SYM = {"bitcoin": "XBTUSD", "ethereum": "ETHUSD", "solana": "SOLUSD",
       "xrp": "XRPUSD"}


def active_strike_markets():
    """Live strike markets from hourly events (events endpoint, end_date_min
    filters stale junk). Window: closing in 10-60min."""
    now = time.time()
    out = []
    for off in (0, 50, 100, 150):
        r = requests.get(f"{GAMMA}/events", params={
            "active": "true", "closed": "false", "limit": 50, "offset": off,
            "end_date_min": datetime.datetime.fromtimestamp(
                now, datetime.UTC).isoformat().replace("+00:00", "Z"),
            "order": "endDate", "ascending": "true"},
            headers=UA, timeout=30)
        evs = [e for e in r.json() if isinstance(e, dict)]
        if not evs:
            break
        for e in evs:
            slug = e.get("slug", "") or ""
            if not re.search(r"-(above|below)-on-", slug):
                continue
            try:
                end = datetime.datetime.fromisoformat(
                    e["endDate"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            mins = (end - now) / 60
            if not (10 <= mins <= 60):
                continue
            coin = slug.split("-")[0]  # bitcoin / ethereum
            for m in e.get("markets", []):
                ms = m.get("slug", "") or ""
                mm = re.match(rf"{coin}-(above|below)-([\d.]+)-on-", ms)
                if not mm:
                    continue
                out.append({"slug": ms, "event": slug, "coin": coin,
                            "dir": mm.group(1), "strike": float(mm.group(2)),
                            "end": end,
                            "clob": json.loads(m.get("clobTokenIds") or "[]")})
        time.sleep(0.2)
    return out


def atr_distance(m):
    """(spot, atr, dist) — dist = (spot-strike)/ATR, signed by direction.

    Kraken OHLC: returns per-pair {key: [[time, open, high, low, close, vwap,
    volume, count], ...]} — last 720 candles for the interval.
    """
    sym = SYM[m["coin"]]
    kl = None
    for attempt in range(3):
        try:
            r = requests.get(f"{KRAKEN}/OHLC", params={
                "pair": sym, "interval": 15}, headers=UA, timeout=20)
            d = r.json()
            if d.get("error"):
                kl = None
            else:
                res = d["result"]
                key = next(k for k in res if k != "last")
                kl = res[key]
            break
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1)
    if not kl or len(kl) < 6:
        return None
    end = int(m["end"])
    # candles strictly before T-10min of market end
    kl = [k for k in kl if int(k[0]) <= end - 600]
    if len(kl) < 6:
        return None
    closes = [float(k[4]) for k in kl[-6:]]
    highs = [float(k[2]) for k in kl[-6:]]
    lows = [float(k[3]) for k in kl[-6:]]
    atr = max(h - l for h, l in zip(highs, lows))
    if atr <= 0:
        return None
    spot = closes[-1]
    # signed reach distance: how far spot must travel (in ATR) for YES to win
    if m["dir"] == "above":
        dist = (m["strike"] - spot) / atr   # strike above spot -> YES must climb
    else:  # below
        dist = (spot - m["strike"]) / atr   # strike below spot -> YES must fall
    return spot, atr, dist


def scan():
    mkts = active_strike_markets()
    if not mkts:
        print("no strike markets in window")
        return
    positions = load(POS)
    for m in mkts:
        if m["slug"] in {p["slug"] for p in positions if not p.get("resolved")}:
            continue
        d = atr_distance(m)
        if not d:
            continue
        spot, atr, dist = d
        if dist > THRESH:
            # strike far from spot -> market effectively dead -> buy NO
            # NO price ~ 1 - YES ask; approximate entry at 0.98 conservative
            entry = 0.98
            positions.append({
                "slug": m["slug"], "ts": int(time.time()), "end": m["end"],
                "coin": m["coin"], "dir": m["dir"], "strike": m["strike"],
                "spot": spot, "atr": round(atr, 2), "dist": round(dist, 2),
                "side": "NO", "entry": entry, "stake": STAKE,
                "shares": STAKE / entry, "resolved": False})
            print(f"NO  {m['slug'][:55]} dist={dist:+.1f} ATR spot={spot}")
        elif dist < -THRESH:
            print(f"YES-fav {m['slug'][:55]} dist={dist:+.1f} (no action)")
    save(POS, positions)


def resolve():
    positions = load(POS)
    for p in positions:
        if p["resolved"]:
            continue
        if time.time() < p["end"] + 300:
            continue
        r = requests.get(f"{GAMMA}/markets", params={"slug": p["slug"]},
                         headers=UA, timeout=20)
        mk = r.json()
        if not mk:
            continue
        try:
            final = float(json.loads(mk[0]["outcomePrices"])[0])
        except (KeyError, ValueError, IndexError):
            continue
        # NO wins if YES lost
        p["yes_won"] = final == 1.0
        p["payout"] = p["shares"] * 1.0 if not p["yes_won"] else 0.0
        p["pnl"] = p["payout"] - p["stake"]
        p["resolved"] = True
        print(f"resolved {p['slug'][:50]} yes={p['yes_won']} pnl={p['pnl']:+.2f}")
    save(POS, positions)


def report():
    positions = load(POS)
    done = [p for p in positions if p.get("resolved")]
    open_ = [p for p in positions if not p.get("resolved")]
    wins = sum(1 for p in done if p["pnl"] > 0)
    pnl = sum(p["pnl"] for p in done)
    print(f"open: {len(open_)} | resolved: {len(done)} | wins: {wins}")
    if done:
        print(f"P&L: ${pnl:+.2f} on ${sum(p['stake'] for p in done):.0f} staked "
              f"({wins}/{len(done)} = {wins/len(done):.0%} win rate)")
    for p in open_:
        print(f"  open {p['slug'][:50]} dist={p['dist']} entry={p['entry']}")


def load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"scan": scan, "resolve": resolve, "report": report}.get(cmd, scan)()
