"""Write the JSON the static pages read (same content the local server's /api/* endpoints return). Run with HOSTED=1 from run_all.py."""
import os, json, time
import tracker, server, scalp_book

OUT = os.path.dirname(os.path.abspath(__file__))


def write(dst, name, obj):
    os.makedirs(dst, exist_ok=True)
    tmp = os.path.join(dst, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, os.path.join(dst, name))


def build(dst):
    res = {}
    def step(name, fn):
        try:
            write(dst, name + ".json", fn())
            res[name] = "ok"
        except Exception as e:
            res[name] = "error: " + str(e)[:120]
    step("live", server.live)
    step("plan", server.plan)
    step("flip", server.flip)
    step("scalp", scalp_book.view)
    step("bots", lambda: json.load(open(os.path.join(OUT, "bots_state.json"))))
    step("trend", lambda: json.load(open(os.path.join(OUT, "trend_state.json"))))
    def feed():
        keys = {t["key"] for t in tracker.load() if t.get("std")}
        return [x for x in reversed(tracker.feed_load()) if x["key"] in keys or x.get("std")][:40]
    step("feed", feed)
    return res
