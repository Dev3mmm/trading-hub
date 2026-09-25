"""Live PAPER accounts for the two trend bots (Growth, Steady). Same engine as the backtest (trend_bot.py), replayed from the launch date on the latest CLOSED daily candles.
Nothing here places real orders. Output: bots_state.json (served at /api/bots)."""
import os, json, time, threading
import numpy as np, pandas as pd
import scanner
import trend_bot as TB

OUT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(OUT, "bots_state.json")
LAUNCH = int(pd.Timestamp("2026-09-24").timestamp() * 1000)  # accounts start with the close of 24 Sep 2026 (UTC)
RISK, CAP, MAXPOS, EQ0 = 0.005, 0.20, 10, 10000.0
LOCK = threading.RLock()


def daily(sym):
    d = scanner.get(scanner.API + "/klines", symbol=sym + "USDT", interval="1d", limit=400)
    if not d or len(d) < 120:
        return None, None
    df = pd.DataFrame(d, columns="t o h l c v ct qv n tb tq i".split())
    for c in "ohlc":
        df[c] = df[c].astype(float)
    df["t"] = df["t"].astype("int64")
    df = df[["t", "o", "h", "l", "c"]]
    return df.iloc[:-1].reset_index(drop=True), float(df.c.iloc[-1])  # closed candles, live price


def run():
    with LOCK:
        coins = scanner.universe(60)
        if "BTC" not in coins:
            coins.insert(0, "BTC")
        dfs, live = {}, {}
        for c in coins:
            d, px = daily(c)
            if d is not None:
                dfs[c], live[c] = d, px
        now = int(time.time() * 1000)
        out = dict(ts=now, launch=LAUNCH, universe=len(dfs), btc_uptrend=bool(TB.btc_ok(dfs["BTC"]).get(int(dfs["BTC"].t.iloc[-1]), False)), profiles={})
        for key, prof in TB.PROFILES.items():
            cv, tr, st = TB.simulate(dfs, prof, risk=RISK, cap=CAP, maxpos=MAXPOS, start_ms=LAUNCH, equity0=EQ0, keep_state=True)
            eq = st["eq"]
            prep = {s: TB.prep(dfs[s], prof["n"], prof["m"]) for s in st["pos"]}
            open_rows = []
            for s, p in st["pos"].items():
                px = live.get(s, p["last"])
                unreal = p["units"] * (px - p["last"])
                eq += unreal
                risk_px = p["entry"] - p["stop0"]
                xl_now = float(dfs[s].l.iloc[-prof["m"]:].min())
                open_rows.append(dict(sym=s, entry=p["entry"], entry_t=p["entry_t"], stop0=p["stop0"], price=px, units=p["units"], notional=p["notional"], half_done=p["half"],
                                      target=None if not prof["partial"] else p["entry"] + prof["partial"] * risk_px, be_stop=p["entry"] if (p["half"] and prof["be"]) else None,
                                      exit_level=xl_now, r_now=round((px - p["entry"]) / risk_px, 2) if risk_px else 0, pnl=round(p["pnl"] + p["units"] * (px - p["entry"]), 2)))
            actions = []
            for s in st["exit_flag"]:
                actions.append(dict(kind="SELL ALL", sym=s, note="closed below the 10-day low: sell at the next open"))
            for rf, s, stop0 in sorted(st["pending"]):
                if s in st["pos"]:
                    continue
                if len(st["pos"]) + sum(1 for a in actions if a["kind"] == "BUY") >= MAXPOS:
                    actions.append(dict(kind="SKIP", sym=s, note="breakout, but 10 positions are already open"))
                    continue
                if 0.005 < rf < 0.6:
                    px = live.get(s, 0)
                    actions.append(dict(kind="BUY", sym=s, note=f"broke its 20-day high: buy at the next open, stop reference {stop0:.6g}", stop0=float(stop0), size=round(min(RISK * st["eq"] / rf, CAP * st["eq"])), risk_pct=round(100 * rf, 1)))
            for r in open_rows:
                if prof["partial"] and not r["half_done"]:
                    actions.append(dict(kind="LIMIT SELL HALF", sym=r["sym"], note=f"standing order at {r['target']:.6g} (+1R)", price=r["target"]))
                if r["be_stop"]:
                    actions.append(dict(kind="STOP AT ENTRY", sym=r["sym"], note=f"stop on the rest at {r['be_stop']:.6g} (break-even)", price=r["be_stop"]))
            closed = [dict(sym=t["sym"], entry_t=t["entry_t"], exit_t=t["exit_t"], pnl=round(t["pnl"], 2), r=round(t["r"], 2), entry=t["entry"]) for t in tr[-25:]][::-1]
            out["profiles"][key] = dict(name=prof["name"], equity=round(eq, 2), realized=round(sum(t["pnl"] for t in tr), 2), unrealized=round(eq - st["eq"], 2), open=open_rows, closed=closed, n_closed=len(tr),
                                        wins=sum(1 for t in tr if t["pnl"] > 0), actions=actions, days=len(cv), equity0=EQ0)
        json.dump(out, open(STATE + ".tmp", "w"))
        os.replace(STATE + ".tmp", STATE)
        return out


if __name__ == "__main__":
    o = run()
    for k, v in o["profiles"].items():
        print(k, "equity", v["equity"], "open", len(v["open"]), "closed", v["n_closed"], "actions", [(a["kind"], a["sym"]) for a in v["actions"]][:8])
    print("BTC uptrend filter on:", o["btc_uptrend"])
