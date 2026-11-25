import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA = "https://cdn.nba.com/static/json"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
ALIAS = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS", "PHO": "PHX"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def get(url, params=None):
    if params: url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if "nba.com" in url: headers.update(Referer="https://www.nba.com/", Origin="https://www.nba.com")
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            return json.load(urllib.request.urlopen(req, timeout=25))
        except Exception as e:
            if i == 2:
                print(f"fetch failed ({e}) {urllib.parse.urlparse(url).netloc}"); return {}
            time.sleep(2)

def code(abbr): return ALIAS.get((abbr or "").upper(), (abbr or "").upper())

def num(*v):
    for x in v:
        try:
            if x not in (None, ""): return float(x)
        except (TypeError, ValueError): pass
    return None

# ---------- games: try ESPN, fall back to NBA.com ----------
def team_espn(c):
    t = c["team"]
    rec = {r.get("type") or r.get("name", "").lower(): r.get("summary", "") for r in c.get("records", [])}
    return {"code": code(t["abbreviation"]), "abbr": t["abbreviation"], "name": t["displayName"],
            "short": t["shortDisplayName"], "rec": rec.get("total", ""), "home": rec.get("home", ""),
            "road": rec.get("road", "")}

def games_espn(day):
    out = []
    for ev in get(f"{ESPN}/scoreboard", {"dates": day.strftime("%Y%m%d")}).get("events", []):
        comp = ev["competitions"][0]
        side = {c["homeAway"]: team_espn(c) for c in comp["competitors"]}
        odds = (comp.get("odds") or [{}])[0]
        out.append({"away": side["away"], "home": side["home"],
                    "start": datetime.fromisoformat(ev["date"].replace("Z", "+00:00")),
                    "done": comp.get("status", {}).get("type", {}).get("state") == "post",
                    "line": (odds.get("details") or "") + (f", O/U {odds['overUnder']}" if odds.get("overUnder") else "")})
    return out

def team_nba(t):
    rec = f"{t.get('wins', '')}-{t.get('losses', '')}" if t.get("wins") is not None else ""
    return {"code": code(t.get("teamTricode")), "abbr": t.get("teamTricode", ""),
            "name": f"{t.get('teamCity', '')} {t.get('teamName', '')}".strip(), "short": t.get("teamName", ""),
            "rec": rec, "home": "", "road": ""}

_schedule = None
def nba_schedule():
    global _schedule
    if _schedule is None:
        _schedule = {}
        data = get(f"{NBA}/staticData/scheduleLeagueV2.json").get("leagueSchedule", {})
        for gd in data.get("gameDates", []):
            try: d = datetime.strptime(gd["gameDate"].split(" ")[0], "%m/%d/%Y").date()
            except Exception: continue
            _schedule[d] = gd.get("games", [])
    return _schedule

def games_nba(day):
    games = []
    sb = get(f"{NBA}/liveData/scoreboard/todaysScoreboard_00.json").get("scoreboard", {})
    if sb.get("gameDate") == day.isoformat():
        games = sb.get("games", [])
    if not games:
        games = nba_schedule().get(day, [])
    out = []
    for g in games:
        t = g.get("gameTimeUTC") or g.get("gameDateTimeUTC") or ""
        out.append({"away": team_nba(g["awayTeam"]), "home": team_nba(g["homeTeam"]),
                    "start": datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None,
                    "done": g.get("gameStatus") == 3, "line": ""})
    return out

def games_on(day):
    return games_espn(day) or games_nba(day)

def teams_played(day):
    gs = games_on(day)
    return {t["code"] for g in gs for t in (g["away"], g["home"])}

# ---------- injuries (ESPN; optional) ----------
def injuries(names):
    out = {}
    data = get(f"{ESPN}/injuries")
    for team in data.get("injuries", []):
        tcode, rows = None, []
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            tcode = tcode or code((ath.get("team") or {}).get("abbreviation", ""))
            rows.append((inj.get("status") or "", ath.get("displayName", "?")))
        tcode = tcode or names.get(team.get("displayName", ""), "")
        if tcode: out[tcode] = rows
    return out if data else None

