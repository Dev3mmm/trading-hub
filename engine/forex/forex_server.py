"""Forex news dashboard server (http://localhost:8766).
  - polls the free TradingView economic calendar (high impact only) for actual/forecast/previous
  - polls Yahoo 1-minute prices for the majors + gold
  - turns each release into a verdict (positive/negative for the currency) and long/short suggestions for related pairs
  - paper-trades the main pair of every release with a realistic entry (5 min AFTER the release) and keeps a scoreboard
Not financial advice."""
import os, json, time, threading, traceback, datetime as dt
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import numpy as np, requests

OUT = os.path.dirname(os.path.abspath(__file__))
os.chdir(OUT)
PORT = 8766
CUR = {"US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY", "AU": "AUD", "CA": "CAD", "CH": "CHF", "NZ": "NZD"}
YAHOO = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X",
         "USDCHF": "USDCHF=X", "NZDUSD": "NZDUSD=X", "XAUUSD": "GC=F"}
MAIN = {"USD": "EURUSD", "EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY", "AUD": "AUDUSD", "CAD": "USDCAD", "CHF": "USDCHF", "NZD": "NZDUSD"}
INVERTED = ("unemployment", "jobless", "claims")  # fallback when there is no history: a HIGHER number is bad for the currency
STOP_BP, HOLD_MIN, COST_BP, RISK_USD = 10, 10, 2.0, 100  # first-10-minutes strategy: exit at T+10 min or a 10bp stop
DELAYS = (1, 2, 3, 5)  # paper-trade several entry speeds (minutes after the release) to learn how fast you must be
TVH = {"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"}
S = {"events": [], "bars": {}, "cal_ts": 0, "bar_ts": {}, "err": {}, "seen": {}, "waiting": set()}
LOCK = threading.RLock()
TRADES_F = os.path.join(OUT, "forex_trades.json")


def log(m):
    open(os.path.join(OUT, "forex_server.log"), "a", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


def playbook():
    try:
        return json.load(open(os.path.join(OUT, "playbook.json")))
    except Exception:
        return {}


PLAY = playbook()


def pol_of(key, title):
    p = PLAY.get(key)
    conv = -1 if any(w in title.lower() for w in INVERTED) else 1  # conventional: a beat is positive, except unemployment/claims
    if p and p["n"] >= 8 and abs(p["t"]) >= 1.5:
        return p["pol"], p["conf"]  # history is convincing: use what actually happened
    return conv, (p["conf"] if p and p["n"] >= 8 else "no history")


def related(cur, positive):
    out = []
    for pair in YAHOO:
        base, quote = pair[:3], pair[3:]
        if cur == base:
            out.append((pair, "long" if positive else "short"))
        elif cur == quote:
            out.append((pair, "short" if positive else "long"))
    return out


def tv_calendar():
    now = dt.datetime.now(dt.timezone.utc)
    a, b = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z"), (now + dt.timedelta(days=9)).strftime("%Y-%m-%dT00:00:00.000Z")
    r = requests.get(f"https://economic-calendar.tradingview.com/events?from={a}&to={b}&countries={','.join(CUR)}&minImportance=1", headers=TVH, timeout=25)
    r.raise_for_status()
    return r.json().get("result", [])


def bars(pair, rng="1d"):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO[pair]}?interval=1m&range={rng}", headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    d = r.json()["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    rows = [(t * 1000, o, h, l, c) for t, o, h, l, c in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
    return np.array(rows, dtype=float)


def active_window(now_ms):
    return any(abs(now_ms - e["T"]) < 100 * 60000 for e in S["events"])


def hot_events(now_ms):
    """Events inside the critical window: 3 min before to 12 min after the release."""
    return [e for e in S["events"] if -12 * 60000 <= e["T"] - now_ms <= 3 * 60000]


def loops():
    while True:
        now_ms = int(time.time() * 1000)
        hot = hot_events(now_ms)
        try:
            if time.time() - S["cal_ts"] > (5 if hot else 45 if active_window(now_ms) else 600):
                raw = tv_calendar()
                evs = []
                for e in raw:
                    cur = CUR.get(e["country"])
                    if not cur:
                        continue
                    ev = dict(id=e["id"], key=f"{e['country']}|{e['title']}", title=e["title"], country=e["country"], cur=cur,
                              T=int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp() * 1000),
                              actual=e.get("actual"), forecast=e.get("forecast"), previous=e.get("previous"), unit=e.get("unit") or "", period=e.get("period") or "")
                    got = int(time.time() * 1000)
                    if ev["actual"] is None:
                        S["waiting"].add(ev["id"])
                    elif ev["id"] not in S["seen"]:  # data latency: only meaningful if we were already watching this event before it printed
                        S["seen"][ev["id"]] = got if ev["id"] in S["waiting"] else None
                    ev["latency_s"] = round((S["seen"][ev["id"]] - ev["T"]) / 1000) if S["seen"].get(ev["id"]) else None
                    evs.append(ev)
                with LOCK:
                    S["events"] = sorted(evs, key=lambda x: x["T"])
                S["cal_ts"] = time.time()
        except Exception:
            log("calendar: " + traceback.format_exc().splitlines()[-1])
        hot_pairs = {MAIN[e["cur"]] for e in hot}
        busy = bool(hot) or active_window(now_ms) or any(t["state"] in ("waiting", "open") for t in load_trades())
        for pair in YAHOO:
            gap = 12 if pair in hot_pairs else 75 if busy else 420
            if time.time() - S["bar_ts"].get(pair, 0) < gap:
                continue
            try:
                old = S["bars"].get(pair)
                b = bars(pair, "1d" if old is not None else "7d")
                if old is not None:  # merge the fresh day into the 7-day history
                    m = {int(x[0]): x for x in old}
                    m.update({int(x[0]): x for x in b})
                    b = np.array([m[k] for k in sorted(m)])
                with LOCK:
                    S["bars"][pair] = b
                S["bar_ts"][pair] = time.time()
                S["err"].pop(pair, None)
            except Exception as ex:
                S["err"][pair] = str(ex)[:60]
                S["bar_ts"][pair] = time.time() - gap + 30
            time.sleep(0.5 if hot_pairs else 1.5)
        try:
            engine()
        except Exception:
            log("engine: " + traceback.format_exc())
        time.sleep(2 if hot else 10)


