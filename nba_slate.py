import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
# ESPN and Kalshi spell a few team codes differently
ALIAS = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS", "PHO": "PHX", "BKN": "BKN"}

def get(url, params=None):
    if params: url += "?" + urllib.parse.urlencode(params)
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-slate-bot"})
            return json.load(urllib.request.urlopen(req, timeout=20))
        except Exception as e:
            if i == 2: print("fetch failed:", url, e); return {}
            time.sleep(2)

def code(abbr): return ALIAS.get(abbr.upper(), abbr.upper())

def num(*v):
    for x in v:
        try:
            if x not in (None, ""): return float(x)
        except (TypeError, ValueError): pass
    return None

def scoreboard(day):
    return get(f"{ESPN}/scoreboard", {"dates": day.strftime("%Y%m%d")}).get("events", [])

def teams_played(day):
    out = set()
    for ev in scoreboard(day):
        for c in ev["competitions"][0]["competitors"]:
            out.add(code(c["team"]["abbreviation"]))
    return out

def injuries(names):
    """team code -> list of (status, player, note)"""
    out = {}
    for team in get(f"{ESPN}/injuries").get("injuries", []):
        tcode = None
        rows = []
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            tcode = tcode or code((ath.get("team") or {}).get("abbreviation", ""))
            status = inj.get("status") or inj.get("type", {}).get("description", "")
            note = inj.get("shortComment") or (inj.get("details") or {}).get("type", "")
            rows.append((status, ath.get("displayName", "?"), note))
        if not tcode:
            tcode = code((team.get("team") or {}).get("abbreviation", "") or team.get("abbreviation", "")) \
                    or names.get(team.get("displayName", ""), "")
        if tcode: out[tcode] = rows
    return out

def kalshi_prices():
    """team code -> (yes price 0-1, market ticker) for open NBA game-winner markets"""
    out = {}
    data = get(f"{KALSHI}/events", {"series_ticker": "KXNBAGAME", "status": "open",
                                    "with_nested_markets": "true", "limit": 200})
    for ev in data.get("events", []):
        for m in ev.get("markets", []):
            tk = m.get("ticker", "")
            team = tk.rsplit("-", 1)[-1].upper()
            ask = num(m.get("yes_ask_dollars"), (num(m.get("yes_ask")) or 0) / 100 or None)
            last = num(m.get("last_price_dollars"), (num(m.get("last_price")) or 0) / 100 or None)
            p = ask if ask and 0 < ask < 1 else last
            if p: out[team] = (p, ev.get("event_ticker", ""))
    return out

def moneyline(p):
    if not p or p <= 0 or p >= 1: return "—"
    return f"{-round(100*p/(1-p)):+d}" if p >= 0.5 else f"+{round(100*(1-p)/p)}"

def record(c, kind):
    for r in c.get("records", []):
        if r.get("type") == kind or r.get("name", "").lower() == kind: return r.get("summary", "")
    return ""

def inj_lines(rows):
    order = {"out": 0, "doubtful": 1, "questionable": 2, "day-to-day": 3}
    rows = sorted(rows, key=lambda r: order.get(r[0].lower(), 4))
    keep = [r for r in rows if r[0].lower() in order][:5]
    if not keep: return "None listed"
    icon = {"out": "🔴", "doubtful": "🟠", "questionable": "🟡", "day-to-day": "🟡"}
    return "\n".join(f"{icon.get(s.lower(),'⚪')} {n} ({s})" for s, n, _ in keep)