# ---------- Kalshi prices ----------
def kalshi_prices():
    out = {}
    data = get(f"{KALSHI}/events", {"series_ticker": "KXNBAGAME", "status": "open",
                                    "with_nested_markets": "true", "limit": 200})
    for ev in data.get("events", []):
        for m in ev.get("markets", []):
            team = m.get("ticker", "").rsplit("-", 1)[-1].upper()
            ask = num(m.get("yes_ask_dollars"), (num(m.get("yes_ask")) or 0) / 100 or None)
            last = num(m.get("last_price_dollars"), (num(m.get("last_price")) or 0) / 100 or None)
            p = ask if ask and 0 < ask < 1 else last
            if p: out[team] = p
    return out

def moneyline(p):
    if not p or p <= 0 or p >= 1: return "—"
    return f"{-round(100*p/(1-p)):+d}" if p >= 0.5 else f"+{round(100*(1-p)/p)}"

def inj_lines(rows):
    if rows is None: return "Unavailable"
    order = {"out": 0, "doubtful": 1, "questionable": 2, "day-to-day": 3}
    keep = sorted([r for r in rows if r[0].lower() in order], key=lambda r: order[r[0].lower()])[:5]
    icon = {"out": "🔴", "doubtful": "🟠", "questionable": "🟡", "day-to-day": "🟡"}
    return "\n".join(f"{icon[s.lower()]} {n} ({s})" for s, n in keep) or "None listed"

def build(now):
    today = now.astimezone(ET).date()
    games = [g for g in games_on(today) if not g["done"]]
    if not games: return None, []
    yday = teams_played(today - timedelta(days=1))
    recent = [yday] + [teams_played(today - timedelta(days=d)) for d in (2, 3)]
    names = {t["name"]: t["code"] for g in games for t in (g["away"], g["home"])}
    inj, prices = injuries(names), kalshi_prices()

    def fatigue(t):
        if t in yday: return "⚠️ Back-to-back"
        if sum(t in s for s in recent) >= 2: return "⚠️ 3 games in 4 nights"
        return "Rested"

    embeds = []
    for g in games:
        A, H = g["away"], g["home"]
        pa, ph = prices.get(A["code"]), prices.get(H["code"])
        if pa or ph:
            ml = "\n".join(x for x in [
                f"**{A['short']}** {moneyline(pa)} ({pa*100:.0f}¢)" if pa else "",
                f"**{H['short']}** {moneyline(ph)} ({ph*100:.0f}¢)" if ph else ""] if x)
            fav = A["code"] if (pa or 0) > (ph or 0) else H["code"]
        else:
            ml, fav = "Not on Kalshi yet", None
        if g["line"]: ml += f"\nSportsbook line: {g['line']}"
        rec = (f"{A['abbr']} {A['rec']}" + (f" ({A['road']} road)" if A["road"] else "") + "\n" +
               f"{H['abbr']} {H['rec']}" + (f" ({H['home']} home)" if H["home"] else ""))
        fa, fh = fatigue(A["code"]), fatigue(H["code"])
        notes = []
        if fa != "Rested" and fh == "Rested": notes.append(f"{H['short']} have the rest edge")
        if fh != "Rested" and fa == "Rested": notes.append(f"{A['short']} have the rest edge")
        if inj and fav:
            outs = sum(r[0].lower() == "out" for r in inj.get(fav, []))
            if outs >= 2: notes.append(f"Favorite has {outs} players out — check if the line has moved")
        ia = None if inj is None else inj.get(A["code"], [])
        ih = None if inj is None else inj.get(H["code"], [])
        fields = [{"name": "💰 Kalshi moneyline", "value": ml[:1024], "inline": False},
                  {"name": "📊 Records", "value": rec.strip() or "—", "inline": True},
                  {"name": "😮‍💨 Fatigue", "value": f"{A['abbr']}: {fa}\n{H['abbr']}: {fh}", "inline": True},
                  {"name": f"🏥 {A['short']} injuries", "value": inj_lines(ia)[:1024], "inline": True},
                  {"name": f"🏥 {H['short']} injuries", "value": inj_lines(ih)[:1024], "inline": True}]
        if notes: fields.append({"name": "📝 Notes", "value": "\n".join("• " + n for n in notes), "inline": False})
        when = g["start"].astimezone(PT).strftime("%-I:%M %p") + " PT" if g["start"] else "Time TBD"
        embeds.append({"title": f"{A['name']} @ {H['name']}", "description": f"🕒 {when}",
                       "color": 0x1D428A, "fields": fields})
    header = (f"🏀 **NBA slate — {today.strftime('%a %b %-d')}** · {len(embeds)} games\n"
              f"_Moneylines from Kalshi prices (¢ = implied win chance). Not betting advice._")
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
