"""Daily long-only trend scanner (Turtle style) + forward paper tracker.
Rules: buy at the next daily open after a CLOSE above the prior 20-day high; exit at the next open after a CLOSE below the prior 10-day low.
Sizing (paper $10,000): risk 0.5% of equity per trade (entry to the 10-day low), notional capped at 20% of equity, max 10 open positions, fees 0.14% round trip.
Backtest (60 coins, 209 coin-years, survivorship-biased so treat as an upper bound): about +26%/yr, max drawdown -20%, 18% of 12-month windows lost money."""
import os, json, time, threading
import numpy as np, pandas as pd
import scanner
import safeio

OUT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(OUT, "trend_state.json")
LOG = os.path.join(OUT, "trend_log.json")
N, M = 20, 10
RISK, MAXPOS, CAP, EQ0, COST, NEAR = 0.005, 10, 0.20, 10000.0, 0.0014, 0.03
LOCK = threading.RLock()


def daily(sym):
    d = scanner.get(scanner.API + "/klines", symbol=sym + "USDT", interval="1d", limit=260)
    if not d or len(d) < 60:
        return None
    df = pd.DataFrame(d, columns="t o h l c v ct qv n tb tq i".split())
    for c in "ohlc":
        df[c] = df[c].astype(float)
    return df[["t", "o", "h", "l", "c"]].reset_index(drop=True)  # last row = today's forming candle


def replay(df):
    """Walk the closed candles. Returns (open position or None, closed trades). Entry/exit fills are the NEXT candle's open (may be today's forming candle)."""
    hh, xl = df.h.shift().rolling(N).max().values, df.l.shift().rolling(M).min().values
    c = df.c.values
    n = len(df)
    pos, done = None, []
    for i in range(max(N, M) + 1, n):
        if pos is None:
            if c[i] > hh[i]:
                pos = dict(sig=i, i0=i + 1, stop=float(xl[i]))
        elif c[i] < xl[i]:
            pos["i1"] = i + 1
            done.append(pos)
            pos = None
    return pos, done


