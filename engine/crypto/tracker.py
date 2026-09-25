"""Paper-trade forward tester. Every scanner setup becomes a simulated limit order; outcomes come from real Binance 1h candles.
Rules (conservative): fill when price touches entry; on the fill candle only the stop is checked; if stop and TP1 are in the
same candle it counts as a loss; unfilled after 72h or TP1 reached before fill = expired (not counted); still open after 10 days
= closed at market (timeout). Win = R>0, loss = -1R, win = +R:R to TP1."""
import os, json, time, threading
import requests
import safeio

LOCK = threading.RLock()

OUT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(OUT, "trades.json")
TFMS = {"1h": 3600000, "4h": 14400000}
import datasrc
API = datasrc.API + "/klines"
HOUR = 3600000


def load():
    return safeio.load_json(FILE, [])


def save(trades):
    safeio.save_json(FILE, trades, indent=1)


def candles(sym, start):
    try:
        r = requests.get(API, params=dict(symbol=sym + "USDT", interval="1h", startTime=int(start), limit=1000), timeout=15)
        d = r.json() if r.status_code == 200 else []
    except requests.RequestException:
        return []
    now = time.time() * 1000
    return [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in d if int(x[6]) < now]


def step(t):
    long = t["side"] == "long"
    risk = abs(t["entry"] - t["sl"])
    for ts, o, h, l, c in candles(t["symbol"], t["checked"]):
        t["checked"] = ts + HOUR
        hit_sl = (long and l <= t["sl"]) or (not long and h >= t["sl"])
        if t["state"] == "pending":
            if (long and h >= t["tp1"] and l > t["entry"]) or (not long and l <= t["tp1"] and h < t["entry"]):
                t["state"], t["close_ts"] = "expired", ts
                return
            if l <= t["entry"] <= h:
                t["state"], t["fill_ts"] = "open", ts
                if hit_sl:
                    t["state"], t["r"], t["close_ts"] = "loss", -1.0 - t.get("fee_r", 0), ts
                    return
                continue
            if ts - t["ts"] > 72 * HOUR:
                t["state"], t["close_ts"] = "expired", ts
                return
        elif t["state"] == "open":
            if hit_sl:
                t["state"], t["r"], t["close_ts"] = "loss", -1.0 - t.get("fee_r", 0), ts
                return
            if (long and h >= t["tp1"]) or (not long and l <= t["tp1"]):
                t["state"], t["r"], t["close_ts"] = "win", round(t["rr"] - t.get("fee_r", 0), 2), ts
                return
            if ts - t["fill_ts"] > 10 * 24 * HOUR:
                t["state"], t["close_ts"] = "timeout", ts
                t["r"] = round(((c - t["entry"]) if long else (t["entry"] - c)) / risk - t.get("fee_r", 0), 2)
                return


def update(data):
  with LOCK:
    trades = load()
    keys = {t["key"] for t in trades}
    active = {(t["symbol"], t["side"], t["tf"]) for t in trades if t["state"] in ("pending", "open")}
    for d in data:
        k = keyof(d)
        if k in keys:
            tt = next(x for x in trades if x["key"] == k)
            if d.get("ann") and not tt.get("ann"):
                tt["ann"], tt["zone"] = d["ann"], d.get("zone")
            if d.get("std") and not tt.get("std"):
                tt["std"] = True
            continue
        if (d["symbol"], d["side"], d["tf"]) in active:
            if d.get("std"):  # the live trade for this idea predates the STANDARD tag: upgrade it if it is the same zone
                for tt in trades:
                    if tt["state"] in ("pending", "open") and (tt["symbol"], tt["side"], tt["tf"]) == (d["symbol"], d["side"], d["tf"])                             and abs(tt["entry"] - d["entry"]) / tt["entry"] < 0.02 and not tt.get("std"):
                        tt["std"] = True
            continue  # one live idea per coin/side/timeframe, like the backtest
        active.add((d["symbol"], d["side"], d["tf"]))
        trades.append(dict(key=k, zone=d.get("zone"), ann=d.get("ann"), symbol=d["symbol"], side=d["side"], tf=d["tf"], score=d["score"], status0=d["status"],
                           entry=d["entry"], sl=d["sl"], tp1=d["tp1"], rr=d["rr"], ts=d["ts"], checked=d["ts"],
                           std=bool(d.get("std")), lev=d.get("lev"), fee_r=round(d.get("fee_r", 0), 4), state="pending", added=time.strftime("%Y-%m-%d %H:%M")))
    for t in trades:
        if t["state"] in ("pending", "open"):
            step(t)
    save(trades)
    return trades


def keyof(d):
    return f"{d['symbol']}|{d['side']}|{d['tf']}|{d['entry']:.6g}"


def refresh():
    with LOCK:
        trades = load()
        for t in trades:
            if t["state"] in ("pending", "open"):
                step(t)
        save(trades)


