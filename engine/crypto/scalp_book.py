"""Paper 'scalp book': limit-order entries for 1-2 hour trades, tracked on 1-minute candles. NO real orders are ever placed.
Rules (same as the plan given to Martin):
  - Limit order waits up to 2h to fill. Fill = a closed 1m candle trades down to (long) / up to (short) the limit. Gap-through fills at the open.
  - Fill bar: only the stop is checked (TP1 ignored in the fill bar, conservative). After that: stop first, then TP1 inside each bar.
  - TP1 sells HALF and moves the stop to the entry (break-even). The rest goes to TP2 if set, else rides until the time exit.
  - Time exit: 2h after the fill, close what is left at that 1m close.
  - News cutoff (per batch): unfilled orders are cancelled and open trades closed at the cutoff.  BTC kill switch: unfilled orders cancelled if BTC falls below a level.
  - Sizing: risk RISK_USD per trade (notional = RISK_USD / stop distance). Fee 0.14% round trip on the traded notional. Each trade is tracked independently.
State: scalp_book.json (served by server.py at /api/scalp)."""
import os, json, time, threading, requests

OUT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(OUT, "scalp_book.json")
import datasrc
KL = datasrc.API + "/klines"
TP = datasrc.API + "/ticker/price"
RISK_USD, FEE, CAPITAL = 2.0, 0.0014, 100.0
HOLD_MS, FILL_MS = 2 * 3600 * 1000, 2 * 3600 * 1000
LOCK = threading.RLock()


def load():
    """Never silently start empty when a state file exists: fall back to the backup, else raise (so a bad read cannot overwrite good data)."""
    for f in (FILE, FILE + ".bak"):
        if os.path.exists(f):
            try:
                with open(f) as fh:
                    return json.load(fh)
            except Exception:
                continue
    if os.path.exists(FILE) or os.path.exists(FILE + ".bak"):
        raise RuntimeError("scalp_book state unreadable; refusing to start empty")
    return dict(trades=[], batches={}, prices={}, ts=0)


def save(d):
    tmp = FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1)
    for i in range(8):  # Windows: replace fails while another thread/process has the target open
        try:
            os.replace(tmp, FILE)
            break
        except PermissionError:
            time.sleep(0.25 * (i + 1))
    else:
        raise PermissionError("could not replace " + FILE)
    try:
        import shutil
        shutil.copyfile(FILE, FILE + ".bak")
    except Exception:
        pass


def now_ms():
    return int(time.time() * 1000)


def add_batch(name, picks, cutoff_ms=None, btc_kill=None, note="", placed_ms=None):
    """picks: list of dict(sym, side, entry, stop, tp1, tp2, reason)"""
    with LOCK:
        d = load()
        t0 = placed_ms or now_ms()
        d["batches"][name] = dict(placed=t0, cutoff=cutoff_ms, btc_kill=btc_kill, note=note)
        for p in picks:
            risk = abs(p["entry"] - p["stop"]) / p["entry"]
            notional = RISK_USD / risk
            d["trades"].append(dict(id=f"{name}:{p['sym']}", batch=name, sym=p["sym"], side=p.get("side", "LONG"), entry=p["entry"], stop=p["stop"], tp1=p["tp1"], tp2=p.get("tp2"),
                                    reason=p.get("reason", ""), placed=t0, expire=t0 + FILL_MS, status="pending", risk_pct=round(risk * 100, 2), notional=round(notional, 2), units=notional / p["entry"],
                                    stop_cur=p["stop"], half=False, legs=[], last_ms=t0 - 60000, filled_at=None, fill_px=None, closed_at=None, result=None, pnl=0.0))
        save(d)
        return d


def _bars(sym, start_ms):
    r = requests.get(KL, params=dict(symbol=sym, interval="1m", startTime=start_ms, limit=1500), timeout=20)
    if r.status_code != 200:
        return []
    return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in r.json()]


def _leg(t, px, frac, why):
    sg = 1 if t["side"] == "LONG" else -1
    units = t["units"] * frac
    gross = sg * (px - t["fill_px"]) * units
    fee = FEE * t["fill_px"] * units
    t["legs"].append(dict(px=px, frac=frac, why=why, pnl=round(gross - fee, 4)))
    t["pnl"] = round(sum(l["pnl"] for l in t["legs"]), 4)


def _close(t, px, ms, why):
    rem = 1.0 - sum(l["frac"] for l in t["legs"])
    if rem > 1e-9:
        _leg(t, px, rem, why)
    t["status"], t["closed_at"] = "closed", ms
    tp1_hit = t["half"]
    t["result"] = why if not tp1_hit else ("TP2" if why == "TP2" else f"TP1 + {why}")
    if not tp1_hit and why not in ("STOP", "TIME", "NEWS"):
        t["result"] = why