def build(now):
    today = now.astimezone(ET).date()
    games = scoreboard(today)
    if not games: return None, []
    yday = teams_played(today - timedelta(days=1))
    recent = [teams_played(today - timedelta(days=d)) for d in (1, 2, 3)]
    names = {c["team"]["displayName"]: code(c["team"]["abbreviation"])
             for ev in games for c in ev["competitions"][0]["competitors"]}
    inj, prices = injuries(names), kalshi_prices()

    embeds = []
    for ev in games:
        comp = ev["competitions"][0]
        if comp.get("status", {}).get("type", {}).get("state") == "post": continue
        side = {c["homeAway"]: c for c in comp["competitors"]}
        away, home = side["away"], side["home"]
        a, h = code(away["team"]["abbreviation"]), code(home["team"]["abbreviation"])
        start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(PT)
        pa, ph = prices.get(a, (None,))[0], prices.get(h, (None,))[0]

        if pa or ph:
            ml = (f"**{away['team']['shortDisplayName']}** {moneyline(pa)} ({pa*100:.0f}¢)\n" if pa else "") + \
                 (f"**{home['team']['shortDisplayName']}** {moneyline(ph)} ({ph*100:.0f}¢)" if ph else "")
            fav = a if (pa or 0) > (ph or 0) else h
        else:
            ml, fav = "Not on Kalshi yet", None
        odds = (comp.get("odds") or [{}])[0]
        if odds.get("details"): ml += f"\nSportsbook line: {odds['details']}" + \
                                      (f", O/U {odds['overUnder']}" if odds.get("overUnder") else "")

        def fatigue(t):
            if t in yday: return "⚠️ Back-to-back"
            if sum(t in s for s in recent) >= 2: return "⚠️ 3 games in 4 nights"
            return "Rested"
        fat = f"{away['team']['abbreviation']}: {fatigue(a)}\n{home['team']['abbreviation']}: {fatigue(h)}"
        rec = (f"{away['team']['abbreviation']} {record(away,'total')} ({record(away,'road')} road)\n"
               f"{home['team']['abbreviation']} {record(home,'total')} ({record(home,'home')} home)")

        notes = []
        if fatigue(a) != "Rested" and fatigue(h) == "Rested": notes.append(f"{home['team']['shortDisplayName']} have the rest edge")
        if fatigue(h) != "Rested" and fatigue(a) == "Rested": notes.append(f"{away['team']['shortDisplayName']} have the rest edge")
        outs = {t: sum(r[0].lower() == "out" for r in inj.get(t, [])) for t in (a, h)}
        if fav and outs[fav] >= 2: notes.append(f"Favorite has {outs[fav]} players out — check if the line has moved")

        fields = [{"name": "💰 Kalshi moneyline", "value": ml[:1024], "inline": False},
                  {"name": "📊 Records", "value": rec, "inline": True},
                  {"name": "😮‍💨 Fatigue", "value": fat, "inline": True},
                  {"name": f"🏥 {away['team']['shortDisplayName']} injuries", "value": inj_lines(inj.get(a, []))[:1024], "inline": True},
                  {"name": f"🏥 {home['team']['shortDisplayName']} injuries", "value": inj_lines(inj.get(h, []))[:1024], "inline": True}]
        if notes: fields.append({"name": "📝 Notes", "value": "\n".join("• " + n for n in notes), "inline": False})
        embeds.append({"title": f"{away['team']['displayName']} @ {home['team']['displayName']}",
                       "description": f"🕒 {start.strftime('%-I:%M %p')} PT",
                       "color": 0x1D428A, "fields": fields})
    header = f"🏀 **NBA slate — {today.strftime('%a %b %-d')}** · {len(embeds)} games\n" \
             f"_Moneylines from Kalshi prices (¢ = implied win chance). Injuries from ESPN. Not betting advice._"
    return header, embeds

def post(payload):
    req = urllib.request.Request(HOOK, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "nba-slate-bot"})
    urllib.request.urlopen(req, timeout=15)

def main():
    if not HOOK: raise SystemExit("Add the DISCORD_WEBHOOK_NBA secret first")
    header, embeds = build(datetime.now(ET))
    if not header:
        print("no games today"); return
    if os.environ.get("UPDATE") == "true":
        header = header.replace("NBA slate", "NBA pre-game update")
    for i in range(0, len(embeds), 10):
        post({"content": header, "embeds": embeds[i:i + 10]} if i == 0 else {"embeds": embeds[i:i + 10]})
        time.sleep(1)
    print(f"posted {len(embeds)} games")

main()
