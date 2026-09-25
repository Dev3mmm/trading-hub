"""Daily trend bot engine (one engine, two profiles) used by BOTH the backtest and the live paper accounts.

Entry   : a daily CLOSE above the prior N-day high (N=20) -> buy at the NEXT day's open. Only when Bitcoin is in an uptrend (BTC close > its 100-day average, and that average rising over 10 days).
Stop    : the prior M-day low (M=10) at the time of the signal. It sizes the position (risk 0.5% of equity, max 20% of equity per coin, max 10 positions) and is the reference for +1R.
Exit    : a daily CLOSE below the prior M-day low -> sell at the next open.
GROWTH  : hold everything until the exit above.
STEADY  : sell HALF at +1R (limit order), then move the stop on the rest to the entry price (break-even; checked from the next day), and exit the rest on the same 10-day-low rule.
Costs   : 0.14% round trip on every dollar traded. No look-ahead: signals use closed candles only; fills are next open / limit levels."""
import numpy as np, pandas as pd

COST = 0.0014
PROFILES = {
    "growth": dict(key="growth", name="Growth", n=20, m=10, btc=True, partial=None, be=False),
    "steady": dict(key="steady", name="Steady", n=20, m=10, btc=True, partial=1.0, be=True),
}


def prep(df, n, m):
    d = df.copy()
    d["hh"] = d.h.shift().rolling(n).max()  # prior n-day high (excludes today)
    d["xl"] = d.l.shift().rolling(m).min()  # prior m-day low
    return d


def btc_ok(btc, sma=100, slope=10):
    s = btc.c.rolling(sma).mean()
    ok = (btc.c > s) & (s > s.shift(slope))
    return dict(zip(btc.t.values.tolist(), ok.tolist()))


def simulate(dfs, prof, risk=0.005, cap=0.20, maxpos=10, cost=COST, start_ms=None, exclude=(), equity0=10000.0, btc_sma=100, keep_state=False):
    n, m = prof["n"], prof["m"]
    data = {s: prep(d, n, m) for s, d in dfs.items() if s not in exclude}
    ok = btc_ok(dfs["BTC"], btc_sma) if prof["btc"] else None
    arr = {s: dict(t=d.t.values, o=d.o.values, h=d.h.values, l=d.l.values, c=d.c.values, hh=d.hh.values, xl=d.xl.values, idx={int(t): i for i, t in enumerate(d.t.values)}) for s, d in data.items()}
    dates = sorted(set(int(t) for a in arr.values() for t in a["t"]))
    if start_ms is not None:
        dates = [d for d in dates if d >= start_ms]
    eq, pos, pending, curve, trades = equity0, {}, [], [], []
    exit_flag = set()

    def close_leg(sym, p, px, units, day, reason):
        nonlocal eq
        eq += units * (px - p["last"]) - p["entry"] * units * cost
        p["pnl"] += units * (px - p["entry"]) - p["entry"] * units * cost
        p["units"] -= units

    def finish(sym, p, day):
        p["exit_t"] = day
        exit_flag.discard(sym)
        p["r"] = p["pnl"] / (p["notional"] * p["rf"]) if p["notional"] * p["rf"] else 0.0
        trades.append(p)
        del pos[sym]

    for day in dates:
        # 1) exits flagged at yesterday's close -> sell at today's open
        for sym in list(exit_flag):
            a = arr[sym]
            i = a["idx"].get(day)
            if i is None or sym not in pos:
                continue
            p = pos[sym]
            close_leg(sym, p, a["o"][i], p["units"], day, "signal")
            finish(sym, p, day)
            exit_flag.discard(sym)
        # 2) entries decided at yesterday's close -> buy at today's open
        for rf_hint, sym, stop0 in sorted(pending, key=lambda x: x[0]):
            if len(pos) >= maxpos or sym in pos:
                continue
            a = arr[sym]
            i = a["idx"].get(day)
            if i is None:
                continue
            entry = a["o"][i]
            rf = (entry - stop0) / entry
            if not (0.005 < rf < 0.6):
                continue
            notional = min(risk * eq / rf, cap * eq)
            pos[sym] = dict(sym=sym, entry=entry, stop0=stop0, rf=rf, units=notional / entry, units0=notional / entry, notional=notional, last=entry, entry_t=day, pnl=0.0, half=False, be_from=None)
        pending = []
        # 3) intraday management and end-of-day marking
        for sym in list(pos):
            a = arr[sym]
            i = a["idx"].get(day)
            if i is None:
                continue
            p = pos[sym]
            risk_px = p["entry"] - p["stop0"]
            if prof["partial"] and not p["half"]:
                tgt = p["entry"] + prof["partial"] * risk_px
                if a["h"][i] >= tgt:
                    px = max(tgt, a["o"][i]) if a["o"][i] >= tgt else tgt
                    close_leg(sym, p, px, p["units0"] * 0.5, day, "half")
                    p["half"] = True
                    p["last"] = px if False else p["last"]
                    if prof["be"]:
                        p["be_from"] = day + 86400000
            elif p["half"] and prof["be"] and p["be_from"] is not None and day >= p["be_from"]:
                if a["l"][i] <= p["entry"]:
                    px = min(a["o"][i], p["entry"])
                    close_leg(sym, p, px, p["units"], day, "breakeven")
                    finish(sym, p, day)
                    continue
            c = a["c"][i]
            eq += p["units"] * (c - p["last"])
            p["last"] = c
            if not np.isnan(a["xl"][i]) and c < a["xl"][i]:
                exit_flag.add(sym)
        # 4) new signals at today's close for tomorrow's open
        if ok is None or ok.get(day, False):
            for sym, a in arr.items():
                if sym in pos:
                    continue
                i = a["idx"].get(day)
                if i is None or np.isnan(a["hh"][i]) or np.isnan(a["xl"][i]):
                    continue
                if a["c"][i] > a["hh"][i]:
                    rf = (a["c"][i] - a["xl"][i]) / a["c"][i]
                    pending.append((rf, sym, a["xl"][i]))
        curve.append((day, eq, len(pos)))
    if keep_state:
        return curve, trades, dict(pos=pos, pending=pending, exit_flag=set(exit_flag), eq=eq)
    return curve, trades