def run():
    with LOCK:
        coins = scanner.universe(60)
        now = int(time.time() * 1000)
        log = safeio.load_json(LOG, None) or dict(start=now, trades=[])
        rows, fresh, exits, watch = [], [], [], []
        px, data = {}, {}
        for sym in coins:
            df = daily(sym)
            if df is None:
                continue
            data[sym] = df
            closed = df.iloc[:-1].reset_index(drop=True)
            forming = df.iloc[-1]
            n = len(closed)
            px[sym] = float(forming.c)
            pos, done = replay(closed)
            hh_now = float(closed.h.iloc[-N:].max())
            xl_now = float(closed.l.iloc[-M:].min())
            row = dict(sym=sym, price=px[sym], last_close=float(closed.c.iloc[-1]), high20=hh_now, low10=xl_now, status="flat",
                       to_break_pct=round(100 * (hh_now / px[sym] - 1), 2))
            if pos is not None:
                entry = float(forming.o) if pos["i0"] >= n else float(closed.o.iloc[pos["i0"]])
                rf0 = (entry - pos["stop"]) / entry if pos["stop"] < entry else None
                row.update(status="signal" if pos["sig"] == n - 1 else "in", entry=entry, entry_ts=int(df.t.iloc[pos["i0"]]), stop0=pos["stop"], rf0=rf0,
                           pnl_pct=round(100 * (px[sym] / entry - 1), 2), r_now=round((px[sym] / entry - 1) / rf0, 2) if rf0 else None,
                           days=int((now - int(df.t.iloc[pos["i0"]])) / 86400000))
                if pos["sig"] == n - 1:
                    fresh.append(row)
            elif done and done[-1]["i1"] >= n:  # exit signal on the last closed candle
                row.update(status="exit_today")
                exits.append(row)
            elif 0 < row["to_break_pct"] <= 100 * NEAR:
                row["status"] = "watch"
                watch.append(row)
            rows.append(row)
        # ---- forward paper log
        realized = sum(t.get("pnl", 0) for t in log["trades"] if t["state"] == "closed")
        eq = EQ0 + realized
        openn = [t for t in log["trades"] if t["state"] == "open"]
        for r in fresh:
            tid = f"{r['sym']}|{int(r['entry_ts'])}"
            if any(t["id"] == tid for t in log["trades"]) or not r["rf0"] or not (0.005 < r["rf0"] < 0.6):
                continue
            if len(openn) >= MAXPOS:
                log["trades"].append(dict(id=tid, sym=r["sym"], state="skipped", note="max 10 open positions", entry_ts=r["entry_ts"], entry=r["entry"], stop0=r["stop0"], rf=r["rf0"]))
                continue
            notional = min(RISK * eq / r["rf0"], CAP * eq)
            t = dict(id=tid, sym=r["sym"], state="open", entry_ts=r["entry_ts"], entry=r["entry"], stop0=r["stop0"], rf=r["rf0"], notional=notional, units=notional / r["entry"])
            log["trades"].append(t)
            openn.append(t)
        for t in log["trades"]:
            if t["state"] != "open":
                continue
            df = data.get(t["sym"])
            if df is None:
                continue
            closed = df.iloc[:-1].reset_index(drop=True)
            xl = closed.l.shift().rolling(M).min().values
            c = closed.c.values
            i0 = int(np.searchsorted(df.t.values, t["entry_ts"]))
            for i in range(max(i0, M + 1), len(closed)):
                if c[i] < xl[i]:
                    ex = float(df.o.iloc[i + 1])
                    t.update(state="closed", exit=ex, exit_ts=int(df.t.iloc[i + 1]), pnl=t["units"] * (ex - t["entry"]) - t["notional"] * COST)
                    t["r"] = round(t["pnl"] / (t["notional"] * t["rf"]), 2)
                    break
        safeio.save_json(LOG, log, indent=1)
        # ---- stats
        cl = [t for t in log["trades"] if t["state"] == "closed"]
        op = [t for t in log["trades"] if t["state"] == "open"]
        for t in op:
            p = px.get(t["sym"], t["entry"])
            t["price"], t["unreal"] = p, t["units"] * (p - t["entry"]) - t["notional"] * COST
            t["stop_now"] = next((r["low10"] for r in rows if r["sym"] == t["sym"]), None)
            t["r_now"] = round(t["unreal"] / (t["notional"] * t["rf"]), 2)
        realized = sum(t["pnl"] for t in cl)
        unreal = sum(t["unreal"] for t in op)
        stats = dict(equity0=EQ0, realized=round(realized, 2), unrealized=round(unreal, 2), equity=round(EQ0 + realized + unreal, 2), open=len(op), closed=len(cl),
                     wins=sum(1 for t in cl if t["pnl"] > 0), avg_r=round(float(np.mean([t["r"] for t in cl])), 2) if cl else None)
        eqn = EQ0 + realized
        for r in fresh:  # what a real account would do today
            r["suggest_notional"] = round(min(RISK * eqn / r["rf0"], CAP * eqn)) if r["rf0"] else None
            r["suggest_risk"] = round(RISK * eqn)
        state = dict(ts=now, rules=dict(N=N, M=M, risk=RISK, maxpos=MAXPOS, cap=CAP), fresh=fresh, exits=exits, watch=sorted(watch, key=lambda r: r["to_break_pct"]),
                     holding=[r for r in rows if r["status"] == "in"], paper_open=op, paper_closed=cl[-30:][::-1], paper_skipped=[t for t in log["trades"] if t["state"] == "skipped"][-10:],
                     stats=stats, universe=len(rows), log_start=log["start"])
        json.dump(state, open(STATE, "w"))
        return state


if __name__ == "__main__":
    s = run()
    print(len(s["fresh"]), "fresh breakouts;", len(s["exits"]), "exit signals;", len(s["holding"]), "already in trend;", len(s["watch"]), "near breakout")
    for r in s["fresh"]:
        print("  BUY signal:", r["sym"], "price", r["price"], "stop", r["stop0"], "risk %.1f%%" % (100 * r["rf0"]), "suggest notional $", r["suggest_notional"])
    print("paper:", s["stats"])
