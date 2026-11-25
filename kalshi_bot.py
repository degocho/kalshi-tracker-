import json, math, os, time, urllib.request, urllib.parse, urllib.error
from collections import defaultdict
from datetime import datetime

API = "https://api.elections.kalshi.com/trade-api/v2"
HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
MIN_SCORE = 5        # post trades scoring this or higher
LOOKBACK = 30 * 60   # seconds of trades to scan each run

def get(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot"})
            return json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e:
            if e.code != 429: raise
            time.sleep(2 ** i)
    raise RuntimeError("rate limited")

def num(*v):
    for x in v:
        try: return float(x)
        except (TypeError, ValueError): pass
    return None

def ts(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def trade(t):
    yes = num(t.get("yes_price_dollars"))
    if yes is None: yes = (num(t.get("yes_price")) or 0) / 100
    side = (t.get("taker_side") or "").lower()
    price = yes if side == "yes" else 1 - yes
    count = num(t.get("count_fp"), t.get("count")) or 0
    return {"id": t.get("trade_id"), "tk": t.get("ticker", ""), "side": side, "yes": yes,
            "price": price, "count": count, "usd": count * price, "ts": ts(t.get("created_time")) or time.time()}

def fetch_trades():
    out, cur = [], None
    for _ in range(30):
        p = {"limit": 1000, "min_ts": int(time.time() - LOOKBACK)}
        if cur: p["cursor"] = cur
        d = get("/markets/trades", p)
        out += [trade(t) for t in d.get("trades", [])]
        cur = d.get("cursor")
        if not cur or not d.get("trades"): break
    return sorted(out, key=lambda t: t["ts"])

def fetch_markets(tickers):
    info = {}
    for tk in tickers:
        try:
            m = get("/markets/" + urllib.parse.quote(tk))["market"]
            info[tk] = {"title": m.get("title", ""), "sub": m.get("yes_sub_title", ""),
                        "event": m.get("event_ticker", ""), "close": ts(m.get("close_time")),
                        "vol": num(m.get("volume_24h_fp"), m.get("volume_24h")) or 0}
        except Exception:
            info[tk] = {}
    return info

def score(t, m, recent):
    s, why, usd, p = 0, [], t["usd"], t["price"]
    if usd >= 5000:
        s += min(4, 2 + math.log2(usd / 5000)); why.append(f"Whale bet ${usd:,.0f}")
    if p <= 0.20 and usd >= 1000:
        s += 3 if p <= 0.10 else 2; why.append(f"Longshot: big money at {p*100:.0f}¢")
    if m.get("close") and usd >= 1000:
        h = (m["close"] - t["ts"]) / 3600
        if 0 <= h <= 24:
            s += 3 if h <= 2 else 2; why.append(f"Pre-close: {h:.1f}h before close")
    if m.get("vol") and usd >= 500 and t["count"] / m["vol"] >= 0.25:
        s += 2; why.append(f"Thin market: {min(t['count']/m['vol'],1)*100:.0f}% of 24h volume")
    same = [x for x in recent if x["side"] == t["side"] and 0 <= t["ts"] - x["ts"] <= 300]
    if len(same) >= 3 and sum(x["usd"] for x in same) >= 3000:
        s += 2; why.append(f"Burst: {len(same)} same-side buys, ${sum(x['usd'] for x in same):,.0f} in 5 min")
    win = [x for x in recent if 0 <= t["ts"] - x["ts"] <= 900]
    if len(win) >= 2:
        move = (t["yes"] - win[0]["yes"]) * (1 if t["side"] == "yes" else -1)
        if move >= 0.15:
            s += 2; why.append(f"Price shock: +{move*100:.0f} pts toward {t['side'].upper()} in 15 min")
    return round(s, 1), why

def post(payload):
    req = urllib.request.Request(HOOK, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "kalshi-bot"})
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=15); return True
        except urllib.error.HTTPError as e:
            if e.code != 429: print("discord error", e.code); return False
            time.sleep(3)
    return False

def card(t, m, s, why):
    title = (m.get("title") or t["tk"]) + (f" — {m['sub']}" if m.get("sub") else "")
    series = (m.get("event") or t["tk"]).split("-")[0].lower()
    return {"title": ("🚨 " + title)[:256], "url": f"https://kalshi.com/markets/{series}",
            "color": 0xFF4F00 if s >= 8 else 0xFFB347 if s >= 6 else 0x9AA0A8,
            "description": "\n".join("• " + w for w in why),
            "fields": [{"name": "Bet", "value": f"{t['side'].upper()} @ {t['price']*100:.0f}¢", "inline": True},
                       {"name": "Size", "value": f"${t['usd']:,.0f}", "inline": True},
                       {"name": "Score", "value": str(s), "inline": True}],
            "footer": {"text": t["tk"] + " · trades are anonymous; a flag is a lead, not proof"}}

def main():
    if not HOOK: raise SystemExit("Add the DISCORD_WEBHOOK secret first")
    if os.environ.get("TEST") == "true":
        post({"content": "✅ Kalshi insider tracker is connected. Flags will show up here."}); return
    try: posted = json.load(open("state.json"))
    except Exception: posted = {}

    trades = fetch_trades()
    by_tk = defaultdict(list)
    for t in trades: by_tk[t["tk"]].append(t)
    big = {t["tk"] for t in trades if t["usd"] >= 400}
    markets = fetch_markets(sorted(big))

    flags = []
    for t in trades:
        if t["tk"] not in big or not t["side"] or t["usd"] < 250 or t["id"] in posted: continue
        s, why = score(t, markets.get(t["tk"], {}), [x for x in by_tk[t["tk"]] if x["ts"] <= t["ts"]])
        if s >= MIN_SCORE: flags.append((s, t, why))
    flags.sort(key=lambda f: (-f[0], -f[1]["usd"]))
    print(f"scanned {len(trades)} trades, {len(flags)} new flags")

    for i in range(0, min(len(flags), 10), 5):
        chunk = flags[i:i + 5]
        if post({"embeds": [card(t, markets.get(t["tk"], {}), s, w) for s, t, w in chunk]}):
            for s, t, w in chunk: posted[t["id"]] = t["ts"]
        time.sleep(1)
    for s, t, w in flags[10:]: posted[t["id"]] = t["ts"]

    cutoff = time.time() - 6 * 3600
    json.dump({k: v for k, v in posted.items() if v >= cutoff}, open("state.json", "w"))

main()