def stats(curve, trades, label=""):
    days = np.array([d for d, _, _ in curve])
    eqs = np.array([e for _, e, _ in curve])
    yrs = (days[-1] - days[0]) / 86400000 / 365.25
    ret = np.diff(eqs) / eqs[:-1]
    cagr = (eqs[-1] / eqs[0]) ** (1 / yrs) - 1 if eqs[-1] > 0 else -1
    dd = (eqs / np.maximum.accumulate(eqs) - 1)
    pnl = np.array([t["pnl"] for t in trades]) if trades else np.array([0.0])
    w = pnl > 0
    # longest time under water
    under, best = 0, 0
    for v in dd:
        under = under + 1 if v < -1e-9 else 0
        best = max(best, under)
    s = pd.Series(eqs, index=pd.to_datetime(days, unit="ms"))
    mon = s.resample("ME").last().pct_change().dropna()
    yr = s.resample("YE").last().pct_change()
    yr.iloc[0] = s.resample("YE").last().iloc[0] / eqs[0] - 1
    roll = eqs[365:] / eqs[:-365] - 1 if len(eqs) > 400 else np.array([np.nan])
    return dict(label=label, trades=len(trades), win=float(w.mean()), avg_win_loss=float(pnl[w].mean() / abs(pnl[~w].mean())) if (~w).any() and w.any() else None,
                pf=float(pnl[w].sum() / abs(pnl[~w].sum())) if (~w).any() else None, cagr=float(cagr), maxdd=float(dd.min()), calmar=float(cagr / abs(dd.min())) if dd.min() < 0 else None,
                sharpe=float(ret.mean() / ret.std() * np.sqrt(365)) if ret.std() > 0 else None, longest_dd_days=int(best), exposure=float(np.mean([k for _, _, k in curve])),
                pos_months=float((mon > 0).mean()), worst_12m=float(np.nanmin(roll)), best_12m=float(np.nanmax(roll)), losing_12m=float(np.mean(roll < 0)) if len(roll) > 1 else None,
                yearly={str(k.year): float(v) for k, v in yr.items()}, total=float(eqs[-1] / eqs[0] - 1), end_equity=float(eqs[-1]), years=float(yrs))
