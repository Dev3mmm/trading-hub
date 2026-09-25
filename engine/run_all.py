"""Hosted update job (GitHub Actions). One run: restore state from the last deploy, update every tracker, rebuild the static site, write it to PUBLISH_DIR.
Everything is replayed from candles, so a delayed or skipped run only delays the numbers, it never loses a fill."""
import os, sys, json, time, shutil, traceback, re

os.environ["HOSTED"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRY, FX = os.path.join(ROOT, "engine", "crypto"), os.path.join(ROOT, "engine", "forex")
PUB = os.environ.get("PUBLISH_DIR", os.path.join(ROOT, "publish"))
sys.path.insert(0, CRY)
os.chdir(CRY)

STATE_CRY = ["trades.json", "feed.json", "flip_cfg.json", "setups.json", "trend_state.json", "trend_log.json", "bots_state.json", "scalp_book.json", "scalp_book.json.bak",
             "hosted_meta.json", "dashboard.html", "setups.pine", "watchlist_tradingview.txt"]
STATE_FX = ["forex_trades.json"]
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


def restore():
    st = os.path.join(PUB, "state")
    for d, names in ((CRY, STATE_CRY), (FX, STATE_FX)):
        for n in names:
            src = os.path.join(st, n)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(d, n))
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
    # ---- publish
    data = os.path.join(PUB, "data")
    os.makedirs(data, exist_ok=True)
    import build_static
    step("build_static", lambda: build_static.build(data))
    sys.path.insert(0, FX)
    import forex_build
    step("forex", lambda: forex_build.build(data))
    step("assemble", assemble)
    st = os.path.join(PUB, "state")
    os.makedirs(st, exist_ok=True)
    for d, names in ((CRY, STATE_CRY), (FX, STATE_FX)):
        for n in names:
            if os.path.exists(os.path.join(d, n)):
                shutil.copyfile(os.path.join(d, n), os.path.join(st, n))
    report["_total_secs"] = round(time.time() - t_start, 1)
    report["_ts"] = int(time.time() * 1000)
    json.dump(report, open(os.path.join(data, "status.json"), "w"), indent=1)
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
    fxt = fxt.replace("(live bid/ask from Binance, no real orders)", "(needs the PC running: not part of this hosted copy, so no live results here)")
    open(os.path.join(PUB, "forex.html"), "w", encoding="utf-8").write(fxt)
    hub = open(os.path.join(PUB, "hub.html"), encoding="utf-8").read()
    hub = hub.replace("not running (start run_daily.bat)", "no data yet").replace("not running (start run_forex.bat)", "no data yet")
    hub = hub.replace('<div class="mut">Everything in one place.', '<div class="mut" id="upd">Everything in one place.', 1)
    extra = ("(async()=>{$('d9').className='dot ok';try{const S=await j('/api/status');const a=Math.round((Date.now()-S._ts)/60000);"
             "$('upd').innerHTML+=` <b>Live copy: refreshed about every 10 minutes (last update ${a} min ago), works with the PC off.</b>`}catch(e){}})();\n")
    hub = hub.replace("</script></body></html>", extra + "</script></body></html>", 1)
    open(os.path.join(PUB, "hub.html"), "w", encoding="utf-8").write(hub)
    shutil.copyfile(os.path.join(PUB, "hub.html"), os.path.join(PUB, "index.html"))
    ch = os.path.join(CRY, "charts")
    dst = os.path.join(PUB, "charts")
    shutil.rmtree(dst, ignore_errors=True)
    if os.path.isdir(ch):
        shutil.copytree(ch, dst)
    open(os.path.join(PUB, ".nojekyll"), "w").write("")
    return f"{n} pages"


if __name__ == "__main__":
    main()
