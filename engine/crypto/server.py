"""Local dashboard server (port 8765): serves the scanner folder + JSON APIs, rescans every 10 min, refreshes trade outcomes every 2 min.
  /api/live      paper trades at live futures prices + stats
  /api/feed      entry alerts (setups that reached their entry zone)
  /api/candles   candles for the trade chart
  /api/close     POST ?key=...  close an open trade at market / cancel an unfilled one"""
import os, json, time, threading, traceback
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import requests
import tracker
import trend_scan
import scalp_book

OUT = os.path.dirname(os.path.abspath(__file__))
os.chdir(OUT)
RISK_USD = 100  # paper risk per trade: 1R = $100
SCAN_EVERY = 600
import datasrc
FAPI = datasrc.API
_prices = {"t": 0, "d": {}}
_scan = {"running": False, "last": 0, "error": None}


def log(msg):
    with open(os.path.join(OUT, "server.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def prices():
    if time.time() - _prices["t"] > 4:
        try:
            r = requests.get(FAPI + "/ticker/price", timeout=10).json()
            _prices["d"] = {x["symbol"]: float(x["price"]) for x in r}
            _prices["t"] = time.time()
        except Exception:
            pass
    return _prices["d"]


def live():
    trades = tracker.load()
    px = prices()
    out, real, unreal = [], 0.0, 0.0
    for t in trades:
        risk = abs(t["entry"] - t["sl"])
        d = 1 if t["side"] == "long" else -1
        p = px.get(t["symbol"] + "USDT")
        row = dict(key=t["key"], symbol=t["symbol"], side=t["side"], tf=t["tf"], state=t["state"], entry=t["entry"], sl=t["sl"],
                   tp1=t["tp1"], rr=t["rr"], zone=t.get("zone"), ann=t.get("ann"), std=bool(t.get("std")), ts=t["ts"], fill_ts=t.get("fill_ts"), close_ts=t.get("close_ts"),
                   price=p, lev=t.get("lev"), score=t["score"], session=tracker.session(t["ts"]), r=None)
        if t["state"] == "open" and p:
            row["r"] = round(d * (p - t["entry"]) / risk - t.get("fee_r", 0), 2)
            unreal += row["r"]
        elif t["state"] in ("win", "loss", "timeout", "manual"):
            row["r"] = t["r"]
            real += t["r"]
        elif t["state"] == "pending" and p:
            row["away_pct"] = round(100 * (p - t["entry"]) / t["entry"], 2)
            row["stale"] = d * (p - t["entry"]) / risk > 1.0  # price already ran >1R the way we wanted, without filling
        if row["r"] is not None:
            row["usd"] = round(row["r"] * RISK_USD, 2)
        out.append(row)
    cl = [x for x in out if x["state"] in ("win", "loss", "timeout", "manual")]
    wins = sum(1 for x in cl if x["r"] > 0)
    sessions = [dict(name=n, closed=c, wins=w, winrate=wr, avg_r=a) for n, c, w, wr, a in tracker.stats(trades)
                if "UTC" in n]
    try:
        if datasrc.HOSTED:
            scan_ts = int(json.load(open(os.path.join(OUT, "hosted_meta.json")))["last_scan"] * 1000)
        else:
            scan_ts = int(os.path.getmtime(os.path.join(OUT, "dashboard.html")) * 1000)
    except (OSError, KeyError, ValueError):
        scan_ts = 0
    return dict(trades=out, risk_usd=RISK_USD, realized_usd=round(real * RISK_USD, 2), unrealized_usd=round(unreal * RISK_USD, 2),
                closed=len(cl), wins=wins, winrate=round(100 * wins / len(cl)) if cl else None, sessions=sessions,
                scan_ts=scan_ts, scanning=_scan["running"], scan_error=_scan["error"], ts=int(time.time() * 1000))


FLIP0, FLIP_RISK, FLIP_MINPOS, FLIP_MAXPOS = 20.0, 0.10, 5.0, 2
FLIP_CFG = os.path.join(OUT, "flip_cfg.json")


def flip():
    """The $20 flip account (paper). Every STANDARD trade that FILLS after the account started is taken automatically:
    position = the size that loses at most 10% of the account at the stop (=$2 at $20), never more than the free cash (no leverage),
    at most 2 open at once, minimum $5 order. Replayed from the trade log, so it always matches the tracker."""
    try:
        cfg = json.load(open(FLIP_CFG))
    except Exception:
        cfg = {"start": int(time.time() * 1000)}
        json.dump(cfg, open(FLIP_CFG, "w"))
    start = cfg["start"]
    px = prices()
    std = [t for t in tracker.load() if t.get("std")]
    ev = []
    for t in std:
        if t.get("fill_ts") and t["fill_ts"] >= start:
            ev.append((t["fill_ts"], 1, t))
            if t["state"] in ("win", "loss", "timeout", "manual") and t.get("close_ts"):
                ev.append((max(t["close_ts"], t["fill_ts"]), 0, t))
    ev.sort(key=lambda e: (e[0], e[1]))
    cash, pos, closed, skipped = FLIP0, {}, [], []
    for ts, kind, t in ev:
        sd = (t["entry"] - t["sl"]) / t["entry"]
        if kind == 1:
            eq = cash + sum(p["size"] for p in pos.values())
            if len(pos) >= FLIP_MAXPOS or cash < FLIP_MINPOS:
                skipped.append(dict(symbol=t["symbol"], ts=ts, why="no free cash or 2 trades already open"))
                continue
            size = min(FLIP_RISK * eq / sd, cash)
            pos[t["key"]] = dict(symbol=t["symbol"], size=size, sd=sd, entry=t["entry"], sl=t["sl"], tp1=t["tp1"], opened=ts, fee_r=t.get("fee_r", 0), t=t)
            cash -= size
        else:
            p = pos.pop(t["key"], None)
            if p:
                pnl = p["size"] * p["sd"] * t["r"]
                cash += p["size"] + pnl
                closed.append(dict(symbol=t["symbol"], size=round(p["size"], 2), r=t["r"], pnl=round(pnl, 2), closed=ts, opened=p["opened"], state=t["state"]))
    open_rows, unreal = [], 0.0
    for p in pos.values():
        price = px.get(p["symbol"] + "USDT")
        risk = p["entry"] - p["sl"]
        r_now = ((price - p["entry"]) / risk - p["fee_r"]) if price else 0.0
        u = p["size"] * p["sd"] * r_now
        unreal += u
        open_rows.append(dict(symbol=p["symbol"], size=round(p["size"], 2), entry=p["entry"], price=price, stop=p["sl"], tp1=p["tp1"], max_loss=round(p["size"] * p["sd"], 2),
                              target_gain=round(p["size"] * (p["tp1"] / p["entry"] - 1), 2), unreal=round(u, 2), opened=p["opened"]))
    invested = sum(p["size"] for p in pos.values())
    equity = cash + invested + unreal
    realized = sum(c["pnl"] for c in closed)
    waiting = []
    eq_cost = cash + invested
    for t in std:
        if t["state"] == "pending":
            pxn = px.get(t["symbol"] + "USDT")
            if pxn and (pxn - t["entry"]) / (t["entry"] - t["sl"]) > 1.0:
                continue  # price already ran away from the entry: not a live plan, do not list it
            sd = (t["entry"] - t["sl"]) / t["entry"]
            size = min(FLIP_RISK * eq_cost / sd, cash) if len(pos) < FLIP_MAXPOS and cash >= FLIP_MINPOS else 0
            waiting.append(dict(symbol=t["symbol"], entry=t["entry"], stop=t["sl"], tp1=t["tp1"], price=px.get(t["symbol"] + "USDT"), size=round(size, 2), max_loss=round(size * sd, 2),
                                target_gain=round(size * (t["tp1"] / t["entry"] - 1), 2), stop_pct=round(100 * sd, 1)))
    return dict(start=start, start_equity=FLIP0, equity=round(equity, 2), cash=round(cash, 2), invested=round(invested, 2), realized=round(realized, 2), unrealized=round(unreal, 2),
                open=open_rows, closed=closed[::-1][:30], waiting=waiting, skipped=skipped[-5:], wins=sum(1 for c in closed if c["pnl"] > 0), n_closed=len(closed),
                rules=dict(max_loss_pct=100 * FLIP_RISK, max_open=FLIP_MAXPOS, min_order=FLIP_MINPOS))


def plan():
    """STANDARD setups with everything needed to explain them: live price, paper-trade state and the backtest record for that kind of setup."""
    try:
        setups = json.load(open(os.path.join(OUT, "setups.json")))
        stats = json.load(open(os.path.join(OUT, "standard_stats.json")))
    except Exception:
        return {"error": "not ready"}
    px = prices()
    trades = tracker.load()
    out = []
    for x in setups:
        if not x.get("std"):
            continue
        t = next((t for t in reversed(trades) if t.get("std") and t["symbol"] == x["symbol"] and t["tf"] == x["tf"] and t["side"] == x["side"]
                  and abs(t["entry"] - x["entry"]) / x["entry"] < 0.02), None)
        rr = x["rr"]
        bucket = stats["rr_1_5_2"] if rr < 2 else stats.get("rr_2_3") if rr < 3 else None
        out.append(dict(x, live=px.get(x["symbol"] + "USDT"), trade={k: t.get(k) for k in ("state", "r", "fill_ts", "added", "key")} if t else None,
                        bucket=bucket or stats["all"]))
    cl = [t for t in trades if t.get("std") and t["state"] in ("win", "loss", "timeout", "manual")]
    try:
        exits = json.load(open(os.path.join(OUT, "standard_exits.json")))
    except Exception:
        exits = None
    return dict(exits=exits, plans=out, stats=stats, live_std=dict(closed=len(cl), wins=sum(1 for t in cl if t["r"] > 0), open=sum(1 for t in trades if t.get("std") and t["state"] == "open"),
                pending=sum(1 for t in trades if t.get("std") and t["state"] == "pending")), ts=int(time.time() * 1000))


class H(SimpleHTTPRequestHandler):
    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/flip":
            self.send_json(flip())
        elif u.path == "/api/plan":
            self.send_json(plan())
        elif u.path == "/api/trend":
            try:
                self.send_json(json.load(open(os.path.join(OUT, "trend_state.json"))))
            except Exception:
                self.send_json({"error": "not ready"}, 503)
        elif u.path == "/api/bots":
            try:
                self.send_json(json.load(open(os.path.join(OUT, "bots_state.json"))))
            except Exception:
                self.send_json({"error": "not ready"}, 503)
        elif u.path == "/api/scalp":
            try:
                self.send_json(scalp_book.view())
            except Exception:
                self.send_json({"error": "not ready"}, 503)
        elif u.path == "/api/trend_candles":
            try:
                r = requests.get(FAPI + "/klines", params=dict(symbol=q["symbol"] + "USDT", interval="1d", limit=170), timeout=15).json()
                self.send_json([[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r])
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif u.path == "/api/live":
            self.send_json(live())
        elif u.path == "/api/feed":
            keys = {t["key"] for t in tracker.load() if t.get("std")}  # alerts are for STANDARD setups only; hide the old ones logged before that rule
            self.send_json([x for x in reversed(tracker.feed_load()) if x["key"] in keys or x.get("std")][:40])
        elif u.path == "/api/candles":
            try:
                r = requests.get(FAPI + "/klines", params=dict(symbol=q["symbol"] + "USDT", interval=q["tf"],
                                                               startTime=int(float(q["from"])), limit=500), timeout=15).json()
                self.send_json([[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r])
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            if u.path in ("/", ""):
                self.path = "/dashboard.html"
            super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/close":
            t = next((x for x in tracker.load() if x["key"] == q.get("key")), None)
            p = prices().get(t["symbol"] + "USDT") if t else None
            ok = bool(t and p and tracker.manual_close(t["key"], p))
            self.send_json({"ok": ok})
        else:
            self.send_json({"error": "not found"}, 404)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def scanner_loop():
    import scanner
    while True:
        if time.time() - _scan["last"] >= SCAN_EVERY:
            _scan["running"] = True
            try:
                scanner.main()
                _scan["error"] = None
            except Exception:
                _scan["error"] = "scan failed, see server.log"
                log(traceback.format_exc())
            _scan["running"], _scan["last"] = False, time.time()
        time.sleep(15)


def trend_loop():
    while True:
        try:
            trend_scan.run()
        except Exception:
            log(traceback.format_exc())
        try:
            import bots_live
            bots_live.run()
        except Exception:
            log(traceback.format_exc())
        time.sleep(1800)


def scalp_loop():
    while True:
        try:
            scalp_book.update()
        except Exception:
            log(traceback.format_exc())
        time.sleep(20)


def refresher():
    while True:
        time.sleep(120)
        try:
            tracker.refresh()
        except Exception:
            log(traceback.format_exc())


if __name__ == "__main__":
    threading.Thread(target=refresher, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    threading.Thread(target=trend_loop, daemon=True).start()
    threading.Thread(target=scalp_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
