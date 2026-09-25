"""Hosted forex board: pull the economic calendar + 1-minute bars once, run the first-10-minutes paper engine, write state/sniper/history JSON.
The live sniper (seconds-level timing) needs a running PC and is not part of the hosted board."""
import os, sys, json, time, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forex_server as FS


def build(dst):
    res = {}
    try:
        raw = FS.tv_calendar()
        evs = []
        for e in raw:
            cur = FS.CUR.get(e["country"])
            if not cur:
                continue
            evs.append(dict(id=e["id"], key=f"{e['country']}|{e['title']}", title=e["title"], country=e["country"], cur=cur,
                            T=int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp() * 1000),
                            actual=e.get("actual"), forecast=e.get("forecast"), previous=e.get("previous"), unit=e.get("unit") or "", period=e.get("period") or "", latency_s=None))
        FS.S["events"] = sorted(evs, key=lambda x: x["T"])
        FS.S["cal_ts"] = time.time()
        res["calendar"] = f"ok ({len(evs)} events)"
    except Exception as ex:
        res["calendar"] = "error: " + str(ex)[:100]
    nb = 0
    for pair in FS.YAHOO:
        try:
            FS.S["bars"][pair] = FS.bars(pair, "7d")
            nb += 1
        except Exception as ex:
            FS.S["err"][pair] = str(ex)[:60]
        time.sleep(0.5)
    res["bars"] = f"{nb}/{len(FS.YAHOO)} pairs"
    try:
        FS.engine()
    except Exception as ex:
        res["engine"] = "error: " + str(ex)[:100]
    os.makedirs(dst, exist_ok=True)
    def w(name, obj):
        with open(os.path.join(dst, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)
    try:
        w("state.json", FS.state())
        w("sniper.json", FS.sniper_summary())
        hist = {}
        for k, p in FS.PLAY.items():
            p = dict(p)
            p["pol"] = FS.pol_of(k, k.split("|", 1)[-1])[0]
            hist[k] = p
        w("history.json", hist)
        res["state"] = "ok"
    except Exception as ex:
        res["state"] = "error: " + str(ex)[:100]
    return res
