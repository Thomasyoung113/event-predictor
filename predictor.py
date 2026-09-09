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
CLOB = "https://clob.polymarket.com"
KRAKEN = "https://api.kraken.com/0/public"
UA = {"User-Agent": "Mozilla/5.0"}

THRESH = 1.0     # ATR distance beyond which strike is "dead" -> NO
T_MIN = 20       # scan window: markets closing in 20-40 min
STAKE = 10.0     # USD per NO share buy (paper)
FEE = 0.02       # est. cost/slippage per share (2c)

SLUG = re.compile(r"(bitcoin|ethereum|solana|xrp)-(above|below)-([\d.]+)-on-")


def yes_bid(clob_id):
    """Best YES bid from the CLOB book — what selling YES would actually get.
    Empty-bid books are common on dead strikes; fall back to 1 - best ask."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": clob_id},
                         headers=UA, timeout=15)
        book = r.json()
        bids = book.get("bids") or []
        if bids:
            return max(float(b["price"]) for b in bids)
        asks = book.get("asks") or []
        if asks:
            return 1.0 - min(float(a["price"]) for a in asks)
        return None
    except (requests.RequestException, ValueError, KeyError):
        return None
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
    try:
        mkts = active_strike_markets()
    except requests.RequestException as e:
        print(f"scan aborted: market fetch failed: {e}")
        return
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
            # --- parallel paper line: SELL YES on the same dead strike ---
            # profit = bid received per share; loss = (1 - bid) if YES wins
            clob_id = (m.get("clob") or [""])[0]
            bid = yes_bid(clob_id) if clob_id else None
            if bid and bid < 1.0 - FEE:
                positions.append({
                    "slug": m["slug"], "ts": int(time.time()), "end": m["end"],
                    "coin": m["coin"], "dir": m["dir"], "strike": m["strike"],
                    "spot": spot, "atr": round(atr, 2), "dist": round(dist, 2),
                    "side": "SELL_YES", "entry": round(bid, 3),
                    "stake": STAKE,  # collateral notionally at risk
                    "shares": STAKE,  # 1:1: selling $10 worth of YES shares
                    "resolved": False})
                print(f"SY+ {m['slug'][:55]} dist={dist:+.1f} bid={bid:.3f}")
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
        try:
            r = requests.get(f"{GAMMA}/markets",
                             params={"slug": p["slug"], "closed": "true"},
                             headers=UA, timeout=20)
            r.raise_for_status()
            mk = r.json()
            if not isinstance(mk, list):
                continue
        except (requests.RequestException, ValueError):
            continue  # keep unresolved; retried next cycle
        try:
            final = float(json.loads(mk[0]["outcomePrices"])[0])
        except (KeyError, ValueError, IndexError):
            continue
        # Only settle on definitive outcomes. closed-but-UMA-pending markets
        # report ["0.5","0.5"] — settling those would guess, not resolve.
        status = (mk[0].get("umaResolutionStatus") or "").lower()
        if status and status != "resolved":
            continue
        if abs(final - 1.0) > 0.01 and abs(final - 0.0) > 0.01:
            continue  # ambiguous price — retry next cycle
        # NO wins if YES lost
        p["yes_won"] = final > 0.99
        if p.get("side") == "SELL_YES":
            # sold YES at `entry`: keep entry/share if YES loses,
            # pay (1 - entry) per share if YES wins
            p["payout"] = p["shares"] * p["entry"] if not p["yes_won"] \
                else 0.0
            p["liability"] = p["shares"] * (1.0 - p["entry"]) \
                if p["yes_won"] else 0.0
            p["pnl"] = p["payout"] - p["liability"]
        else:
            p["payout"] = p["shares"] * (1.0 - FEE) if not p["yes_won"] else 0.0
            p["pnl"] = p["payout"] - p["stake"]
        p["resolved"] = True
        print(f"resolved {p['slug'][:50]} side={p.get('side', 'NO')} "
              f"yes={p['yes_won']} pnl={p['pnl']:+.2f}")
    save(POS, positions)


def report():
    positions = load(POS)
    for side in ("NO", "SELL_YES"):
        done = [p for p in positions
                if p.get("resolved") and p.get("side", "NO") == side]
        open_ = [p for p in positions
                 if not p.get("resolved") and p.get("side", "NO") == side]
        wins = sum(1 for p in done if p["pnl"] > 0)
        pnl = sum(p["pnl"] for p in done)
        print(f"== {side} == open: {len(open_)} | resolved: {len(done)} "
              f"| wins: {wins}")
        if done:
            staked = sum(p["stake"] for p in done)
            print(f"P&L: ${pnl:+.2f} on ${staked:.0f} "
                  f"({wins}/{len(done)} = {wins/len(done):.0%})")
        for p in open_:
            print(f"  open {p['slug'][:50]} dist={p['dist']} "
                  f"entry={p['entry']}")


def load(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (ValueError, OSError):
            pass  # corrupt file -> fall through to backup/restart
        bak = path + ".corrupt"
        try:
            os.replace(path, bak)
            print(f"WARNING: corrupt {path}, moved to {bak}, starting fresh")
        except OSError:
            pass
    return []


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic: never leaves a half-written file


if __name__ == "__main__":
    cmds = {"scan": scan, "resolve": resolve, "report": report}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd not in cmds:
        sys.exit(f"unknown command: {cmd!r} (use: scan|resolve|report)")
    cmds[cmd]()
