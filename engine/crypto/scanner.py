"""ICT/SMC crypto scanner: top-100 Binance USDT pairs -> liquidity sweep + MSS + FVG setups -> charts + dashboard.
Free data only (Binance public API). Not financial advice; research aid."""
import os, json, sys, time, html
from concurrent.futures import ThreadPoolExecutor
import tracker
import requests, pandas as pd, numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

OUT = os.path.dirname(os.path.abspath(__file__))
CH = os.path.join(OUT, "charts")
os.makedirs(CH, exist_ok=True)
import datasrc
API = datasrc.API  # local: Binance USDT-M futures; hosted: Binance spot mirror
MUST = ["BTC", "ETH", "XRP", "SOL", "ZEC"]
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "EURI", "AEUR", "USD1", "XUSD", "BFUSD", "PAXG", "WBTC", "WBETH"}
TFMS = tracker.TFMS
TFS = {"1h": 1, "4h": 4}  # entry timeframes; bias from 1d
N = 3  # swing fractal width
SEND_TELEGRAM = False  # set True to forward setups to Telegram again


def get(url, **p):
    for _ in range(3):
        try:
            r = requests.get(url, params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1)
    return None


def universe(n=100):
    t = get(API + "/ticker/24hr") or []
    if datasrc.HOSTED:
        crypto = {x["symbol"] for x in t if x["symbol"].endswith("USDT")}
    else:
        info = (get(API + "/exchangeInfo") or {}).get("symbols", [])
        crypto = {x["symbol"] for x in info if x.get("underlyingType") == "COIN" and x.get("quoteAsset") == "USDT" and x.get("contractType") == "PERPETUAL"
                  and x.get("status") == "TRADING"}  # drops gold/oil/stock perps
    rows = []
    for x in t:
        s = x["symbol"]
        if s not in crypto:
            continue
        b = s[:-4]
        if b in STABLES or b.endswith(("UP", "DOWN")) or not b.isascii():
            continue
        rows.append((b, float(x["quoteVolume"])))
    rows.sort(key=lambda r: -r[1])
    top = [b for b, _ in rows[:n]]
    for m in MUST:
        if m not in top and any(b == m for b, _ in rows):
            top[-1 - MUST.index(m) % 5] = m
    return top


def klines(sym, tf, limit=300, end=None, drop=True):
    d = get(API + "/klines", symbol=sym + "USDT", interval=tf, limit=limit, **({"endTime": end} if end else {}))
    if not d or len(d) < 120:
        return None
    df = pd.DataFrame(d, columns="t o h l c v ct qv n tb tq i".split())
    df = df.iloc[:-1] if drop else df  # drop still-forming candle
    for c in "ohlcv":
        df[c] = df[c].astype(float)
    df["tms"] = df["t"]
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    return df.reset_index(drop=True)


