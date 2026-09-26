"""News sniper (demo / paper): watches high+medium impact releases in real time and paper-trades the first minutes.
  - polls the TradingView calendar every ~0.7s from 45s before to 5 min after a release, until the actual number appears
  - streams live bid/ask (Binance EURUSDT, GBPUSDT, AUDUSDT spot + XAUUSDT gold futures) 4x per second
  - the moment the number is seen: verdict (positive/negative for the currency) -> long/short each related instrument
  - paper fills at the real ask (buy) / bid (sell), entered 0s / 2s / 5s after detection, exited 30s / 60s / 3min / 5min after the RELEASE time
  - logs everything (incl. the raw price path) to sniper_log.json + sniper_ticks/<id>.json.  No real orders are ever placed.
Run: pythonw sniper.py        Test with a fake release in ~1 min: python sniper.py test"""
import os, sys, json, time, threading, traceback, datetime as dt
import requests
import forex_server as fs

OUT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(OUT, "sniper_log.json")
TICKS = os.path.join(OUT, "sniper_ticks")
os.makedirs(TICKS, exist_ok=True)
HOSTED = os.environ.get("HOSTED") == "1"  # GitHub runner: Binance futures/api.binance.com are blocked (451), use the spot mirror and skip gold futures
INST = ({"USD": [("EURUSDT", -1, "spot")], "EUR": [("EURUSDT", 1, "spot")]} if HOSTED else
        {"USD": [("EURUSDT", -1, "spot"), ("XAUUSDT", -1, "fut")], "EUR": [("EURUSDT", 1, "spot")]})  # GBPUSDT/AUDUSDT on Binance have dead quotes
ENTRY_DELAYS = (0, 2, 5)  # seconds after the number is detected
EXIT_AFTER = (30, 60, 180, 300)  # seconds after the release time
SPOT = "https://data-api.binance.vision/api/v3" if HOSTED else "https://api.binance.com/api/v3"
FUT = "https://fapi.binance.com/fapi/v1"
LOGLOCK = threading.Lock()
TEST = {"on": False}


