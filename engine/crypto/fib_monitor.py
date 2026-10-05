"""Fib OTE live monitor -- OBSERVATION ONLY, no trading, ports strategy_lab/fib_monitor/monitor.py onto the
hosted worker so Martin can check it from a link instead of a local script. Same rule: HTF bias decides
direction, LTF (15m) swing gives the fib, the 0.5-0.68 zone is the entry, we just watch what happens next
(target 0.3, deeper discount 0.382, invalidation at the 1.0 swing extreme) -- and we save the actual chart
so the swing selection can be checked by eye, not trusted blind.
"""
import os, json, time
from datetime import datetime, timezone
import requests, numpy as np, pandas as pd
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import datasrc  # local PC: Binance USDT-M futures; hosted (GitHub Actions): Binance spot mirror (futures API 451s from US cloud IPs)

BASE = datasrc.API  # e.g. ".../fapi/v1" locally, ".../api/v3" hosted -- same kline/ticker shape either way
OUT = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = os.path.join(OUT, 'charts', 'fib')
LOG = os.path.join(OUT, 'fib_signals.json')
N_PIV = 15
HORIZON_HOURS = 8
MAX_LOG = 300  # keep the site light


def get(path, params=None):
    for _ in range(3):
        try:
            return requests.get(BASE + path, params=params or {}, timeout=10).json()
        except Exception:
            time.sleep(1.5)
    raise RuntimeError('api unreachable ' + path)


def klines(sym, interval, limit):
    raw = get('/klines', {'symbol': sym, 'interval': interval, 'limit': limit})
    if not isinstance(raw, list):
        raise RuntimeError(str(raw)[:150])
    raw = raw[:-1]
    df = pd.DataFrame(raw, columns=['t', 'o', 'h', 'l', 'c', 'v', *range(6)])
    for col in ('o', 'h', 'l', 'c'):
        df[col] = df[col].astype(float)
    df['t'] = df['t'].astype(np.int64)
    return df[['t', 'o', 'h', 'l', 'c']].reset_index(drop=True)


def ema(a, span):
    return pd.Series(a).ewm(span=span, adjust=False).mean().values


def htf_bias(sym):
    d = klines(sym, '1d', 80)
    if len(d) < 55: return None
    e20, e50 = ema(d.c.values, 20), ema(d.c.values, 50)
    c = d.c.values[-1]
    if c > e50[-1] and e20[-1] > e50[-1]: return 'bullish'
    if c < e50[-1] and e20[-1] < e50[-1]: return 'bearish'
    return None


def pivots(h, l, n):
    N = len(h); ph = np.zeros(N, bool); pl = np.zeros(N, bool)
    for i in range(n, N - n):
        if h[i] == h[i - n:i + n + 1].max() and h[i] > h[i - n:i].max(): ph[i] = True
        if l[i] == l[i - n:i + n + 1].min() and l[i] < l[i - n:i].min(): pl[i] = True
    return ph, pl


def bullish_leg(h, l, ph, pl, n):
    N = len(h); last_pl_bar, last_pl_val, H, L, h_bar, l_bar = -1, 0.0, None, None, -1, -1
    for i in range(n, N):
        j = i - n
        if j >= n and pl[j]: last_pl_bar, last_pl_val = j, l[j]
        if j >= n and ph[j] and last_pl_bar >= 0 and last_pl_bar < j:
            H, L, h_bar, l_bar = h[j], last_pl_val, j, last_pl_bar
    if H is None or L is None or H <= L: return None
    return dict(dir='LONG', H=H, L=L, h_bar=h_bar, l_bar=l_bar,
                zone_lo=H - 0.68 * (H - L), zone_hi=H - 0.50 * (H - L),
                target=H - 0.30 * (H - L), deep=L + 0.382 * (H - L), stop=L - 0.02 * (H - L))


def bearish_leg(h, l, ph, pl, n):
    N = len(h); last_ph_bar, last_ph_val, H, L, h_bar, l_bar = -1, 0.0, None, None, -1, -1
    for i in range(n, N):
        j = i - n
        if j >= n and ph[j]: last_ph_bar, last_ph_val = j, h[j]
        if j >= n and pl[j] and last_ph_bar >= 0 and last_ph_bar < j:
            L, H, l_bar, h_bar = l[j], last_ph_val, j, last_ph_bar
    if H is None or L is None or H <= L: return None
    return dict(dir='SHORT', H=H, L=L, h_bar=h_bar, l_bar=l_bar,
                zone_lo=L + 0.50 * (H - L), zone_hi=L + 0.68 * (H - L),
                target=L + 0.30 * (H - L), deep=H - 0.382 * (H - L), stop=H + 0.02 * (H - L))