def load_trades():
    try:
        return json.load(open(TRADES_F))
    except Exception:
        return []


def save_trades(t):
    json.dump(t, open(TRADES_F + ".tmp", "w"), indent=1)
    os.replace(TRADES_F + ".tmp", TRADES_F)


def verdict(e):
    if e["actual"] is None or e["forecast"] is None:
        return None
    s = e["actual"] - e["forecast"]
    if s == 0:
        return "in line"
    pol, _ = pol_of(e["key"], e["title"])
    return "positive" if pol * (1 if s > 0 else -1) > 0 else "negative"


def engine():
    """Paper trades for the FIRST 10 MINUTES: for every release, the main pair is entered `d` minutes after the release
    (d in DELAYS), exited at T+10 min or at a 10bp stop, 2bp cost. Bars are 1-minute; entry = close of the minute ending at T+d."""
    now_ms = int(time.time() * 1000)
    with LOCK:
        trades = load_trades()
        ids = {t["id"] for t in trades}
        for e in S["events"]:
            v = verdict(e)
            if v in ("positive", "negative") and now_ms - e["T"] < 7.5 * 86400000:
                pair = MAIN[e["cur"]]
                side = dict(related(e["cur"], v == "positive"))[pair]
                _, conf = pol_of(e["key"], e["title"])
                for d in DELAYS:
                    tid = f"{e['id']}|{d}"
                    if tid not in ids:
                        trades.append(dict(id=tid, event=f"{e['country']} {e['title']}", cur=e["cur"], pair=pair, side=side, verdict=v, conf=conf, T=e["T"],
                                           delay=d, actual=e["actual"], forecast=e["forecast"], state="waiting"))
        for t in trades:
            if t["state"] not in ("waiting", "open"):
                continue
            b = S["bars"].get(t["pair"])
            d = t.get("delay", 1)
            if b is None or not len(b) or now_ms < t["T"] + d * 60000 + 15000:
                continue
            tt = b[:, 0]
            i = int(np.searchsorted(tt, t["T"] + (d - 1) * 60000))
            if i >= len(tt) or tt[i] > t["T"] + d * 60000:
                if now_ms - t["T"] > 20 * 60000:
                    t["state"] = "no data"
                continue
            entry, sgn = b[i, 4], 1 if t["side"] == "long" else -1
            t.update(state="open", entry=float(entry), entry_ts=int(tt[i]) + 60000)
            end = t["T"] + HOLD_MIN * 60000
            for j in range(i + 1, len(tt)):
                adverse = (entry - b[j, 3]) / entry * 1e4 if sgn > 0 else (b[j, 2] - entry) / entry * 1e4
                if adverse >= STOP_BP:
                    t.update(state="closed", exit="stop", bp=-STOP_BP - COST_BP, close_ts=int(tt[j]))
                    break
                if tt[j] + 60000 >= end:
                    t.update(state="closed", exit="time", bp=float(sgn * (b[j, 4] / entry - 1) * 1e4 - COST_BP), close_ts=int(tt[j]) + 60000)
                    break
            if t["state"] == "open":
                t["bp_now"] = float(sgn * (b[-1, 4] / entry - 1) * 1e4 - COST_BP)
        save_trades(trades)


def reaction(e):
    """How the currency has moved since the release, from live 1-minute bars (bp of currency strength, per pair)."""
    out = {}
    for pair in YAHOO:
        base, quote = pair[:3], pair[3:]
        if e["cur"] not in (base, quote):
            continue
        b = S["bars"].get(pair)
        if b is None or not len(b):
            continue
        i = int(np.searchsorted(b[:, 0], e["T"]))
        if i >= len(b) or b[i, 0] - e["T"] > 120000:
            continue
        m = 1 if e["cur"] == base else -1
        out[pair] = round(float(m * (b[-1, 4] / b[i, 1] - 1) * 1e4), 1)
    return out