def slog(m):
    open(os.path.join(OUT, "sniper.log"), "a", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


def clock_offset():
    """ms to add to the local clock to get true time (Binance server time; best of 3 by round trip)."""
    best = None
    for _ in range(3):
        t0 = time.time() * 1000
        st = requests.get(SPOT + "/time", timeout=5).json()["serverTime"]
        t1 = time.time() * 1000
        if best is None or t1 - t0 < best[0]:
            best = (t1 - t0, st - (t0 + t1) / 2)
    return best[1]


def tnow(off):
    return time.time() * 1000 + off


def poll_prices(insts, off, ticks, until, stop):
    spot = [i for i, m, k in insts if k == "spot"]
    fut = [i for i, m, k in insts if k == "fut"]
    sess = requests.Session()
    while not stop.is_set() and tnow(off) < until:
        t0 = time.time()
        try:
            if spot:
                r = sess.get(SPOT + "/ticker/bookTicker", params={"symbols": json.dumps(spot, separators=(",", ":"))}, timeout=4).json()
                ts = tnow(off)
                for x in r:
                    if float(x["bidPrice"]) > 0 and float(x["askPrice"]) > 0:  # skip dead quotes
                        ticks.append([ts, x["symbol"], float(x["bidPrice"]), float(x["askPrice"])])
            if fut:
                for s in fut:
                    x = sess.get(FUT + "/ticker/bookTicker", params={"symbol": s}, timeout=4).json()
                    if float(x["bidPrice"]) > 0 and float(x["askPrice"]) > 0:
                        ticks.append([tnow(off), x["symbol"], float(x["bidPrice"]), float(x["askPrice"])])
        except Exception:
            pass
        time.sleep(max(0, 0.25 - (time.time() - t0)))


def fetch_event(ev):
    """Latest calendar row for this event id (narrow window so the call is fast)."""
    if TEST["on"]:
        return dict(ev, actual=TEST["actual"] if time.time() * 1000 >= ev["T"] + TEST["delay"] else None)
    a = dt.datetime.fromtimestamp((ev["T"] - 1800000) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    b = dt.datetime.fromtimestamp((ev["T"] + 1800000) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get(f"https://economic-calendar.tradingview.com/events?from={a}&to={b}&countries={ev['country']}&minImportance=-1", headers=fs.TVH, timeout=6).json()
    for e in r.get("result", []):
        if e["id"] == ev["id"]:
            return dict(ev, actual=e.get("actual"), forecast=e.get("forecast"), previous=e.get("previous"))
    return ev


def quote(ticks, inst, ts, want):
    """First tick for `inst` at/after ts. want = 'bid' or 'ask'."""
    for t in ticks:
        if t[1] == inst and t[0] >= ts:
            return t[0], (t[2] if want == "bid" else t[3])
    return None, None


def run_session(group):
    """group = events sharing one release time; the trigger (highest importance) decides the trade."""
    trig = group[0]
    T, cur = trig["T"], trig["cur"]
    insts = INST[cur]
    off = clock_offset()
    while tnow(off) < T - 45000:
        time.sleep(0.5)
    ticks, stop = [], threading.Event()
    threading.Thread(target=poll_prices, args=(insts, off, ticks, T + 330000, stop), daemon=True).start()
    det = None
    seen = {}
    while tnow(off) < T + 300000:
        t0 = time.time()
        try:
            e = fetch_event(trig)
            if e.get("actual") is not None and det is None:
                det = tnow(off)
                trig = e
                slog(f"DETECTED {trig['title']} actual={trig['actual']} +{(det - T) / 1000:.1f}s after release")
                break
        except Exception:
            pass
        time.sleep(max(0, 0.7 - (time.time() - t0)))
    rec = dict(id=trig["id"], event=f"{trig['country']} {trig['title']}", cur=cur, T=T, forecast=trig.get("forecast"), previous=trig.get("previous"),
               actual=trig.get("actual"), detected_s=None if det is None else round((det - T) / 1000, 1), test=TEST["on"], trades=[], others=[])
    v = fs.verdict(trig) if det else None
    rec["verdict"] = v
    # wait for the price path to complete
    while tnow(off) < T + 325000:
        time.sleep(0.5)
    stop.set()
    for o in group[1:]:  # other releases at the same minute: log what they said (no trade)
        try:
            o2 = fetch_event(o)
            rec["others"].append(dict(event=f"{o['country']} {o['title']}", forecast=o2.get("forecast"), actual=o2.get("actual"), verdict=fs.verdict(o2) if o2.get("actual") is not None else None))
        except Exception:
            pass
    if v in ("positive", "negative"):
        up = v == "positive"
        for inst, m, kind in insts:
            long = up == (m == 1)
            for d in ENTRY_DELAYS:
                ets, ep = quote(ticks, inst, det + d * 1000, "ask" if long else "bid")
                if ep is None:
                    continue
                row = dict(inst=inst, side="long" if long else "short", entry_delay_s=d, entry_after_release_s=round((ets - T) / 1000, 1), entry=ep, exits={})
                for h in EXIT_AFTER:
                    xts, xp = quote(ticks, inst, T + h * 1000, "bid" if long else "ask")
                    if xp is not None and xts > ets:
                        row["exits"][str(h)] = round((1 if long else -1) * (xp / ep - 1) * 1e4, 2)
                rec["trades"].append(row)
    mid0 = {}
    for inst, m, kind in insts:  # what the market did (mid) 10s before vs at +30/+60s, whatever we traded
        a = [t for t in ticks if t[1] == inst]
        if a:
            def mid(ts):
                for t in a:
                    if t[0] >= ts:
                        return (t[2] + t[3]) / 2
            m0 = mid(T - 2000)
            mid0[inst] = {str(s): round((mid(T + s * 1000) / m0 - 1) * 1e4, 2) for s in (5, 15, 30, 60, 180, 300) if m0 and mid(T + s * 1000)}
    rec["mid_move_bp"] = mid0
    json.dump(ticks, open(os.path.join(TICKS, f"{trig['id']}.json"), "w"))
    with LOGLOCK:
        try:
            lg = json.load(open(LOG))
        except Exception:
            lg = []
        lg.append(rec)
        json.dump(lg, open(LOG, "w"), indent=1)
    slog(f"session done {rec['event']} verdict={v} trades={len(rec['trades'])}")


def upcoming():
    now = dt.datetime.now(dt.timezone.utc)
    a, b = (now - dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:00.000Z"), (now + dt.timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:00.000Z")
    r = requests.get(f"https://economic-calendar.tradingview.com/events?from={a}&to={b}&countries={','.join(fs.CUR)}&minImportance=0", headers=fs.TVH, timeout=20).json()["result"]
    out = []
    for e in r:
        cur = fs.CUR.get(e["country"])
        if cur in INST and e.get("forecast") is not None and e.get("actual") is None:
            out.append(dict(id=e["id"], key=f"{e['country']}|{e['title']}", title=e["title"], country=e["country"], cur=cur, imp=e["importance"],
                            T=int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp() * 1000), actual=None,
                            forecast=e.get("forecast"), previous=e.get("previous"), unit=e.get("unit") or ""))
    return out


def main():
    started = set()
    slog("sniper started")
    while True:
        try:
            evs = upcoming()
            groups = {}
            for e in evs:
                groups.setdefault((e["T"], e["cur"]), []).append(e)
            for (T, cur), g in groups.items():
                g.sort(key=lambda e: -e["imp"])
                if (T, cur) not in started and T - time.time() * 1000 < 10 * 60000:
                    started.add((T, cur))
                    slog(f"armed for {g[0]['cur']} {g[0]['title']} at {time.strftime('%H:%M:%S', time.localtime(T / 1000))} local (+{len(g) - 1} others)")
                    threading.Thread(target=run_session, args=(g,), daemon=True).start()
        except Exception:
            slog("loop: " + traceback.format_exc().splitlines()[-1])
        time.sleep(20)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        TEST.update(on=True, actual=0.5, delay=7000)  # fake USD release in ~50s, number "appears" 7s later
        T = int(time.time() * 1000) + 50000
        ev = dict(id=f"test{T}", key="US|Durable Goods Orders MoM", title="TEST Durable Goods Orders MoM", country="US", cur="USD", imp=1, T=T, actual=None,
                  forecast=-0.4, previous=1.1, unit="%")
        run_session([ev])
        print(json.dumps(json.load(open(LOG))[-1], indent=1)[:2500])
    else:
        main()
        while True:
            time.sleep(60)