def unmitigated_fvg_overlap(h, l, zone_lo, zone_hi, bearish):
    live = []
    for i in range(2, len(h)):
        if not bearish and l[i] > h[i - 2]: live.append((h[i - 2], l[i]))
        if bearish and h[i] < l[i - 2]: live.append((h[i], l[i - 2]))
        live = [g for g in live if (l[i] > g[0] if not bearish else h[i] < g[1])]
    return any(hi >= zone_lo and lo <= zone_hi for lo, hi in live)


def draw_chart(sym, d, leg, bias, out_path):
    h, l, c, o = d.h.values, d.l.values, d.c.values, d.o.values
    N = len(c); a = max(0, min(leg['l_bar'], leg['h_bar']) - 10); b = N - 1
    fig, ax = plt.subplots(figsize=(13, 7))
    for i in range(a, b + 1):
        up = c[i] >= o[i]; col = '#26a69a' if up else '#ef5350'
        ax.plot([i - a, i - a], [l[i], h[i]], color=col, lw=1)
        ax.add_patch(Rectangle((i - a - 0.35, min(o[i], c[i])), 0.7, max(abs(c[i] - o[i]), 1e-9), color=col))
    ax.axhline(leg['H'], color='white', lw=1); ax.text(0, leg['H'], ' 0 (swing extreme)', color='white', fontsize=8, va='bottom')
    ax.axhline(leg['target'], color='orange', lw=1.2); ax.text(0, leg['target'], ' 0.3 target', color='orange', fontsize=8, va='bottom')
    ax.axhline(leg['deep'], color='yellow', lw=1, ls='--'); ax.text(0, leg['deep'], ' 0.382 (deeper discount)', color='yellow', fontsize=8, va='bottom')
    ax.axhspan(leg['zone_lo'], leg['zone_hi'], color='cyan', alpha=0.15)
    ax.axhline(leg['zone_lo'], color='cyan', lw=1); ax.axhline(leg['zone_hi'], color='cyan', lw=1)
    ax.text(0, (leg['zone_lo'] + leg['zone_hi']) / 2, ' 0.5-0.68 OTE entry zone', color='cyan', fontsize=8, va='center')
    ax.axhline(leg['L'], color='white', lw=1); ax.text(0, leg['L'], ' 1 (swing extreme)', color='white', fontsize=8, va='top')
    ax.axhline(leg['stop'], color='red', lw=1.2, ls='--'); ax.text(0, leg['stop'], ' invalidation', color='red', fontsize=8, va='top')
    ax.axvline(leg['h_bar'] - a, color='grey', ls=':'); ax.axvline(leg['l_bar'] - a, color='grey', ls=':')
    ax.scatter([leg['h_bar'] - a], [leg['H']], color='white', s=60, zorder=5, marker='v')
    ax.scatter([leg['l_bar'] - a], [leg['L']], color='white', s=60, zorder=5, marker='^')
    ax.set_xticks([]); ax.set_facecolor('#0e0e0e'); fig.patch.set_facecolor('#0e0e0e')
    ax.tick_params(colors='white'); ax.spines[:].set_color('#555')
    ax.set_title(f'{sym} 15m -- {leg["dir"]} setup, HTF bias {bias} -- swing points are the white ^/v markers', color='white')
    fig.tight_layout(); fig.savefig(out_path, dpi=85, facecolor='#0e0e0e'); plt.close(fig)


def load_log():
    if not os.path.exists(LOG): return []
    try:
        return json.load(open(LOG, encoding='utf8'))
    except Exception:
        return []


def save_log(rows):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    json.dump(rows[-MAX_LOG:], open(LOG, 'w', encoding='utf8'))


