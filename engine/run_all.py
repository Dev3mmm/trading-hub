"""Hosted update job. `python engine/run_all.py`         full update (restore state, update every tracker, rebuild site)
                     `python engine/run_all.py --fast`  forex-only refresh (calendar, bars, paper engine, sniper log) + reassemble
State lives in PUBLISH_DIR/state (the last deploy). Everything is replayed from candles, so a delayed run only delays numbers, never loses a fill."""
import os, sys, json, time, shutil, traceback, re

os.environ["HOSTED"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRY, FX = os.path.join(ROOT, "engine", "crypto"), os.path.join(ROOT, "engine", "forex")
PUB = os.environ.get("PUBLISH_DIR", os.path.join(ROOT, "publish"))
sys.path.insert(0, CRY)
os.chdir(CRY)
FAST = "--fast" in sys.argv

STATE_CRY = ["trades.json", "feed.json", "flip_cfg.json", "setups.json", "trend_state.json", "trend_log.json", "bots_state.json", "scalp_book.json", "scalp_book.json.bak",
             "hosted_meta.json", "dashboard.html", "setups.pine", "watchlist_tradingview.txt"]
STATE_FX = ["forex_trades.json", "sniper_log.json"]
PAGES = ["hub.html", "plan.html", "trend.html", "bots.html", "scalp.html", "research.html", "standard.html", "dashboard.html", "live.js", "bots_results.json"]
SCAN_EVERY, SLOW_EVERY = 25 * 60, 25 * 60
report = {}
t_start = time.time()


def step(name, fn):
    t0 = time.time()
    try:
        r = fn()
        report[name] = dict(ok=True, secs=round(time.time() - t0, 1), info=r if isinstance(r, (str, dict)) else None)
    except Exception:
        report[name] = dict(ok=False, secs=round(time.time() - t0, 1), error=traceback.format_exc().splitlines()[-1][:200])
        print(traceback.format_exc())


def restore(only_fx=False):
    st = os.path.join(PUB, "state")
    for d, names in (((CRY, STATE_CRY),) if not only_fx else ()) + ((FX, STATE_FX),):
        for n in names:
            src = os.path.join(st, n)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(d, n))
    tk = os.path.join(PUB, "data", "ticks")  # sniper price paths
    if os.path.isdir(tk):
        os.makedirs(os.path.join(FX, "sniper_ticks"), exist_ok=True)
        for f in os.listdir(tk):
            dst = os.path.join(FX, "sniper_ticks", f)
            if not os.path.exists(dst):
                shutil.copyfile(os.path.join(tk, f), dst)
    if not only_fx:
        ch = os.path.join(PUB, "charts")
        if os.path.isdir(ch):
            shutil.rmtree(os.path.join(CRY, "charts"), ignore_errors=True)
            shutil.copytree(ch, os.path.join(CRY, "charts"))
        os.makedirs(os.path.join(CRY, "charts"), exist_ok=True)


def meta():
    try:
        return json.load(open(os.path.join(CRY, "hosted_meta.json")))
    except Exception:
        return {}


def save_meta(m):
    json.dump(m, open(os.path.join(CRY, "hosted_meta.json"), "w"))


def main():
    data = os.path.join(PUB, "data")
    os.makedirs(data, exist_ok=True)
    if not FAST:
        step("restore", lambda: restore())
        import scalp_book, tracker
        m = meta()
        now = time.time()
        step("scalp_book", lambda: scalp_book.update() and "updated")
        step("tracker_refresh", lambda: tracker.refresh() or "updated")
        if now - m.get("last_scan", 0) > SCAN_EVERY:
            import scanner
            step("scanner", lambda: scanner.main() or "scanned")
            m["last_scan"] = now
        if now - m.get("last_slow", 0) > SLOW_EVERY:
            import trend_scan, bots_live
            step("trend_scan", lambda: trend_scan.run() and "ok")
            step("bots_live", lambda: bots_live.run() and "ok")
            m["last_slow"] = now
        save_meta(m)
        import build_static
        step("build_static", lambda: build_static.build(data))
    else:
        step("restore_fx", lambda: restore(only_fx=True))
    sys.path.insert(0, FX)
    import forex_build
    step("forex", lambda: forex_build.build(data))
    step("assemble", assemble)
    st = os.path.join(PUB, "state")
    os.makedirs(st, exist_ok=True)
    for d, names in (((CRY, STATE_CRY),) if not FAST else ()) + ((FX, STATE_FX),):
        for n in names:
            if os.path.exists(os.path.join(d, n)):
                shutil.copyfile(os.path.join(d, n), os.path.join(st, n))
    tk = os.path.join(FX, "sniper_ticks")
    if os.path.isdir(tk):
        os.makedirs(os.path.join(data, "ticks"), exist_ok=True)
        for f in os.listdir(tk):
            shutil.copyfile(os.path.join(tk, f), os.path.join(data, "ticks", f))
    if not FAST:
        report["_total_secs"] = round(time.time() - t_start, 1)
        report["_ts"] = int(time.time() * 1000)
        json.dump(report, open(os.path.join(data, "status.json"), "w"), indent=1)
    else:  # keep the last full status, just stamp the fast refresh
        try:
            s = json.load(open(os.path.join(data, "status.json")))
        except Exception:
            s = {}
        s["_fast_ts"] = int(time.time() * 1000)
        s["forex"] = report.get("forex", s.get("forex"))
        json.dump(s, open(os.path.join(data, "status.json"), "w"), indent=1)
    print(json.dumps(report, indent=1))


