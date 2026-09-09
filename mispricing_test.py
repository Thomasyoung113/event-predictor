"""Test: were 'dead strike' hourly crypto markets mispriced at T-30min?"""
import json, re, time, datetime, requests, os
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0"}
HERE = os.path.expanduser("~/event-predictor")
os.chdir(HERE)
S = requests.Session()

total = [m for m in json.load(open("archive.json")) if isinstance(m, dict)]

rows = []
for m in total:
    s = m.get("slug", "") or ""
    mm = re.match(r"(bitcoin|ethereum)-above-([\d,.]+)-on-(.+?)-(\d+)(am|pm)-et", s)
    if not mm or not m.get("endDate"):
        continue
    try:
        final = float(json.loads(m["outcomePrices"])[0])
        toks = json.loads(m.get("clobTokenIds") or "[]")
    except Exception:
        continue
    if not toks:
        continue
    rows.append({
        "coin": mm.group(1), "strike": float(mm.group(2).replace(",", "")),
        "yes_won": final == 1.0,
        "end": datetime.datetime.fromisoformat(
            m["endDate"].replace("Z", "+00:00")).timestamp(),
        "token": toks[0], "slug": s})

ded = {(r["coin"], r["strike"], r["end"]): r for r in rows}
rows = sorted(ded.values(), key=lambda x: x["end"])
print(f"unique markets: {len(rows)}")

# --- fetch implied YES price at T-30min ---
implied = {}
if os.path.exists("implied_prices.json"):
    implied = json.load(open("implied_prices.json"))
fails = defaultdict(int)

for i, r in enumerate(rows):
    key = r["slug"]
    if key in implied:
        continue
    try:
        rr = S.get("https://clob.polymarket.com/prices-history",
                   params={"market": r["token"], "interval": "6h", "fidelity": 10},
                   headers=UA, timeout=30)
        if rr.status_code != 200:
            fails[f"http_{rr.status_code}"] += 1
            implied[key] = None
        else:
            hist = rr.json().get("history", [])
            cutoff = r["end"] - 1800
            prior = [h["p"] for h in hist if h["t"] <= cutoff]
            implied[key] = prior[-1] if prior else None
            if not prior:
                fails["no_prior_point"] += 1
    except Exception as e:
        fails[type(e).__name__] += 1
        implied[key] = None
    if (i + 1) % 25 == 0:
        json.dump(implied, open("implied_prices.json", "w"))
        print(f"{i+1}/{len(rows)} fetched, fails={dict(fails)}")
    time.sleep(0.2)
json.dump(implied, open("implied_prices.json", "w"))
usable = sum(1 for r in rows if implied.get(r["slug"]) is not None)
print(f"usable implied prices: {usable}/{len(rows)}, fails={dict(fails)}")

# --- ATR from Binance ---
symmap = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT"}
kcache = {}

def candles(sym, start, end):
    try:
        rr = S.get("https://api.binance.com/api/v3/klines", params={
            "symbol": sym, "interval": "15m", "startTime": int(start * 1000),
            "endTime": int(end * 1000), "limit": 25}, headers=UA, timeout=20)
        return rr.json() if rr.status_code == 200 else []
    except Exception:
        return []

buck = defaultdict(lambda: [0, 0.0, 0])  # n, sum_implied, yes_wins
tested = 0
for r in rows:
    p = implied.get(r["slug"])
    if p is None:
        continue
    sym = symmap[r["coin"]]
    wstart = (int(r["end"]) - 5400) // 900 * 900
    ck = (sym, wstart)
    if ck not in kcache:
        kcache[ck] = candles(sym, wstart, int(r["end"]) - 60)
        time.sleep(0.25)
    kl = [k for k in kcache[ck] if int(k[0]) // 1000 <= r["end"] - 600]
    if len(kl) < 6:
        continue
    closes = [float(k[4]) for k in kl]
    atr = max(float(k[2]) - float(k[3]) for k in kl)
    if atr <= 0:
        continue
    dist = (closes[-1] - r["strike"]) / atr
    b = ("far_below" if dist < -2 else "below" if dist < -0.5 else
         "near" if dist < 0.5 else "above" if dist < 2 else "far_above")
    buck[b][0] += 1
    buck[b][1] += p
    buck[b][2] += 1 if r["yes_won"] else 0
    tested += 1

print(f"\nP-MISPRICING (n={tested})")
print(f"{'bucket':>10} {'n':>5} {'implied':>8} {'hit_rate':>9} {'edge':>8} {'EV@2%':>8}")
for b, (n, sp, w) in sorted(buck.items()):
    if n < 10:
        continue
    imp, hit = sp / n, w / n
    # strategy: SELL YES at implied price -> keep (1-imp) per unit, lose hit
    # fraction of the time (payout 1). EV = (1-imp) - hit - fees.
    ev = (1 - imp) - hit - 0.02
    print(f"{b:>10} {n:>5} {imp:>8.4f} {hit:>9.4f} {imp-hit:>+8.4f} {ev:>+8.4f}")

json.dump({b: [n, sp, w] for b, (n, sp, w) in buck.items()},
          open("mispricing_result.json", "w"))