def manual_close(key, price):
    """Close an open trade at the given price (counts in results), or cancel one that has not filled."""
    with LOCK:
        trades = load()
        for t in trades:
            if t["key"] != key:
                continue
            if t["state"] == "open":
                risk = abs(t["entry"] - t["sl"])
                d = 1 if t["side"] == "long" else -1
                t["r"] = round(d * (price - t["entry"]) / risk - t.get("fee_r", 0), 2)
                t["state"], t["close_ts"], t["close_px"] = "manual", int(time.time() * 1000), price
            elif t["state"] == "pending":
                t["state"], t["close_ts"] = "cancelled", int(time.time() * 1000)
            else:
                return False
            save(trades)
            return True
    return False


FEED = os.path.join(OUT, "feed.json")


def feed_load():
    return safeio.load_json(FEED, [])


def feed_add(data):
    """Log every setup that is in its entry zone (once each) so the dashboard can post it as an alert."""
    with LOCK:
        feed = feed_load()
        seen = {f["key"] for f in feed}
        for d in data:
            k = keyof(d)
            if "IN ZONE" in d["status"] and d.get("std") and k not in seen:  # alerts only for STANDARD setups
                feed.append(dict(key=k, symbol=d["symbol"], side=d["side"], tf=d["tf"], score=d["score"], entry=d["entry"],
                                 sl=d["sl"], tp1=d["tp1"], rr=d["rr"], lev=d.get("lev"), ms=int(time.time() * 1000)))
        safeio.save_json(FEED, feed[-200:], indent=1)


def session(ms):
    h = time.gmtime(ms / 1000).tm_hour
    return "Asia (00-07 UTC)" if h < 7 else "London (07-13 UTC)" if h < 13 else "New York (13-21 UTC)" if h < 21 else "Late (21-24 UTC)"


def closed(trades):
    return [t for t in trades if t["state"] in ("win", "loss", "timeout", "manual")]


def stats(trades):
    res = closed(trades)

    def row(name, ts):
        n = len(ts)
        if not n:
            return None
        w = sum(1 for t in ts if t["r"] > 0)
        return (name, n, w, round(100 * w / n), round(sum(t["r"] for t in ts) / n, 2))

    groups = [row("All", res), row("Buys", [t for t in res if t["side"] == "long"]),
              row("Sells", [t for t in res if t["side"] == "short"]),
              row("1h", [t for t in res if t["tf"] == "1h"]), row("4h", [t for t in res if t["tf"] == "4h"]),
              row("Score 70+", [t for t in res if t["score"] >= 70]), row("Score under 70", [t for t in res if t["score"] < 70])]
    groups.append(row("STANDARD v1", [t for t in res if t.get("std")]))
    groups.append(row("Not standard", [t for t in res if not t.get("std")]))
    groups += [row(n, [t for t in res if session(t["ts"]) == n]) for n in
               ("Asia (00-07 UTC)", "London (07-13 UTC)", "New York (13-21 UTC)", "Late (21-24 UTC)")]
    return [g for g in groups if g]


def counts(trades):
    return {s: sum(1 for t in trades if t["state"] == s) for s in ("pending", "open", "expired")}


def summary_text(trades):
    c = counts(trades)
    g = stats(trades)
    a = f"Paper trades: {len(trades)} tracked | pending {c['pending']} | open {c['open']} | expired {c['expired']}\n"
    if g:
        _, n, w, wr, avg = g[0]
        a += f"Closed {n}: {w} wins / {n - w} losses = {wr}% win rate, avg {avg}R per trade"
    else:
        a += "No closed trades yet - results build up as setups play out."
    return a


def html_block(trades):
    g = stats(trades)
    c = counts(trades)
    head = (f"<h2>Paper-trade results</h2><p>{len(trades)} setups tracked &middot; pending {c['pending']} &middot; "
            f"open {c['open']} &middot; closed {len(closed(trades))} &middot; expired {c['expired']}</p>")
    if g:
        tbl = "<table><tr><th>Group<th>Closed<th>Wins<th>Win rate<th>Avg R</tr>" + "".join(
            f"<tr><td>{n}<td>{t}<td>{w}<td><b>{wr}%</b><td>{a}</tr>" for n, t, w, wr, a in g) + "</table>"
    else:
        tbl = "<p>No closed trades yet. Results build up as setups play out.</p>"
    rec = sorted(trades, key=lambda t: -t["ts"])[:30]
    lst = "<table><tr><th>Added<th>Coin<th>TF<th>Side<th>Score<th>Entry<th>State<th>R</tr>" + "".join(
        f"<tr><td>{t['added']}<td>{t['symbol']}<td>{t['tf']}<td>{'BUY' if t['side'] == 'long' else 'SELL'}<td>{t['score']}"
        f"<td>{t['entry']:.6g}<td class={t['state']}>{t['state']}<td>{t.get('r', '')}</tr>" for t in rec) + "</table>"
    return f"<div class=perf>{head}{tbl}<details><summary>Recent trades</summary>{lst}</details></div>"