def fix(text, name):
    """Local server uses absolute paths (/x.html, fetch('/api/..')). On GitHub Pages the site lives in a sub-folder, so make everything relative and add the shim."""
    text = re.sub(r'(href|src)="/(?!/)', r'\1="', text)
    text = re.sub(r"fetch\((['`])/", r"fetch(\1", text)
    text = text.replace("http://localhost:8765/", "").replace("http://localhost:8766/api/", "api/").replace('href="http://localhost:8766/"', 'href="forex.html"').replace("http://localhost:8766/", "forex.html")
    text = text.replace("scans every 10 min", "hosted copy scans about every 25 min").replace("auto-scan every 10 min", "auto-scan about every 25 min").replace("refreshes every 10s", "refreshes every 10 min")
    if "<title>" in text and "shim.js" not in text:
        text = text.replace("<title>", '<script src="shim.js"></script><title>', 1)
    return text


def assemble():
    shutil.copyfile(os.path.join(ROOT, "site", "shim.js"), os.path.join(PUB, "shim.js"))
    n = 0
    for p in PAGES:
        src = os.path.join(CRY, p)
        if not os.path.exists(src):
            continue
        if p.endswith(".json"):
            shutil.copyfile(src, os.path.join(PUB, p))
        else:
            open(os.path.join(PUB, p), "w", encoding="utf-8").write(fix(open(src, encoding="utf-8").read(), p))
        n += 1
    fxsrc = os.path.join(FX, "forex.html")
    fxt = fix(open(fxsrc, encoding="utf-8").read(), "forex.html").replace('href="hub.html"', 'href="index.html"')
    fxt = fxt.replace("(live bid/ask from Binance, no real orders)", "(runs on GitHub during releases: live EUR/USD bid/ask from Binance, no real orders; gold is PC-only)")
    open(os.path.join(PUB, "forex.html"), "w", encoding="utf-8").write(fxt)
    hub = open(os.path.join(CRY, "hub.html"), encoding="utf-8").read()
    hub = fix(hub, "hub.html")
    hub = hub.replace("not running (start run_daily.bat)", "no data yet").replace("not running (start run_forex.bat)", "no data yet")
    hub = hub.replace('<div class="mut">Everything in one place.', '<div class="mut" id="upd">Everything in one place.', 1)
    extra = ("(async()=>{$('d9').className='dot ok';try{const S=await j('/api/status');const a=Math.round((Date.now()-S._ts)/60000);"
             "$('upd').innerHTML+=` <b>Live copy: full refresh about every 10 minutes (last ${a} min ago), forex board every 1-2 minutes around releases. Works with the PC off.</b>`}catch(e){}})();\n")
    hub = hub.replace("</script></body></html>", extra + "</script></body></html>", 1)
    open(os.path.join(PUB, "hub.html"), "w", encoding="utf-8").write(hub)
    shutil.copyfile(os.path.join(PUB, "hub.html"), os.path.join(PUB, "index.html"))
    ch = os.path.join(CRY, "charts")
    dst = os.path.join(PUB, "charts")
    if not FAST:
        shutil.rmtree(dst, ignore_errors=True)
        if os.path.isdir(ch):
            shutil.copytree(ch, dst)
    open(os.path.join(PUB, ".nojekyll"), "w").write("")
    return f"{n} pages"


if __name__ == "__main__":
    main()