def check_outcomes(rows):
    for r in rows:
        if r.get('status') != 'open': continue
        age_h = (time.time() * 1000 - r['signal_time_ms']) / 3600000
        try:
            d = klines(r['sym'], '15m', min(int(age_h * 4) + 5, 500))
        except Exception:
            continue
        after = d[d.t > r['signal_time_ms']]
        if len(after) == 0: continue
        h, l = after.h.values, after.l.values
        if r['dir'] == 'LONG':
            reached_target = bool((h >= r['target']).any())
            reached_deep = bool((l <= r['deep_0382']).any())
            invalidated = bool((l <= r['stop']).any())
        else:
            reached_target = bool((l <= r['target']).any())
            reached_deep = bool((h >= r['deep_0382']).any())
            invalidated = bool((h >= r['stop']).any())
        r['reached_target_so_far'] = reached_target
        r['reached_deeper_discount_so_far'] = reached_deep
        if invalidated:
            r['status'] = 'INVALIDATED'
        elif reached_target:
            r['status'] = 'REACHED TARGET'
        elif age_h >= HORIZON_HOURS:
            r['status'] = 'TIMED OUT'
    return rows


def run():
    os.makedirs(CHART_DIR, exist_ok=True)
    tickers = get('/ticker/24hr') or []
    if datasrc.HOSTED:
        ok = {t['symbol'] for t in tickers if t['symbol'].endswith('USDT') and t['symbol'].isalnum()}
    else:
        ex = get('/exchangeInfo')
        ok = {s['symbol'] for s in ex['symbols'] if s.get('contractType') == 'PERPETUAL' and s.get('status') == 'TRADING'
              and s.get('quoteAsset') == 'USDT' and s['symbol'].isalnum()}
    tickers = [t for t in tickers if t['symbol'] in ok]
    tickers.sort(key=lambda t: -float(t['quoteVolume']))
    tickers = tickers[:50]  # a bit lighter than the local version, this runs on a shared cloud schedule

    rows = load_log()
    rows = check_outcomes(rows)
    already_open_syms = {r['sym'] for r in rows if r.get('status') == 'open'}
    done_legs = {(r['sym'], r['leg_key']) for r in rows}
    found = 0
    for t in tickers:
        sym = t['symbol']
        if sym in already_open_syms: continue
        try:
            bias = htf_bias(sym)
            if bias is None: continue
            d = klines(sym, '15m', 220)
            if len(d) < 100: continue
            h, l = d.h.values, d.l.values
            ph, pl = pivots(h, l, N_PIV)
            leg = bullish_leg(h, l, ph, pl, N_PIV) if bias == 'bullish' else bearish_leg(h, l, ph, pl, N_PIV)
            if leg is None: continue
            leg_key = f"{leg['dir']}_{leg['h_bar']}_{leg['l_bar']}"
            if (sym, leg_key) in done_legs: continue
            i, prev = len(h) - 1, len(h) - 2
            start = max(leg['h_bar'], leg['l_bar']) + 1
            if leg['dir'] == 'LONG':
                already_invalid = bool((l[start:i + 1] <= leg['stop']).any())
            else:
                already_invalid = bool((h[start:i + 1] >= leg['stop']).any())
            if already_invalid: continue
            touch_now = l[i] <= leg['zone_hi'] and h[i] >= leg['zone_lo']
            touch_prev = l[prev] <= leg['zone_hi'] and h[prev] >= leg['zone_lo']
            if not touch_now or touch_prev: continue
            has_fvg = unmitigated_fvg_overlap(h, l, leg['zone_lo'], leg['zone_hi'], leg['dir'] == 'SHORT')
            img = f"{sym}_{leg['dir']}_{int(time.time())}.png"
            draw_chart(sym, d, leg, bias, os.path.join(CHART_DIR, img))
            rows.append(dict(sym=sym, dir=leg['dir'], htf_bias=bias, has_fvg=has_fvg,
                              signal_time=datetime.now(timezone.utc).isoformat(), signal_time_ms=int(d.t.values[-1]),
                              swing_H=leg['H'], swing_L=leg['L'], zone_lo=leg['zone_lo'], zone_hi=leg['zone_hi'],
                              target=leg['target'], deep_0382=leg['deep'], stop=leg['stop'],
                              leg_key=leg_key, chart='charts/fib/' + img, status='open'))
            found += 1
        except Exception as e:
            print('fib_monitor scan error', sym, e)
    save_log(rows)
    resolved = [r for r in rows if r['status'] != 'open']
    return dict(new_signals=found, open=len(rows) - len(resolved), resolved=len(resolved),
                reached_target=sum(1 for r in resolved if r['status'] == 'REACHED TARGET'),
                invalidated=sum(1 for r in resolved if r['status'] == 'INVALIDATED'),
                timed_out=sum(1 for r in resolved if r['status'] == 'TIMED OUT'))


if __name__ == '__main__':
    print(run())
