(function () {
  const $ = id => document.getElementById(id);
  const TFMS = {'1h': 3600000, '4h': 14400000};
  const CLOSED = ['win', 'loss', 'timeout', 'manual'];
  let sel = null, D = null, baseScan = null, seenFeed = null, onlyStd = true;

  const f = v => v == null ? '' : (v < 1 ? Number(v).toPrecision(5) : Number(v).toLocaleString(undefined, {maximumFractionDigits: 4}));
  const usd = v => (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  const cls = v => v > 0 ? 'win' : v < 0 ? 'loss' : '';
  const when = ms => ms ? new Date(ms).toLocaleString([], {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'}) : '';
  const side = s => s === 'long' ? 'BUY' : 'SELL';
  const label = t => t.state === 'win' ? 'WIN' : t.state === 'loss' ? 'LOSS (stop hit)' :
    t.state === 'manual' ? (t.r > 0 ? 'WIN (closed by you)' : 'LOSS (closed by you)') :
    t.state === 'timeout' ? (t.r > 0 ? 'WIN (timeout)' : 'LOSS (timeout)') : t.state === 'open' ? 'OPEN' : t.state;

  function skeleton() {
    $('live').innerHTML = '<div id=banner></div><div id=feed></div><div id=tbl></div><div id=detail></div>';
    $('live').addEventListener('click', async e => {
      const c = e.target.closest('[data-close]');
      if (c) {
        e.stopPropagation();
        if (confirm(c.dataset.open === '1' ? 'Close this trade now at the current price?' : 'Cancel this unfilled order?')) {
          await fetch('/api/close?key=' + encodeURIComponent(c.dataset.close), {method: 'POST'});
          tick();
        }
        return;
      }
      if (e.target.closest('[data-x]')) { sel = null; $('detail').innerHTML = ''; return; }
      if (e.target.closest('[data-std]')) { onlyStd = !onlyStd; render(); return; }
      if (e.target.closest('[data-std]')) { onlyStd = !onlyStd; render(); return; }
      const r = e.target.closest('[data-k]');
      if (r) { sel = r.dataset.k; drawDetail(); $('detail').scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
    });
  }

  function row(t, extra) {
    const btn = t.state === 'open' ? `<button data-close="${t.key}" data-open=1>Close now</button>` :
      t.state === 'pending' ? `<button data-close="${t.key}" data-open=0>Cancel</button>` : '';
    return `<tr class=click data-k="${t.key}"><td>${when(t.ts)}<td><b>${t.symbol}</b>${t.std ? ' <span class=tier>STD</span>' : ''}<td>${t.tf}<td>${side(t.side)}<td>${t.lev ? t.lev + 'x' : ''}` +
      `<td>${f(t.entry)}<td>${f(t.price)}<td class=loss>${f(t.sl)}<td class=win>${f(t.tp1)}` + (extra || '') +
      `<td class="${cls(t.r)}">${t.r == null ? '' : t.r + 'R'}<td class="${cls(t.r)}">${t.usd == null ? '' : usd(t.usd)}` +
      `<td><b class="${cls(t.r)}">${label(t)}</b><td>${btn}</tr>`;
  }
  const HEAD = '<tr><th>Setup seen<th>Coin<th>TF<th>Side<th>Max lev<th>Entry<th>Price now<th>Stop loss<th>Target (TP1)';

  function render() {
    const vis = D.trades.filter(t => !onlyStd || t.std), cl0 = vis.filter(t => CLOSED.includes(t.state));
    const rz = cl0.reduce((a, t) => a + (t.usd || 0), 0), ur = vis.filter(t => t.state === 'open').reduce((a, t) => a + (t.usd || 0), 0), nw = cl0.filter(t => t.r > 0).length;
    const r = Object.assign({}, D, {trades: vis, realized_usd: rz, unrealized_usd: ur, closed: cl0.length, wins: nw, winrate: cl0.length ? Math.round(100 * nw / cl0.length) : null});
    const total = rz + ur;
    const open = r.trades.filter(t => t.state === 'open').sort((a, b) => b.ts - a.ts);
    const allpend = r.trades.filter(t => t.state === 'pending').sort((a, b) => b.ts - a.ts);
    const pend = allpend.filter(t => !t.stale), ran = allpend.filter(t => t.stale);
    const done = r.trades.filter(t => CLOSED.includes(t.state)).sort((a, b) => b.close_ts - a.close_ts).slice(0, 25);
    const end = '<th>P/L (R)<th>P/L ($)<th>State<th></tr>';
    const sess = r.sessions.length ? '<details><summary>Win rate by session (closed trades)</summary><table><tr><th>Session<th>Closed<th>Wins<th>Win rate<th>Avg R</tr>' +
      r.sessions.map(s => `<tr><td>${s.name}<td>${s.closed}<td>${s.wins}<td><b>${s.winrate}%</b><td>${s.avg_r}</tr>`).join('') + '</table></details>' : '';
    $('tbl').innerHTML =
      `<h2>Live paper trades <small style="font-weight:400;color:#8b949e">(1R = $${r.risk_usd} risk, futures, fees included, refreshes every 10s, click a row to see its chart; <button data-std=1>${onlyStd ? 'Showing STANDARD only. Click to show all tracked setups' : 'Showing everything. Click for STANDARD only'}</button> <span class=tier>STD</span> = STANDARD v1 setup: 4h buy at a support level with the daily trend up. Only these send alerts. Everything else is tracked for comparison)</small></h2>` +
      `<div class=note style="margin:6px 0"><b>Scanner is running.</b> Last scan ${Math.max(0, Math.round((D.ts - D.scan_ts) / 60000))} min ago (scans every 10 min) &middot; ${D.trades.length} paper trades tracked in total: ${D.trades.filter(t => t.state === 'open').length} open, ${D.trades.filter(t => CLOSED.includes(t.state)).length} closed, ${D.trades.filter(t => t.state === 'pending').length} waiting to fill.` +
        (D.trades.filter(t => t.std && t.state === 'pending' && !t.stale).length ? `<br><b>STANDARD setups waiting for a pullback:</b> ` + D.trades.filter(t => t.std && t.state === 'pending' && !t.stale).map(t => `${t.symbol} (limit ${f(t.entry)}, price needs to drop ${Math.abs(t.away_pct)}%)`).join(' &middot; ') : `<br>No STANDARD setup is waiting right now. They are rare (about 2 a week), so the STANDARD-only view can stay empty for days.`) + `</div>` +
      `<p>Total <b class="${cls(total)}">${usd(total)}</b> &middot; realized <b class="${cls(r.realized_usd)}">${usd(r.realized_usd)}</b> &middot; ` +
      `open now <b class="${cls(r.unrealized_usd)}">${usd(r.unrealized_usd)}</b> &middot; closed ${r.closed} &middot; ` +
      `<b class=win>${r.wins} wins</b> / <b class=loss>${r.closed - r.wins} losses</b>${r.winrate == null ? '' : ' &middot; win rate <b>' + r.winrate + '%</b>'}` +
      ` &middot; waiting to fill ${pend.length} &middot; ${r.scanning ? 'scanning now...' : 'auto-scan every 10 min'}${r.scan_error ? ' <b class=loss>' + r.scan_error + '</b>' : ''}</p>` + sess +
      `<h3>Open positions (${open.length})</h3>` + (open.length ? `<table>${HEAD}${end}${open.map(t => row(t)).join('')}</table>` : '<p>None right now.</p>') +
      `<h3>Recently closed</h3>` + (done.length ? `<table>${HEAD}${end}${done.map(t => row(t)).join('')}</table>` : '<p>None yet.</p>') +
      `<details><summary>Waiting to fill (${pend.length})</summary><table>${HEAD}<th>Away from entry${end}` +
      pend.map(t => row(t, `<td>${t.away_pct}%`)).join('') + '</table></details>' +
      `<details><summary>Ran away without filling, unlikely to fill (${ran.length})</summary><table>${HEAD}<th>Away from entry${end}` +
      ran.map(t => row(t, `<td>${t.away_pct}%`)).join('') + '</table></details>';
    if (baseScan == null) baseScan = r.scan_ts;
    if (r.scan_ts > baseScan && !sel && !window.__rl) { window.__rl = setTimeout(() => location.reload(), 8000); }
    $('banner').innerHTML = r.scan_ts > baseScan ? '<div class=note>A new scan finished. <a href="" onclick="location.reload();return false">Reload</a> to see the latest setup cards below.</div>' : '';
  }

  async function feed() {
    try {
      const fd = await (await fetch('/api/feed', {cache: 'no-store'})).json();
      const keys = new Set(fd.map(x => x.key));
      if (seenFeed === null) seenFeed = keys;
      else {
        const fresh = fd.filter(x => !seenFeed.has(x.key));
        if (fresh.length) {
          try {
            if (window.Notification && Notification.permission === 'granted')
              fresh.forEach(x => new Notification(`${side(x.side)} ${x.symbol} ${x.tf} in entry zone`, {body: `entry ${f(x.entry)} stop ${f(x.sl)} TP1 ${f(x.tp1)}`}));
          } catch (e) {}
          seenFeed = keys;
        }
      }
      $('feed').innerHTML = '<h2>Entry alerts <small style="font-weight:400;color:#8b949e">(STANDARD setups only: the ones worth taking, newest first, click to view)</small></h2>' +
        (fd.length ? '<table><tr><th>Time<th>Coin<th>TF<th>Side<th>Entry<th>Stop<th>TP1<th>R:R<th>Max lev<th>Score</tr>' +
          fd.slice(0, 12).map(x => `<tr class=click data-k="${x.key}"><td>${when(x.ms)}<td><b>${x.symbol}</b><td>${x.tf}<td><b class="${x.side === 'long' ? 'win' : 'loss'}">${side(x.side)}</b><td>${f(x.entry)}<td class=loss>${f(x.sl)}<td class=win>${f(x.tp1)}<td>${x.rr}<td>${x.lev || ''}x<td>${x.score}</tr>`).join('') + '</table>' :
          '<p>No STANDARD alert yet. An alert appears here when a STANDARD setup reaches its entry zone. They are selective (about 2 a week in the backtest), so this can stay empty for days.</p>') +
        (window.Notification && Notification.permission === 'default' ? '<p><button onclick="Notification.requestPermission()">Enable desktop notifications</button></p>' : '');
    } catch (e) {}
  }

  async function drawDetail() {
    if (!sel || !D) return;
    const t = D.trades.find(x => x.key === sel);
    if (!t) { $('detail').innerHTML = '<p>That trade is no longer in the log.</p>'; return; }
    const ms = TFMS[t.tf];
    let c;
    try {
      const from = t.ann ? Math.min(t.ann.ref_ts, t.ann.sweep_ts) - 12 * ms : t.ts - 40 * ms;
      c = await (await fetch(`/api/candles?symbol=${t.symbol}&tf=${t.tf}&from=${from}`, {cache: 'no-store'})).json();
    } catch (e) { return; }
    if (!Array.isArray(c) || !c.length) return;
    const info = `<b>${t.symbol}/USDT ${t.tf} ${side(t.side)}</b> &middot; setup seen ${when(t.ts)}` +
      (t.fill_ts ? ` &middot; entered ${when(t.fill_ts)}` : '') + (CLOSED.includes(t.state) ? ` &middot; closed ${when(t.close_ts)}` : '') +
      ` &middot; <b class="${cls(t.r)}">${label(t)}${t.r != null ? ' ' + t.r + 'R (' + usd(t.usd) + ')' : ''}</b>`;
    $('detail').innerHTML = `<h3>${info} <button data-x=1>close chart</button></h3><canvas id=cv></canvas>` +
      '<p class=legend>Yellow box = entry zone &middot; blue = entry &middot; red = stop &middot; green dashed = TP1 &middot; dotted grey = swept liquidity &middot; purple = market structure shift &middot; grey vertical = when the setup was seen &middot; triangle = trade entered</p>';
    paint($('cv'), c, t, ms);
  }

  function paint(cv, c, t, ms) {
    const W = cv.clientWidth || 900, H = 420, dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + 'px';
    const g = cv.getContext('2d'); g.scale(dpr, dpr);
    const L = 8, R = 78, T = 12, B = 26, pw = W - L - R, ph = H - T - B;
    let lo = Infinity, hi = -Infinity;
    c.forEach(k => { lo = Math.min(lo, k[3]); hi = Math.max(hi, k[2]); });
    [t.entry, t.sl, t.tp1].forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
    if (t.zone) { lo = Math.min(lo, t.zone[0]); hi = Math.max(hi, t.zone[1]); }
    if (t.ann) { lo = Math.min(lo, t.ann.lvl, t.ann.refp); hi = Math.max(hi, t.ann.lvl, t.ann.refp); }
    const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    const n = c.length, step = pw / (n + 2), y = v => T + ph * (1 - (v - lo) / (hi - lo));
    const x = i => L + step * (i + 0.5);
    g.fillStyle = '#0e1116'; g.fillRect(0, 0, W, H);
    g.strokeStyle = '#21262d'; g.fillStyle = '#8b949e'; g.font = '11px system-ui'; g.lineWidth = 1;
    for (let i = 0; i <= 5; i++) { const v = lo + (hi - lo) * i / 5, yy = y(v); g.beginPath(); g.moveTo(L, yy); g.lineTo(W - R, yy); g.stroke(); g.fillText(f(+v.toPrecision(5)), W - R + 6, yy + 4); }
    const si = Math.max(0, c.findIndex(k => k[0] + ms > t.ts));
    const A = t.ann, ix = ts => c.findIndex(k => k[0] >= ts);
    const zi = A ? Math.max(0, ix(A.zone_ts)) : si;
    if (t.zone) { g.fillStyle = 'rgba(240,185,11,.22)'; g.fillRect(x(zi), y(t.zone[1]), W - R - x(zi), y(t.zone[0]) - y(t.zone[1])); g.fillStyle = '#f0b90b'; g.font = '11px system-ui'; g.fillText(A ? A.kind : 'zone', x(zi) + 3, y(t.zone[1]) - 3); }
    g.setLineDash([4, 4]); g.strokeStyle = '#8b949e'; g.beginPath(); g.moveTo(x(si) - step / 2, T); g.lineTo(x(si) - step / 2, H - B); g.stroke(); g.setLineDash([]);
    g.fillStyle = '#8b949e'; g.fillText('setup seen', x(si) - step / 2 + 4, T + 10);
    if (A) {
      const sj = ix(A.sweep_ts), rj = ix(A.ref_ts), mj = ix(A.mss_ts), long = t.side === 'long';
      if (sj >= 0) {
        g.strokeStyle = '#8b949e'; g.lineWidth = 1.2; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(x(Math.max(0, sj - 25)), y(A.lvl)); g.lineTo(x(sj) + step, y(A.lvl)); g.stroke(); g.setLineDash([]);
        const py = long ? y(c[sj][3]) + 5 : y(c[sj][2]) - 5, dy = long ? 7 : -7;
        g.fillStyle = '#58a6ff'; g.font = '11px system-ui'; g.fillText('liquidity sweep', x(sj) - 32, long ? py + 20 : py - 12);
        g.beginPath(); g.moveTo(x(sj), py); g.lineTo(x(sj) - 4, py + dy); g.lineTo(x(sj) + 4, py + dy); g.fill();
      }
      if (rj >= 0 && mj >= 0) {
        g.strokeStyle = '#a371f7'; g.lineWidth = 1.2; g.setLineDash([5, 3]); g.beginPath(); g.moveTo(x(rj), y(A.refp)); g.lineTo(x(mj), y(A.refp)); g.stroke(); g.setLineDash([]);
        g.fillStyle = '#a371f7'; g.fillText('MSS', x(mj) + 3, y(A.refp) + (long ? -4 : 12));
      }
    }
    const line = (v, col, dash, txt) => { g.strokeStyle = col; g.lineWidth = 1.4; g.setLineDash(dash); g.beginPath(); g.moveTo(L, y(v)); g.lineTo(W - R, y(v)); g.stroke(); g.setLineDash([]); g.fillStyle = col; g.font = 'bold 11px system-ui'; g.fillText(txt + ' ' + f(+v.toPrecision(5)), W - R + 6, y(v) - 3); };
    line(t.tp1, '#26a69a', [6, 4], 'TP1'); line(t.entry, '#58a6ff', [], 'ENTRY'); line(t.sl, '#ef5350', [], 'STOP');
    c.forEach((k, i) => {
      const up = k[4] >= k[1], col = up ? '#26a69a' : '#ef5350';
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x(i), y(k[2])); g.lineTo(x(i), y(k[3])); g.stroke();
      const top = y(Math.max(k[1], k[4])), h = Math.max(1, Math.abs(y(k[1]) - y(k[4])));
      g.fillRect(x(i) - step * 0.35, top, step * 0.7, h);
    });
    if (t.fill_ts) {
      const fi = c.findIndex(k => k[0] >= t.fill_ts);
      if (fi >= 0) { g.fillStyle = '#f0b90b'; g.beginPath(); g.moveTo(x(fi), y(t.entry) - 2); g.lineTo(x(fi) - 6, y(t.entry) + 10); g.lineTo(x(fi) + 6, y(t.entry) + 10); g.fill(); }
    }
    {
      const fi2 = t.fill_ts ? c.findIndex(k => k[0] >= t.fill_ts) : -1, x0 = fi2 >= 0 ? x(fi2) : x(n - 1), x1 = W - R - 24;
      const y0 = y(t.entry), y1 = y(t.tp1), col = '#3fb950';
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 2; g.setLineDash([7, 5]);
      g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke(); g.setLineDash([]);
      const ang = Math.atan2(y1 - y0, x1 - x0);
      g.beginPath(); g.moveTo(x1, y1); g.lineTo(x1 - 11 * Math.cos(ang - 0.4), y1 - 11 * Math.sin(ang - 0.4)); g.lineTo(x1 - 11 * Math.cos(ang + 0.4), y1 - 11 * Math.sin(ang + 0.4)); g.fill();
      g.font = 'bold 11px system-ui'; g.fillText(fi2 >= 0 ? 'entered here, heading to TP1' : 'planned path to TP1', Math.min(x0 + 8, x1 - 150), (y0 + y1) / 2 + (t.side === 'long' ? 14 : -8));
    }
    const last = c[n - 1][4];
    g.strokeStyle = '#c9d1d9'; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(L, y(last)); g.lineTo(W - R, y(last)); g.stroke(); g.setLineDash([]);
    g.fillStyle = '#c9d1d9'; g.font = 'bold 11px system-ui'; g.fillText('NOW ' + f(+last.toPrecision(5)), W - R + 6, y(last) + 4);
    g.fillStyle = '#8b949e'; g.font = '11px system-ui';
    const every = Math.max(1, Math.floor(n / 7));
    for (let i = 0; i < n; i += every) g.fillText(new Date(c[i][0]).toLocaleString([], {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'}), x(i) - 28, H - 8);
  }

  async function tick() {
    try {
      D = await (await fetch('/api/live', {cache: 'no-store'})).json();
      render();
      if (sel) drawDetail();
    } catch (e) {}
  }

  const st = document.createElement('style');
  st.textContent = '.click{cursor:pointer}.click:hover{background:#1c2330}#cv{width:100%;display:block;border:1px solid #30363d;border-radius:6px}' +
    'button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:2px 8px;cursor:pointer}button:hover{background:#30363d}' +
    '.note{background:#1f2a44;border:1px solid #58a6ff;border-radius:6px;padding:8px 12px;margin-bottom:10px}.legend{color:#8b949e;font-size:12px}.tier{background:#3d3200;color:#f0b90b;border-radius:3px;padding:0 5px;font-size:11px;font-weight:700}a{color:#58a6ff}details{margin:8px 0}h3{margin:14px 0 6px}';
  document.head.appendChild(st);
  skeleton();
  tick(); feed();
  setInterval(tick, 10000); setInterval(feed, 20000);
})();