def state():
    now_ms = int(time.time() * 1000)
    with LOCK:
        evs = list(S["events"])
    out = []
    for e in evs:
        if e["T"] < now_ms - 7.5 * 86400000:
            continue
        pol, conf = pol_of(e["key"], e["title"])
        p = PLAY.get(e["key"])
        v = verdict(e)
        row = dict(e, pol=pol, conf=conf, verdict=v, play=dict(n=p["n"], hit=p["hit"], mean_pos=p["mean_pos"], mean_neg=p["mean_neg"], avg_abs=p["avg_abs"], t=p["t"]) if p else None,
                   if_pos=related(e["cur"], True), if_neg=related(e["cur"], False))
        if v in ("positive", "negative", "in line") and now_ms - e["T"] < 3 * 3600000:
            row["reaction"] = reaction(e)
        out.append(row)
    trades = load_trades()
    cl = [t for t in trades if t["state"] == "closed"]

    def st(rs):
        if not rs:
            return None
        return dict(n=len(rs), win=round(100 * sum(1 for t in rs if t["bp"] > 0) / len(rs)), avg_bp=round(sum(t["bp"] for t in rs) / len(rs), 1),
                    usd=round(sum(t["bp"] / STOP_BP * RISK_USD for t in rs)))
    by_delay = {str(d): st([t for t in cl if t.get("delay") == d]) for d in DELAYS}
    d1 = [t for t in cl if t.get("delay") == 1]
    return dict(now=now_ms, events=out, trades=trades[::-1][:80], by_delay=by_delay,
                stats=dict(strong=st([t for t in d1 if t["conf"] == "strong"]), moderate=st([t for t in d1 if t["conf"] == "moderate"]),
                           weak=st([t for t in d1 if t["conf"] in ("weak", "no history")])),
                errors=S["err"], cal_age=int(time.time() - S["cal_ts"]), rules=dict(stop=STOP_BP, hold=HOLD_MIN, delays=list(DELAYS), cost=COST_BP, risk=RISK_USD))


SNIP_LOG = os.path.join(OUT, "sniper_log.json")
_snip = {"t": 0, "v": []}


def sniper_targets():
    """Releases the sniper will watch in the next 40h (forecast published, currency has a live proxy instrument)."""
    if time.time() - _snip["t"] < 300:
        return _snip["v"]
    try:
        now = dt.datetime.now(dt.timezone.utc)
        a, b = now.strftime("%Y-%m-%dT%H:%M:00.000Z"), (now + dt.timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:00.000Z")
        r = requests.get(f"https://economic-calendar.tradingview.com/events?from={a}&to={b}&countries={','.join(CUR)}&minImportance=0", headers=TVH, timeout=20).json()["result"]
        out = [dict(T=int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp() * 1000), cur=CUR[e["country"]], title=e["title"], imp=e["importance"],
                    forecast=e.get("forecast"), previous=e.get("previous"), unit=e.get("unit") or "")
               for e in r if CUR.get(e["country"]) in ("USD", "EUR") and e.get("forecast") is not None and e.get("actual") is None]
        _snip.update(t=time.time(), v=sorted(out, key=lambda x: x["T"]))
    except Exception:
        pass
    return _snip["v"]


def sniper_summary():
    try:
        lg = json.load(open(SNIP_LOG))
    except Exception:
        lg = []
    real = [x for x in lg if not x.get("test")]
    grid = {}
    for x in real:
        for t in x["trades"]:
            for h, bp in t["exits"].items():
                g = grid.setdefault(str(t["entry_delay_s"]), {}).setdefault(h, [])
                g.append(bp)
    table = {d: {h: dict(n=len(v), avg=round(sum(v) / len(v), 2), win=round(100 * sum(1 for y in v if y > 0) / len(v))) for h, v in hs.items()} for d, hs in grid.items()}
    lat = [x["detected_s"] for x in real if x.get("detected_s") is not None]
    return dict(sessions=list(reversed(lg))[:30], table=table, n_real=len(real), avg_latency=round(sum(lat) / len(lat), 1) if lat else None, targets=sniper_targets()[:14])


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            b = json.dumps(state()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        elif u.path == "/api/sniper":
            b = json.dumps(sniper_summary()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        elif u.path == "/api/sniper_ticks":
            q = parse_qs(u.query)
            try:
                tk = json.load(open(os.path.join(OUT, "sniper_ticks", q["id"][0].replace("/", "") + ".json")))
                b = json.dumps([t for t in tk if t[1] == q["inst"][0]]).encode()
            except Exception:
                b = b"[]"
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        elif u.path == "/api/history":
            k = parse_qs(u.query).get("key", [""])[0]
            p = dict(PLAY.get(k, {}))
            if p:
                p["pol"] = pol_of(k, k.split("|", 1)[-1])[0]  # same direction the board uses
            b = json.dumps(p).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
        else:
            if u.path in ("/", ""):
                self.path = "/forex.html"
            super().do_GET()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    S["start"] = int(time.time() * 1000)
    threading.Thread(target=loops, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
