#!/usr/bin/env python3
"""
Kalshi Insider-Bet Tracker
--------------------------
Polls Kalshi's public trade tape (no API key needed), scores every trade for
patterns that often show up around insider trading, stores everything in
SQLite, and serves a live dashboard at http://localhost:8787

Patterns scored:
  * Whale bet        - one trade with a large dollar amount
  * Longshot conviction - big money on a side priced at 20c or less
  * Pre-close timing - large bet in the last 24h (esp. 2h) before a market closes
  * Thin-market hit  - one trade is a big share of the market's 24h volume
  * Split-order burst - several same-side trades in one market within minutes
  * Price shock      - the market moved 15+ points in the taker's favor within 15 min

Kalshi trades are anonymous: this flags suspicious *trades*, not people.
A flag is a lead, not proof.

Usage:
  python3 kalshi_insider_tracker.py            # live
  python3 kalshi_insider_tracker.py --demo     # synthetic data, no network
  DISCORD_WEBHOOK=https://... python3 kalshi_insider_tracker.py   # also push alerts

Requires only the Python 3.9+ standard library.
"""

import argparse
import json
import math
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.elections.kalshi.com/trade-api/v2"

# ---- Tunable thresholds (override with env vars) ---------------------------
def _env(name, default):
    return type(default)(os.environ.get(name, default))

WHALE_USD        = _env("WHALE_USD", 5000.0)    # single trade size for "whale"
LONGSHOT_PRICE   = _env("LONGSHOT_PRICE", 0.20) # price paid at or below = longshot
LONGSHOT_USD     = _env("LONGSHOT_USD", 1000.0)
PRECLOSE_HOURS   = _env("PRECLOSE_HOURS", 24.0)
PRECLOSE_USD     = _env("PRECLOSE_USD", 1000.0)
THIN_SHARE       = _env("THIN_SHARE", 0.25)     # trade / 24h volume
THIN_USD         = _env("THIN_USD", 500.0)
BURST_WINDOW_S   = _env("BURST_WINDOW_S", 300)
BURST_USD        = _env("BURST_USD", 3000.0)
BURST_MIN_TRADES = _env("BURST_MIN_TRADES", 3)
SHOCK_WINDOW_S   = _env("SHOCK_WINDOW_S", 900)
SHOCK_MOVE       = _env("SHOCK_MOVE", 0.15)
MIN_ALERT_USD    = _env("MIN_ALERT_USD", 250.0)
ALERT_SCORE      = _env("ALERT_SCORE", 4.0)
META_MIN_USD     = _env("META_MIN_USD", 400.0)  # only look up market details above this
POLL_SECONDS     = _env("POLL_SECONDS", 15)
DISCORD_WEBHOOK  = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_MIN_SCORE = _env("DISCORD_MIN_SCORE", 6.0)

# ---- Helpers ---------------------------------------------------------------
def fnum(*vals):
    """First value that parses as a float (Kalshi sends both cents ints and
    fixed-point dollar strings depending on field/version)."""
    for v in vals:
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None

def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

def http_get(path, params=None, retries=3):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "insider-tracker/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:                    # rate limited: back off
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET failed: {url}")

def normalize_trade(t):
    yes = fnum(t.get("yes_price_dollars"))
    if yes is None:
        c = fnum(t.get("yes_price"))
        yes = c / 100 if c is not None else None
    no = fnum(t.get("no_price_dollars"))
    if no is None:
        c = fnum(t.get("no_price"))
        no = c / 100 if c is not None else (1 - yes if yes is not None else None)
    side = (t.get("taker_side") or "").lower()
    count = fnum(t.get("count_fp"), t.get("count")) or 0
    paid = yes if side == "yes" else no
    ts = parse_ts(t.get("created_time")) or time.time()
    return {
        "trade_id": t.get("trade_id") or f"{t.get('ticker')}-{ts}-{count}",
        "ticker": t.get("ticker", ""),
        "side": side,
        "count": count,
        "yes_price": yes,
        "price": paid,
        "usd": round(count * (paid or 0), 2),
        "ts": ts,
    }

def normalize_market(m):
    return {
        "ticker": m.get("ticker", ""),
        "event_ticker": m.get("event_ticker", ""),
        "title": m.get("title") or "",
        "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
        "close_ts": parse_ts(m.get("close_time")),
        "volume_24h": fnum(m.get("volume_24h_fp"), m.get("volume_24h")) or 0,
        "open_interest": fnum(m.get("open_interest_fp"), m.get("open_interest")) or 0,
        "status": m.get("status", ""),
        "fetched": time.time(),
    }