def _step(t, bar, batch, btc_below):
    ms, o, h, l, c = bar
    end = ms + 60000
    long = t["side"] == "LONG"
    cutoff = batch.get("cutoff")
    if t["status"] == "pending":
        ka = batch.get("killed_at")
        if ka and ms >= ka:
            t["status"], t["result"], t["closed_at"] = "cancelled", "BTC kill switch", ka
            return
        if end > t["expire"]:
            t["status"], t["result"], t["closed_at"] = "expired", "not filled in 2h", t["expire"]
            return
        if cutoff and ms >= cutoff:
            t["status"], t["result"], t["closed_at"] = "cancelled", "news cutoff", cutoff
            return
        hit = (l <= t["entry"]) if long else (h >= t["entry"])
        if not hit:
            return
        t["fill_px"] = min(o, t["entry"]) if long else max(o, t["entry"])
        t["filled_at"], t["status"] = ms, "open"
        if (long and l <= t["stop_cur"]) or (not long and h >= t["stop_cur"]):
            _close(t, t["stop_cur"], ms, "STOP")
        return
    if t["status"] != "open":
        return
    if cutoff and ms >= cutoff:
        _close(t, o, ms, "NEWS")
        return
    stop = t["stop_cur"]
    if (long and l <= stop) or (not long and h >= stop):
        _close(t, min(o, stop) if long else max(o, stop), ms, "STOP" if not t["half"] else "BE")
        return
    if not t["half"]:
        if (long and h >= t["tp1"]) or (not long and l <= t["tp1"]):
            _leg(t, t["tp1"], 0.5, "TP1")
            t["half"], t["stop_cur"] = True, t["fill_px"]
            if not t.get("tp2"):
                pass
    elif t.get("tp2") and ((long and h >= t["tp2"]) or (not long and l <= t["tp2"])):
        _close(t, t["tp2"], ms, "TP2")
        return
    if end >= t["filled_at"] + HOLD_MS:
        _close(t, c, ms, "TIME")


def update():
    with LOCK:
        d = load()
        try:
            px = {x["symbol"]: float(x["price"]) for x in requests.get(TP, timeout=20).json()}
        except Exception:
            px = d.get("prices", {})
        d["prices"] = {k: v for k, v in px.items() if any(t["sym"] == k for t in d["trades"]) or k == "BTCUSDT"}
        for name, b in d["batches"].items():
            if b.get("btc_kill") and not b.get("killed_at"):
                try:
                    r = requests.get(KL, params=dict(symbol="BTCUSDT", interval="1m", startTime=b["placed"], limit=1500), timeout=20).json()
                    for x in r:
                        if float(x[3]) < b["btc_kill"] and int(x[0]) + 60000 <= now_ms():
                            b["killed_at"] = int(x[0])
                            break
                except Exception:
                    pass
        for t in d["trades"]:
            if t["status"] in ("closed", "expired", "cancelled"):
                continue
            batch = d["batches"].get(t["batch"], {})
            btc_below = False
            try:
                bars = _bars(t["sym"], t["last_ms"] + 60000)
            except Exception:
                continue
            nowm = now_ms()
            for b in bars:
                if b[0] + 60000 > nowm:
                    break  # forming bar
                _step(t, b, batch, btc_below)
                t["last_ms"] = b[0]
                if t["status"] in ("closed", "expired", "cancelled"):
                    break
            # pending order whose window ended with no new bars
            if t["status"] == "pending" and nowm > t["expire"] + 60000:
                t["status"], t["result"], t["closed_at"] = "expired", "not filled in 2h", t["expire"]
            cut = batch.get("cutoff")
            if t["status"] == "pending" and cut and nowm > cut:
                t["status"], t["result"], t["closed_at"] = "cancelled", "news cutoff", cut
        d["ts"] = now_ms()
        save(d)
        return d


def view():
    with LOCK:
        d = load()
    px = d.get("prices", {})
    out = []
    for t in d["trades"]:
        r = dict(t)
        p = px.get(t["sym"])
        r["price"] = p
        if p:
            r["dist_pct"] = round((t["entry"] / p - 1) * 100, 2)
            if t["status"] == "open":
                sg = 1 if t["side"] == "LONG" else -1
                rem = 1.0 - sum(l["frac"] for l in t["legs"])
                r["unreal"] = round(t["pnl"] + sg * (p - t["fill_px"]) * t["units"] * rem - FEE * t["fill_px"] * t["units"] * rem, 2)
        r["total"] = r.get("unreal", t["pnl"]) if t["status"] == "open" else t["pnl"]
        r["r"] = round(r["total"] / RISK_USD, 2) if t["status"] in ("open", "closed") else None
        out.append(r)
    filled = [t for t in out if t["status"] in ("open", "closed")]
    closed = [t for t in out if t["status"] == "closed"]
    st = dict(placed=len(out), waiting=sum(t["status"] == "pending" for t in out), filled=len(filled), open=sum(t["status"] == "open" for t in out), closed=len(closed),
              unfilled=sum(t["status"] in ("expired", "cancelled") for t in out), tp1_hit=sum(1 for t in filled if t["half"]), stopped=sum(1 for t in closed if t["result"] in ("STOP",)),
              wins=sum(1 for t in closed if t["pnl"] > 0), losses=sum(1 for t in closed if t["pnl"] <= 0), realized=round(sum(t["pnl"] for t in closed), 2),
              net_now=round(sum(t["total"] for t in filled), 2), risk_usd=RISK_USD, capital=CAPITAL)
    return dict(ts=d.get("ts"), batches=d.get("batches"), stats=st, trades=out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        v = update()
    print(json.dumps(view()["stats"]))