def atr(df, n=14):
    pc = df.c.shift()
    tr = pd.concat([df.h - df.l, (df.h - pc).abs(), (df.l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def swings(df):
    hi, lo = [], []
    for i in range(N, len(df) - N):
        if df.h[i] == df.h[i - N:i + N + 1].max():
            hi.append(i)
        if df.l[i] == df.l[i - N:i + N + 1].min():
            lo.append(i)
    return hi, lo


def htf_bias(d1):
    e = d1.c.ewm(span=50).mean()
    up = d1.c.iloc[-1] > e.iloc[-1] and e.iloc[-1] > e.iloc[-10]
    dn = d1.c.iloc[-1] < e.iloc[-1] and e.iloc[-1] < e.iloc[-10]
    return "bull" if up else "bear" if dn else "range"


def find_setup(df, bias, side):
    """side 'long' | 'short'. Sweep of opposite liquidity -> close back -> MSS -> FVG/OB entry."""
    a = atr(df)
    hi, lo = swings(df)
    L = len(df)
    look = 14
    lv, opp = (lo, hi) if side == "long" else (hi, lo)
    best = None
    for j in range(L - look, L - 1):
        prior = [i for i in lv if i < j - N and i > j - 80]
        if not prior:
            continue
        for si in prior[::-1][:4]:
            lvl = df.l[si] if side == "long" else df.h[si]
            swept = (df.l[j] < lvl and df.c[j] > lvl) if side == "long" else (df.h[j] > lvl and df.c[j] < lvl)
            if not swept:
                continue
            # untouched liquidity: nothing between si and j pierced it
            seg = df.l[si + 1:j] if side == "long" else df.h[si + 1:j]
            if len(seg) and ((seg < lvl).any() if side == "long" else (seg > lvl).any()):
                continue
            refs = [i for i in opp if i < j and i > si - 20]
            if not refs:
                continue
            ref = refs[-1]
            refp = df.h[ref] if side == "long" else df.l[ref]
            mss = None
            for k in range(j + 1, L):
                if (side == "long" and df.c[k] > refp) or (side == "short" and df.c[k] < refp):
                    mss = k
                    break
            if mss is None:
                continue
            ext = df.l[j:].min() if side == "long" else df.h[j:].max()
            # invalidation: price closed beyond the sweep extreme after MSS
            if side == "long" and (df.c[mss:] < ext).any():
                continue
            if side == "short" and (df.c[mss:] > ext).any():
                continue
            # FVG between sweep and now
            zone, kind = None, None
            for i in range(L - 3, j - 1, -1):
                if side == "long" and df.l[i + 2] > df.h[i]:
                    zone, kind = (df.h[i], df.l[i + 2]), "FVG"
                    zi = i
                    break
                if side == "short" and df.h[i + 2] < df.l[i]:
                    zone, kind = (df.h[i + 2], df.l[i]), "FVG"
                    zi = i
                    break
            if zone is None:  # order block: last opposite candle before the displacement
                for i in range(mss, j - 1, -1):
                    if side == "long" and df.c[i] < df.o[i]:
                        zone, kind, zi = (df.l[i], df.h[i]), "OB", i
                        break
                    if side == "short" and df.c[i] > df.o[i]:
                        zone, kind, zi = (df.l[i], df.h[i]), "OB", i
                        break
            if zone is None:
                continue
            entry = (zone[0] + zone[1]) / 2
            # already mitigated: price touched the entry after the zone formed -> that trade filled and ran without us
            st0 = zi + 3 if kind == "FVG" else mss + 1
            tail = df.iloc[st0:]
            touched = (tail.l <= entry).any() if side == "long" else (tail.h >= entry).any()
            if touched and not (zone[0] <= df.c.iloc[-1] <= zone[1]):
                continue
            buf = 0.15 * a.iloc[-1]
            sl = ext - buf if side == "long" else ext + buf
            cur = df.c.iloc[-1]
            # targets: opposite-side swing liquidity beyond entry
            rng = df.iloc[max(0, L - 80):]
            if side == "long":
                tps = sorted({round(df.h[i], 10) for i in opp if df.h[i] > entry * 1.004})
                far = rng.h.max()
                risk = entry - sl
            else:
                tps = sorted({round(df.l[i], 10) for i in opp if df.l[i] < entry * 0.996}, reverse=True)
                far = rng.l.min()
                risk = sl - entry
            if risk <= 0 or not tps:
                continue
            tp1 = next((t for t in tps if abs(t - entry) / risk >= 1.5), tps[-1])
            tp2 = far if abs(far - entry) > abs(tp1 - entry) else tp1
            rr = abs(tp1 - entry) / risk
            if rr < 1.5:
                continue
            # price already ran through the entry zone and away? require price not beyond TP1
            if (side == "long" and cur >= tp1) or (side == "short" and cur <= tp1):
                continue
            inzone = zone[0] <= cur <= zone[1]
            beyond = (cur < zone[0]) if side == "long" else (cur > zone[1])
            if beyond:  # went through the zone but structure intact
                status = "deep pullback - watch"
            elif inzone:
                status = "IN ZONE now"
            else:
                status = "waiting for retrace"
            away = ((cur - entry) if side == "long" else (entry - cur)) / risk  # >0: price already moved in trade direction
            if away > 3:
                continue  # ran too far, the entry is stale
            if not inzone and not beyond and away > 1.0:
                status = "MOVED AWAY - limit order only"
            mid = (rng.h.max() + rng.l.min()) / 2
            disc = entry < mid if side == "long" else entry > mid
            disp = abs(df.c[mss] - df.o[mss]) > 1.3 * a[mss] if not np.isnan(a[mss]) else False
            score = 40
            score += 15 if (bias == ("bull" if side == "long" else "bear")) else (-10 if bias != "range" else 0)
            score += 15 if disc else 0
            score += 10 if inzone else 0
            score += 10 if rr >= 3 else 0
            score += 5 if disp else 0
            score += 5 if kind == "FVG" else 0
            score -= (L - mss) // 4  # staleness
            score -= 15 if status.startswith("MOVED") else 0
            setup = dict(side=side, score=int(score), status=status, kind=kind, zone=zone, entry=entry, sl=sl,
                         tp1=tp1, tp2=tp2, rr=round(rr, 2), sweep_i=j, lvl=lvl, ref=ref, refp=refp, mss=mss,
                         zi=zi, price=cur, bias=bias, disc=bool(disc), disp=bool(disp))
            if best is None or setup["score"] > best["score"]:
                best = setup
    return best


LIVEJS = open(os.path.join(OUT, "live.js"), encoding="utf-8").read() if os.path.exists(os.path.join(OUT, "live.js")) else ""

PINE = """//@version=5
indicator("Scanner setups __DATE__", overlay=true, max_lines_count=200, max_boxes_count=100, max_labels_count=100)
// Auto-generated by scanner.py. Open a scanned coin (BINANCE:XXXUSDT) and its setups draw automatically.
string data = "__DATA__"
if barstate.islast
    string sym = syminfo.ticker
    for row in str.split(data, ";")
        f = str.split(row, "|")
        if array.size(f) == 10 and array.get(f, 0) == sym
            isLong = array.get(f, 1) == "long"
            z0 = str.tonumber(array.get(f, 3))
            z1 = str.tonumber(array.get(f, 4))
            en = str.tonumber(array.get(f, 5))
            sl = str.tonumber(array.get(f, 6))
            t1 = str.tonumber(array.get(f, 7))
            t2 = str.tonumber(array.get(f, 8))
            c = isLong ? color.green : color.red
            tag = (isLong ? "BUY " : "SELL ") + array.get(f, 2) + " (" + array.get(f, 9) + ")"
            box.new(bar_index - 15, z1, bar_index + 25, z0, bgcolor=color.new(color.yellow, 80), border_color=color.yellow)
            line.new(bar_index - 15, en, bar_index + 25, en, color=color.blue, width=2)
            line.new(bar_index - 15, sl, bar_index + 25, sl, color=color.red, width=2)
            line.new(bar_index - 15, t1, bar_index + 25, t1, color=color.green, style=line.style_dashed)
            line.new(bar_index - 15, t2, bar_index + 25, t2, color=color.green, style=line.style_dotted)
            label.new(bar_index + 25, en, tag + " entry", style=label.style_label_left, color=c, textcolor=color.white)
            label.new(bar_index + 25, sl, "stop", style=label.style_label_left, color=color.red, textcolor=color.white)
            label.new(bar_index + 25, t1, "TP1", style=label.style_label_left, color=color.green, textcolor=color.white)
"""


def telegram(data, perf=""):
    cfg = os.path.join(OUT, "..", "hack_monitor", "config.local.json")
    try:
        c = json.load(open(cfg))
        tok, chat = c["telegramBotToken"], c["telegramChatId"]
    except Exception as e:
        print("telegram skipped:", e)
        return
    base = f"https://api.telegram.org/bot{tok}/"
    act = [d for d in data if "deep" not in d["status"] and "MOVED" not in d["status"]][:6]
    if not act:
        act = data[:3]
    lines = [f"Scanner {time.strftime('%d %b')}: {len(data)} setups. Top actionable:"]
    for d in act:
        lines.append(f"{'BUY' if d['side']=='long' else 'SELL'} {d['symbol']} {d['tf']} | score {d['score']} | R:R {d['rr']} | {d['status']}\n  entry {fmt(d['entry'])} stop {fmt(d['sl'])} TP1 {fmt(d['tp1'])}")
    lines.append(perf)
    lines.append("Not financial advice. Full charts: dashboard.html")
    requests.post(base + "sendMessage", data={"chat_id": chat, "text": "\n".join(lines)}, timeout=20)
    for d in act[:4]:
        with open(os.path.join(CH, d["chart"]), "rb") as f:
            requests.post(base + "sendPhoto", data={"chat_id": chat}, files={"photo": f}, timeout=30)


def annot(df, s):
    """Chart annotations for a setup, as candle open times (ms) so the dashboard can redraw it."""
    return dict(lvl=float(s["lvl"]), sweep_ts=int(df.tms[s["sweep_i"]]), ref_ts=int(df.tms[s["ref"]]), refp=float(s["refp"]),
                mss_ts=int(df.tms[s["mss"]]), zone_ts=int(df.tms[s["zi"]]), kind=s["kind"])


def backfill():
    """Re-detect the setup as it looked when it was first seen, for trades logged before annotations were stored."""
    with tracker.LOCK:
        trades = tracker.load()
        changed = False
        for t in trades:
            if t.get("ann") or t.get("ann_tried"):
                continue
            t["ann_tried"], changed = True, True
            try:
                df = klines(t["symbol"], t["tf"], end=t["ts"] - 1, drop=False)
                d1 = klines(t["symbol"], "1d", end=t["ts"] - 1, drop=False)
                if df is None or d1 is None:
                    continue
                s = find_setup(df, htf_bias(d1), t["side"])
                if s and abs(s["entry"] - t["entry"]) / t["entry"] < 0.002:
                    t["ann"] = annot(df, s)
                    t["zone"] = [float(s["zone"][0]), float(s["zone"][1])]
            except Exception as e:
                print("backfill", t["symbol"], e)
        if changed:
            tracker.save(trades)


def levinfo(entry, sl, risk_usd=100):
    """Position sizing for a fixed $ risk. Max leverage keeps the isolated liquidation price >=1.5x the stop distance
    (liq distance ~ 1/lev - 0.5% maintenance margin). Capped at 20x."""
    sd = abs(entry - sl) / entry
    lev = max(1, min(20, int(1 / (1.5 * sd + 0.005))))
    notional = risk_usd / sd
    return dict(sd=sd, lev=lev, notional=notional, margin=notional / lev, fee_r=0.001 / sd)


def fmt(p):
    return f"{p:.6g}" if p < 1 else f"{p:,.4f}" if p < 100 else f"{p:,.2f}"


def draw(df, s, sym, tf):
    n = 110
    st = max(0, len(df) - n)
    d = df.iloc[st:].reset_index(drop=True)
    off = st
    bg, fg = "#0e1116", "#c9d1d9"
    fig, ax = plt.subplots(figsize=(12, 6.4), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    for i, r in d.iterrows():
        col = "#26a69a" if r.c >= r.o else "#ef5350"
        ax.vlines(i, r.l, r.h, color=col, lw=1)
        ax.add_patch(Rectangle((i - .35, min(r.o, r.c)), .7, max(abs(r.c - r.o), 1e-12), color=col))
    x1 = len(d) + 8
    z0, z1 = s["zone"]
    zs = max(0, s["zi"] - off)
    ax.add_patch(Rectangle((zs, z0), x1 - zs, z1 - z0, color="#f0b90b", alpha=.22))
    ax.text(zs + 1, z1, f" {s['kind']}", color="#f0b90b", va="bottom", fontsize=9)
    j = s["sweep_i"] - off
    ax.hlines(s["lvl"], max(0, j - 25), j + 1, color="#8b949e", ls=":", lw=1.2)
    sd = s["side"] == "long"
    ax.annotate("liquidity sweep", (j, df.l[s["sweep_i"]] if sd else df.h[s["sweep_i"]]),
                xytext=(j, (df.l[s["sweep_i"]] * .985) if sd else df.h[s["sweep_i"]] * 1.015),
                color="#58a6ff", fontsize=9, ha="center", arrowprops=dict(arrowstyle="->", color="#58a6ff"))
    m = s["mss"] - off
    ax.hlines(s["refp"], max(0, s["ref"] - off), m, color="#a371f7", ls="--", lw=1.1)
    ax.text(m, s["refp"], " MSS", color="#a371f7", fontsize=9, va="bottom" if sd else "top")
    for y, c, lab in [(s["entry"], "#58a6ff", "ENTRY"), (s["sl"], "#ef5350", "STOP"), (s["tp1"], "#26a69a", "TP1"),
                      (s["tp2"], "#26a69a", "TP2")]:
        if lab == "TP2" and abs(y - s["tp1"]) < 1e-12:
            continue
        ax.hlines(y, max(0, zs - 5), x1, color=c, lw=1.4, ls="-" if lab in ("ENTRY", "STOP") else "--")
        ax.text(x1 + .5, y, f"{lab} {fmt(y)}", color=c, va="center", fontsize=9, fontweight="bold")
    ax.set_xlim(-1, x1 + 22)
    lo_, hi_ = d.l.min(), d.h.max()
    pad = (hi_ - lo_) * .08
    ax.set_ylim(min(lo_, s["sl"]) - pad, max(hi_, s["tp2"]) + pad)
    step = max(1, len(d) // 8)
    ax.set_xticks(range(0, len(d), step))
    ax.set_xticklabels([d.t[i].strftime("%d %b %H:%M") for i in range(0, len(d), step)], color=fg, fontsize=8)
    ax.tick_params(colors=fg, labelsize=8)
    ax.grid(color="#21262d", lw=.5)
    for sp in ax.spines.values():
        sp.set_color("#30363d")
    ax.yaxis.tick_left()
    title = f"{sym}/USDT  {tf}   {'BUY' if sd else 'SELL'} setup   score {s['score']}   R:R {s['rr']}   {s['status']}"
    ax.set_title(title, color="#3fb950" if sd else "#f85149", fontsize=12, loc="left")
    fig.tight_layout()
    fn = f"{sym}_{tf}_{s['side']}.png"
    fig.savefig(os.path.join(CH, fn), facecolor=bg)
    plt.close(fig)
    return fn


def sr_features(df, s, d1):
    """Support/resistance confluence for a setup: how many pivots sat within 0.35 ATR of the swept level, and whether it lines up with a daily swing."""
    hi, lo = swings(df)
    a = atr(df).values
    tol = 0.35 * a[len(df) - 1]
    lvl, sw = s["lvl"], s["sweep_i"]
    cnt = 0
    for idxs, col in ((hi, df.h.values), (lo, df.l.values)):
        cnt += sum(1 for k in idxs if sw - 250 < k < sw and abs(col[k] - lvl) <= tol)
    dh, dl = swings(d1)
    ad = atr(d1).iloc[-1]
    near = bool(np.isfinite(ad) and (any(abs(d1.h.values[k] - lvl) <= 0.5 * ad for k in dh[-40:]) or any(abs(d1.l.values[k] - lvl) <= 0.5 * ad for k in dl[-40:])))
    return cnt, near


def analyze(sym):
    try:
        d1 = klines(sym, "1d")
        if d1 is None:
            return []
        bias = htf_bias(d1)
        res = []
        for tf in TFS:
            df = klines(sym, tf)
            if df is None:
                continue
            for side in ("long", "short"):
                s = find_setup(df, bias, side)
                if s:
                    s["touches"], s["htf_d"] = sr_features(df, s, d1)
                    s["aligned"] = bias == ("bull" if side == "long" else "bear")
                    e50 = d1.c.ewm(span=50).mean()
                    s["daily"] = dict(close=float(d1.c.iloc[-1]), ema50=float(e50.iloc[-1]), ema50_prev=float(e50.iloc[-10]))
                    # STANDARD v1 (backtest: 4h setups with S/R + daily trend agreeing were positive in both halves of the period)
                    s["std"] = bool(tf == "4h" and side == "long" and s["aligned"] and (s["touches"] >= 2 or s["htf_d"]))
                    res.append((sym, tf, df, s))
        return res
    except Exception as e:
        print("err", sym, e)
        return []


def main():
    top = universe(100)
    print(len(top), "coins:", " ".join(top[:15]), "...")
    with open(os.path.join(OUT, "watchlist_tradingview.txt"), "w") as f:
        f.write(",".join(f"BINANCE:{b}USDT.P" for b in top))
    with ThreadPoolExecutor(8) as ex:
        allr = [x for r in ex.map(analyze, top) for x in r]
    # keep best per coin+side across timeframes, then rank
    allr.sort(key=lambda r: -r[3]["score"])
    seen, keep = set(), []
    for r in allr:
        k = (r[0], r[3]["side"])
        if k in seen:
            continue
        seen.add(k)
        keep.append(r)
    keep = [r for r in keep if r[3]["score"] >= 50][:40]
    cards_std, cards_oth, data = [], [], []
    for sym, tf, df, s in keep:
        fn = draw(df, s, sym, tf)
        sd = s["side"] == "long"
        lv = levinfo(s["entry"], s["sl"])
        atier = ' <span class=tag style="background:#3d3200;color:#f0b90b">STANDARD</span>' if s.get("std") else ""
        why = (f"Swept {'sell-side' if sd else 'buy-side'} liquidity at {fmt(s['lvl'])} and closed back "
               f"{'above' if sd else 'below'} it, then broke structure at {fmt(s['refp'])}. "
               f"Entry at the {s['kind']} ({fmt(s['zone'][0])}-{fmt(s['zone'][1])}). "
               f"1D bias: {s['bias']}. {'In discount' if sd and s['disc'] else 'In premium' if (not sd and s['disc']) else 'Not in ideal half of range'}.")
        data.append(dict(symbol=sym, tf=tf, side=s["side"], score=s["score"], rr=s["rr"], status=s["status"],
                         entry=s["entry"], sl=s["sl"], tp1=s["tp1"], tp2=s["tp2"], chart=fn, why=why, price=s["price"], **levinfo(s["entry"], s["sl"]),
                         ts=int(df.tms.iloc[-1]) + TFMS[tf], ann=annot(df, s), std=bool(s.get("std")), touches=s.get("touches"), htf_d=bool(s.get("htf_d")),
                         zone=[float(s["zone"][0]), float(s["zone"][1])], daily=s.get("daily"), bias=s.get("bias"), kind=s["kind"]))
        (cards_std if s.get("std") else cards_oth).append(f"""<div class=card><div class=hd><b>{sym}/USDT</b> <span class=tf>{tf}</span>
<span class="tag {'buy' if sd else 'sell'}">{'BUY' if sd else 'SELL'}</span>{atier}<span class=sc>score {s['score']}</span>
<span class=st>{html.escape(s['status'])}</span></div>
<div class=nums>Price at scan <b>{fmt(s['price'])}</b> &middot; {'LIMIT ORDER: only fills if price returns to the entry' if s['status'].startswith(('waiting','MOVED')) else 'price is at/through the entry zone'}</div>
<img src="charts/{fn}" loading=lazy>
<div class=nums>Entry <b>{fmt(s['entry'])}</b> &middot; Stop <b>{fmt(s['sl'])}</b> &middot; TP1 <b>{fmt(s['tp1'])}</b> &middot; R:R <b>{s['rr']}</b></div>
<div class=nums>Futures: max <b>{lv['lev']}x</b> leverage (liquidation stays beyond the stop) &middot; $100 risk = <b>${lv['notional']:,.0f}</b> position, <b>${lv['margin']:,.0f}</b> margin &middot; stop is {lv['sd']*100:.1f}% away</div>
<p>{html.escape(why)}</p></div>""")
    cards = cards_std + cards_oth
    cards = cards_std + cards_oth
    json.dump(data, open(os.path.join(OUT, "setups.json"), "w"), indent=1)
    for d in data:
        d['zone']=next(x[3]['zone'] for x in keep if x[0]==d['symbol'] and x[3]['side']==d['side'] and x[1]==d['tf'])
    rows = ";".join(f"{d['symbol']}USDT.P|{d['side']}|{d['tf']}|{d['zone'][0]:.10g}|{d['zone'][1]:.10g}|{d['entry']:.10g}|{d['sl']:.10g}|{d['tp1']:.10g}|{d['tp2']:.10g}|{d['score']}" for d in data)
    open(os.path.join(OUT, "setups.pine"), "w").write(PINE.replace("__DATA__", rows).replace("__DATE__", time.strftime("%Y-%m-%d")))

    trades = tracker.update(data)
    tracker.feed_add(data)
    backfill()
    perf = tracker.html_block(trades)
    page = f"""<!doctype html><meta charset=utf-8><title>Setups</title><style>
body{{background:#0e1116;color:#c9d1d9;font:14px system-ui;margin:0;padding:16px}}h1{{font-size:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}}.card img{{width:100%;border-radius:4px}}
.hd{{display:flex;gap:10px;align-items:center;margin-bottom:6px}}.tf{{color:#8b949e}}.sc{{margin-left:auto}}.st{{color:#f0b90b}}
.tag{{padding:2px 8px;border-radius:4px;font-weight:700}}.buy{{background:#1b4332;color:#3fb950}}.sell{{background:#4a1d1d;color:#f85149}}
.perf{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin:12px 0}}table{{border-collapse:collapse}}td,th{{padding:4px 12px;text-align:left}}
.win{{color:#3fb950}}.loss{{color:#f85149}}.open{{color:#58a6ff}}.pending,.expired{{color:#8b949e}}h2{{margin:0 0 6px}}.nums{{margin:6px 0}}p{{color:#8b949e;margin:4px 0 0}}</style>
<h1>ICT/SMC setups &mdash; {time.strftime('%Y-%m-%d %H:%M')} &mdash; {len(cards_std)} STANDARD of {len(cards)} setups from {len(top)} coins &nbsp;<a href="/hub.html" style="font-size:14px;color:#58a6ff">&larr; hub</a></h1>
<p>Research aid only, not financial advice. Limit-order style entries; stop is beyond the sweep.</p>
<div id=live class=perf><h2>Live paper trades</h2><p id=livetxt>Start the server (run_daily.bat) to see live P/L.</p></div>
{perf}<h2>STANDARD setups <small style="color:#8b949e;font-weight:400">(4h buy, S/R level, daily trend up: the only ones that alert)</small></h2>
<div class=grid>{''.join(cards_std) or '<p>None right now.</p>'}</div>
<details style="margin-top:18px"><summary style="cursor:pointer;color:#8b949e">Not standard: tracked for comparison, the backtests say do not trade these ({len(cards_oth)})</summary><div class=grid style="margin-top:10px;opacity:.75">{''.join(cards_oth)}</div></details>
<script src="live.js"></script>"""
    open(os.path.join(OUT, "dashboard.html"), "w", encoding="utf-8").write(page)
    print(f"{len(cards)} setups -> dashboard.html")
    if SEND_TELEGRAM:
        telegram(data, tracker.summary_text(trades))
    for d in data[:15]:
        print(f"{d['symbol']:8}{d['tf']:4}{d['side']:6}score {d['score']:3} rr {d['rr']:5} {d['status']}")


if __name__ == "__main__":
    main()