# ---- Storage ---------------------------------------------------------------
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trades(trade_id TEXT PRIMARY KEY, ticker TEXT, side TEXT,
            count REAL, yes_price REAL, price REAL, usd REAL, ts REAL);
        CREATE INDEX IF NOT EXISTS trades_ts ON trades(ts);
        CREATE TABLE IF NOT EXISTS alerts(trade_id TEXT PRIMARY KEY, ticker TEXT, title TEXT,
            subtitle TEXT, event_ticker TEXT, side TEXT, price REAL, usd REAL, ts REAL,
            close_ts REAL, score REAL, reasons TEXT);
        CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
        CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
        """)

    def get(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (k, str(v)))
            self.db.commit()

    def add_trades(self, trades):
        """Insert, return only the ones not seen before."""
        new = []
        with self.lock:
            for t in trades:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?)",
                    (t["trade_id"], t["ticker"], t["side"], t["count"],
                     t["yes_price"], t["price"], t["usd"], t["ts"]))
                if cur.rowcount:
                    new.append(t)
            self.db.commit()
        return new

    def add_alert(self, a):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["trade_id"], a["ticker"], a["title"], a["subtitle"], a["event_ticker"],
                 a["side"], a["price"], a["usd"], a["ts"], a["close_ts"], a["score"],
                 json.dumps(a["reasons"])))
            self.db.commit()

    def alerts(self, since, min_score, q, limit=300):
        sql = ("SELECT * FROM alerts WHERE ts>=? AND score>=? "
               + ("AND (ticker LIKE ? OR title LIKE ? OR subtitle LIKE ?) " if q else "")
               + "ORDER BY ts DESC LIMIT ?")
        args = [since, min_score] + ([f"%{q}%"] * 3 if q else []) + [limit]
        with self.lock:
            cur = self.db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["reasons"] = json.loads(r["reasons"])
        return rows

    def stats(self, since):
        with self.lock:
            n, vol = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(usd),0) FROM trades WHERE ts>=?", (since,)).fetchone()
            na, big = self.db.execute(
                "SELECT COUNT(*), COALESCE(MAX(usd),0) FROM alerts WHERE ts>=?", (since,)).fetchone()
            hourly = self.db.execute(
                "SELECT CAST((ts-?)/3600 AS INT) h, COUNT(*) FROM alerts WHERE ts>=? GROUP BY h",
                (since, since)).fetchall()
            top = self.db.execute(
                "SELECT ticker, title, COUNT(*) c, SUM(usd) s, MAX(score) m FROM alerts "
                "WHERE ts>=? GROUP BY ticker ORDER BY m DESC, s DESC LIMIT 8", (since,)).fetchall()
        return {"trades": n, "volume": round(vol, 2), "alerts": na, "biggest": big,
                "hourly": hourly,
                "top": [dict(ticker=t, title=ti, count=c, usd=round(s, 2), max_score=m)
                        for t, ti, c, s, m in top]}

    def prune(self, keep_days=14):
        cutoff = time.time() - keep_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM trades WHERE ts<?", (cutoff,))
            self.db.commit()

# ---- Detection -------------------------------------------------------------
class Detector:
    def __init__(self, store, market_source):
        self.store = store
        self.market_source = market_source     # callable(list[ticker]) -> dict
        self.meta = {}
        self.recent = defaultdict(deque)       # ticker -> deque of trades (last 15 min)

    def _remember(self, t):
        dq = self.recent[t["ticker"]]
        dq.append(t)
        horizon = t["ts"] - max(SHOCK_WINDOW_S, BURST_WINDOW_S)
        while dq and dq[0]["ts"] < horizon:
            dq.popleft()

    def _need_meta(self, tickers):
        stale = [tk for tk in tickers
                 if tk not in self.meta or time.time() - self.meta[tk]["fetched"] > 600]
        if stale:
            try:
                self.meta.update(self.market_source(stale))
            except Exception as e:
                print("market lookup failed:", e)

    def process(self, trades):
        trades = sorted(trades, key=lambda t: t["ts"])
        for t in trades:
            self._remember(t)

        # Only look up details for trades that could matter
        cand = [t for t in trades if t["usd"] >= META_MIN_USD or self._burst(t)[0]]
        self._need_meta(sorted({t["ticker"] for t in cand}))

        out = []
        for t in cand:
            a = self.score(t)
            if a and a["score"] >= ALERT_SCORE and a["usd"] >= MIN_ALERT_USD:
                self.store.add_alert(a)
                out.append(a)
        return out

    def _burst(self, t):
        window = [x for x in self.recent[t["ticker"]]
                  if x["side"] == t["side"] and t["ts"] - BURST_WINDOW_S <= x["ts"] <= t["ts"]]
        total = sum(x["usd"] for x in window)
        return (len(window) >= BURST_MIN_TRADES and total >= BURST_USD), len(window), total

    def score(self, t):
        if not t["side"] or t["price"] is None:
            return None
        m = self.meta.get(t["ticker"], {})
        usd, price = t["usd"], t["price"]
        score, reasons = 0.0, []

        if usd >= WHALE_USD:
            pts = min(4.0, 2.0 + math.log2(usd / WHALE_USD))
            score += pts
            reasons.append(f"Whale bet ${usd:,.0f}")

        if price <= LONGSHOT_PRICE and usd >= LONGSHOT_USD:
            pts = 3 if price <= 0.10 else 2
            score += pts
            reasons.append(f"Longshot: ${usd:,.0f} on {t['side'].upper()} at {price*100:.0f}¢")

        close_ts = m.get("close_ts")
        if close_ts and usd >= PRECLOSE_USD:
            hrs = (close_ts - t["ts"]) / 3600
            if 0 <= hrs <= PRECLOSE_HOURS:
                score += 3 if hrs <= 2 else 2
                reasons.append(f"Pre-close: {hrs:.1f}h before close")

        vol = m.get("volume_24h") or 0
        if vol > 0 and usd >= THIN_USD:
            share = t["count"] / vol
            if share >= THIN_SHARE:
                score += 2
                reasons.append(f"Thin market: {min(share,1)*100:.0f}% of 24h volume")

        is_burst, n, total = self._burst(t)
        if is_burst:
            score += 2
            reasons.append(f"Burst: {n} {t['side'].upper()} buys, ${total:,.0f} in {BURST_WINDOW_S//60} min")

        # Price shock in the taker's favor (measured in YES-price terms)
        window = [x for x in self.recent[t["ticker"]]
                  if t["ts"] - SHOCK_WINDOW_S <= x["ts"] <= t["ts"] and x["yes_price"] is not None]
        if len(window) >= 2 and t["yes_price"] is not None:
            move = t["yes_price"] - window[0]["yes_price"]
            if t["side"] == "no":
                move = -move
            if move >= SHOCK_MOVE:
                score += 2
                reasons.append(f"Price shock: +{move*100:.0f} pts toward {t['side'].upper()} in {SHOCK_WINDOW_S//60} min")

        if not reasons:
            return None
        return {**t, "title": m.get("title", ""), "subtitle": m.get("subtitle", ""),
                "event_ticker": m.get("event_ticker", ""), "close_ts": close_ts,
                "score": round(score, 1), "reasons": reasons}

# ---- Data sources ----------------------------------------------------------
def live_markets(tickers):
    out = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            data = http_get("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            for m in data.get("markets", []):
                out[m["ticker"]] = normalize_market(m)
        except Exception:
            pass
        for tk in chunk:                         # fallback one-by-one
            if tk not in out:
                try:
                    out[tk] = normalize_market(http_get(f"/markets/{urllib.parse.quote(tk)}")["market"])
                except Exception:
                    pass
    return out

def live_fetch(store, max_pages=20):
    since = int(float(store.get("last_ts", time.time() - 600)))
    trades, cursor, newest = [], None, since
    for _ in range(max_pages):
        params = {"limit": 1000, "min_ts": since}
        if cursor:
            params["cursor"] = cursor
        data = http_get("/markets/trades", params)
        batch = [normalize_trade(t) for t in data.get("trades", [])]
        trades += batch
        for t in batch:
            newest = max(newest, int(t["ts"]))
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    # overlap by a few seconds; duplicates are dropped by trade_id
    store.put("last_ts", max(since, newest - 5))
    return trades

class Demo:
    """Synthetic tape with some planted insider-looking activity."""
    def __init__(self):
        now = time.time()
        specs = [
            ("KXNBAGAME-26NOV25LALBOS-BOS", "Lakers vs Celtics winner?", "Celtics", 30),
            ("KXOSCARPIC-27-ANORA", "Best Picture winner?", "Film X", 24 * 60),
            ("KXFEDDECISION-26DEC-H0", "Fed decision in December?", "Hold", 20 * 24),
            ("KXMRBEAST-26NOV-500M", "MrBeast video views?", "Above 500M", 3),
            ("KXCEOOUT-26-ACME", "Will the Acme CEO step down?", "Yes", 48),
            ("KXSENATE-26-MN", "Minnesota Senate race winner?", "Candidate A", 72),
            ("KXNFLGAME-26NOV30KCBUF-KC", "Chiefs vs Bills winner?", "Chiefs", 120),
            ("KXAPPLEEVENT-26-FOLD", "Apple announces foldable?", "Yes", 6),
        ]
        self.markets = {tk: {"ticker": tk, "event_ticker": tk.rsplit("-", 1)[0], "title": ti,
                             "subtitle": sub, "close_ts": now + h * 3600,
                             "volume_24h": random.choice([2000, 8000, 40000, 250000]),
                             "open_interest": 0, "status": "active", "fetched": now}
                        for tk, ti, sub, h in specs}
        self.price = {tk: random.uniform(0.15, 0.85) for tk in self.markets}
        self.n = 0

    def market_source(self, tickers):
        return {tk: self.markets[tk] for tk in tickers if tk in self.markets}

    def fetch(self, store):
        now, out = time.time(), []
        for _ in range(random.randint(20, 60)):
            tk = random.choice(list(self.markets))
            self.price[tk] = min(0.97, max(0.03, self.price[tk] + random.gauss(0, 0.01)))
            side = random.choice(["yes", "no"])
            out.append(self._t(tk, side, random.choice([5, 10, 25, 50, 100, 300]), now))
        r = random.random()
        tk = random.choice(list(self.markets))
        if r < 0.25:     # whale
            out.append(self._t(tk, "yes", random.randint(8000, 40000), now))
        elif r < 0.45:   # longshot
            self.price[tk] = 0.08
            out.append(self._t(tk, "yes", random.randint(15000, 60000), now))
        elif r < 0.6:    # burst + shock
            for i in range(5):
                self.price[tk] = min(0.97, self.price[tk] + 0.05)
                out.append(self._t(tk, "yes", random.randint(1500, 4000), now - 60 + i * 10))
        return out

    def _t(self, tk, side, count, ts):
        self.n += 1
        yes = round(self.price[tk], 2)
        return normalize_trade({"trade_id": f"demo-{self.n}", "ticker": tk, "taker_side": side,
                                "count": count, "yes_price": yes * 100, "no_price": (1 - yes) * 100,
                                "created_time": datetime.fromtimestamp(ts, timezone.utc).isoformat()})

# ---- Alerts out ------------------------------------------------------------
def push_discord(a):
    if not DISCORD_WEBHOOK or a["score"] < DISCORD_MIN_SCORE:
        return
    msg = (f"**Score {a['score']}** · {a['title'] or a['ticker']} — {a['subtitle']}\n"
           f"{a['side'].upper()} at {a['price']*100:.0f}¢ · ${a['usd']:,.0f}\n"
           + "\n".join("• " + r for r in a["reasons"]))
    try:
        req = urllib.request.Request(DISCORD_WEBHOOK, data=json.dumps({"content": msg}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print("discord failed:", e)

# ---- Poll loop -------------------------------------------------------------
STATUS = {"last_poll": None, "last_error": None, "mode": "live"}

def poller(store, fetch, detector, interval):
    last_prune = 0
    while True:
        try:
            new = store.add_trades(fetch(store))
            alerts = detector.process(new)
            for a in alerts:
                print(f"[ALERT {a['score']}] {a['ticker']} {a['side']} ${a['usd']:,.0f} :: "
                      + " | ".join(a["reasons"]))
                push_discord(a)
            STATUS.update(last_poll=time.time(), last_error=None)
            if time.time() - last_prune > 3600:
                store.prune()
                last_prune = time.time()
        except Exception as e:
            STATUS["last_error"] = str(e)
            print("poll error:", e)
        time.sleep(interval)

# ---- Web server ------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))

def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            hours = float(q.get("hours", ["24"])[0])
            since = time.time() - hours * 3600
            if u.path == "/":
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/alerts":
                rows = store.alerts(since, float(q.get("min_score", ["0"])[0]),
                                    q.get("q", [""])[0].strip())
                self._send(200, json.dumps(rows).encode(), "application/json")
            elif u.path == "/api/stats":
                s = store.stats(since)
                s.update(status=STATUS, hours=hours, now=time.time(),
                         thresholds={"whale": WHALE_USD, "alert_score": ALERT_SCORE})
                self._send(200, json.dumps(s).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
    return H

def main():
    ap = argparse.ArgumentParser(description="Kalshi insider-bet tracker")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to view from your phone on the same Wi-Fi")
    ap.add_argument("--db", default=os.path.join(HERE, "kalshi_tracker.db"))
    args = ap.parse_args()

    if args.demo:
        args.db = os.path.join(HERE, "kalshi_demo.db")
        demo = Demo()
        store, fetch, msrc, interval = Store(args.db), demo.fetch, demo.market_source, 5
        STATUS["mode"] = "demo"
    else:
        store, fetch, msrc, interval = Store(args.db), live_fetch, live_markets, POLL_SECONDS

    det = Detector(store, msrc)
    threading.Thread(target=poller, args=(store, fetch, det, interval), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"Dashboard: http://{'localhost' if args.host=='127.0.0.1' else args.host}:{args.port}  "
          f"({STATUS['mode']} mode, Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
