"""Distance-to-strike predictor.

Scans active hourly crypto strike markets (bitcoin/ethereum-above/below-N).
At each scan: compute spot distance from strike in ATR units using 15m
candles. Sells YES on dead strikes (bid < 1 - FEE). Paper-trades:
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

THRESH = 1.0     # ATR distance beyond which strike is "dead" -> sell YES
T_MIN = 20       # scan window: markets closing in 20-40 min
STAKE = 10.0     # USD collateral per SELL_YES position (paper)
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


def active_5m_markets():
    now = time.time()
    min_iso = datetime.datetime.fromtimestamp(now - 300, datetime.UTC).isoformat().replace("+00:00", "Z")
    max_iso = datetime.datetime.fromtimestamp(now + 1800, datetime.UTC).isoformat().replace("+00:00", "Z")
    try:
        r = requests.get(f"{GAMMA}/events", params={
            "active": "true", "closed": "false", "limit": 20,
            "end_date_min": min_iso, "end_date_max": max_iso,
            "order": "endDate", "ascending": "true"},
            headers=UA, timeout=15)
        evs = [e for e in r.json() if isinstance(e, dict)]
        out = []
        for e in evs:
            eslug = e.get("slug", "") or ""
            if "btc-updown-5m" not in eslug:
                continue
            mkts = e.get("markets") or []
            if not mkts:
                continue
            m = mkts[0]
            try:
                end = datetime.datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            clob_tokens = json.loads(m.get("clobTokenIds") or "[]")
            if len(clob_tokens) >= 2:
                out.append({
                    "slug": m.get("slug") or eslug,
                    "event": eslug,
                    "title": e.get("title") or m.get("question"),
                    "end": end,
                    "up_token": clob_tokens[0],
                    "down_token": clob_tokens[1]
                })
        return out
    except Exception as e:
        print(f"5m markets fetch failed: {e}")
        return []


def btc_spot_price():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                         headers=UA, timeout=5)
        return float(r.json()["bitcoin"]["usd"])
    except Exception:
        pass
    try:
        r = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        return float(r.json()["price"])
    except Exception:
        return None


def clob_best_ask(token_id):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, headers=UA, timeout=5)
        b = r.json()
        asks = b.get("asks") or []
        if asks:
            return min(float(a["price"]) for a in asks)
    except Exception:
        pass
    return None


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


def scan_5m(positions):
    mkts = active_5m_markets()
    if not mkts:
        return
    spot = btc_spot_price()
    if not spot:
        return
    now = time.time()
    existing = {p.get("slug") for p in positions if not p.get("resolved")}
    for m in mkts:
        if m["slug"] in existing:
            continue
        time_left = m["end"] - now
        if not (45 <= time_left <= 270):
            continue
        up_ask = clob_best_ask(m["up_token"])
        down_ask = clob_best_ask(m["down_token"])
        if up_ask and 0.25 <= up_ask <= 0.60:
            shares = round(STAKE / up_ask, 2)
            positions.append({
                "slug": m["slug"], "event": m["event"], "title": m["title"],
                "coin": "bitcoin", "timeframe": "5m", "side": "UP",
                "ts": int(now), "end": int(m["end"]), "spot": spot,
                "entry": round(up_ask, 3), "stake": STAKE,
                "shares": shares, "resolved": False
            })
            print(f"5M+ {m['slug']} UP @ {up_ask:.3f} spot={spot:.1f}")
        elif down_ask and 0.25 <= down_ask <= 0.60:
            shares = round(STAKE / down_ask, 2)
            positions.append({
                "slug": m["slug"], "event": m["event"], "title": m["title"],
                "coin": "bitcoin", "timeframe": "5m", "side": "DOWN",
                "ts": int(now), "end": int(m["end"]), "spot": spot,
                "entry": round(down_ask, 3), "stake": STAKE,
                "shares": shares, "resolved": False
            })
            print(f"5M+ {m['slug']} DOWN @ {down_ask:.3f} spot={spot:.1f}")


def resolve_5m(positions):
    now = time.time()
    for p in positions:
        if p.get("resolved") or p.get("timeframe") != "5m":
            continue
        if now < p.get("end", 0) + 15:
            continue
        try:
            r = requests.get(f"{GAMMA}/events", params={"slug": p.get("event")}, headers=UA, timeout=10)
            evs = r.json()
            if not isinstance(evs, list) or not evs:
                continue
            mkts = evs[0].get("markets") or []
            if not mkts:
                continue
            prices = json.loads(mkts[0].get("outcomePrices") or "[]")
            if len(prices) >= 2:
                up_px = float(prices[0])
                down_px = float(prices[1])
                won = None
                if up_px >= 0.95:
                    won = (p["side"] == "UP")
                elif down_px >= 0.95:
                    won = (p["side"] == "DOWN")
                if won is not None:
                    p["resolved"] = True
                    p["won"] = won
                    if won:
                        p["pnl"] = round(p["shares"] * (1.0 - p["entry"]) - 0.02 * p["stake"], 2)
                    else:
                        p["pnl"] = -p["stake"]
                    print(f"resolved 5m {p['slug']} {p['side']} won={won} pnl={p['pnl']:+.2f}")
        except Exception:
            continue


def scan():
    positions = load(POS)
    scan_5m(positions)
    try:
        mkts = active_strike_markets()
    except requests.RequestException as e:
        print(f"scan aborted: market fetch failed: {e}")
        save(POS, positions)
        return
    if not mkts:
        print("no strike markets in window")
        save(POS, positions)
        return
    for m in mkts:
        if m["slug"] in {p["slug"] for p in positions if not p.get("resolved")}:
            continue
        d = atr_distance(m)
        if not d:
            continue
        spot, atr, dist = d
        if dist > THRESH:
            clob_id = (m.get("clob") or [""])[0]
            bid = yes_bid(clob_id) if clob_id else None
            if bid and bid < 1.0 - FEE:
                positions.append({
                    "slug": m["slug"], "ts": int(time.time()), "end": m["end"],
                    "coin": m["coin"], "dir": m["dir"], "strike": m["strike"],
                    "spot": spot, "atr": round(atr, 2), "dist": round(dist, 2),
                    "side": "SELL_YES", "entry": round(bid, 3),
                    "stake": STAKE,
                    "shares": STAKE,
                    "resolved": False})
                print(f"SY+ {m['slug'][:55]} dist={dist:+.1f} bid={bid:.3f}")
        elif dist < -THRESH:
            print(f"YES-fav {m['slug'][:55]} dist={dist:+.1f} (no action)")
    save(POS, positions)


def resolve():
    positions = load(POS)
    resolve_5m(positions)
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
        # YES wins/loses decides the SELL_YES settlement
        p["yes_won"] = final > 0.99
        # sold YES at `entry`: keep entry/share if YES loses,
        # pay (1 - entry) per share if YES wins
        p["payout"] = p["shares"] * p["entry"] if not p["yes_won"] else 0.0
        p["liability"] = p["shares"] * (1.0 - p["entry"]) \
            if p["yes_won"] else 0.0
        p["pnl"] = p["payout"] - p["liability"]
        p["resolved"] = True
        print(f"resolved {p['slug'][:50]} side=SELL_YES "
              f"yes={p['yes_won']} pnl={p['pnl']:+.2f}")
    save(POS, positions)


def report():
    positions = load(POS)
    done = [p for p in positions
            if p.get("resolved") and p.get("side") == "SELL_YES"]
    open_ = [p for p in positions
             if not p.get("resolved") and p.get("side") == "SELL_YES"]
    wins = sum(1 for p in done if p["pnl"] > 0)
    pnl = sum(p["pnl"] for p in done)
    print(f"== SELL_YES == open: {len(open_)} | resolved: {len(done)} "
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
