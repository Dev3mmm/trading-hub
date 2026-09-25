/* Hosted-mode shim. The dashboards were written for a local server (/api/...). On GitHub Pages there is no server, so:
   - /api/<name>          -> data/<name>.json (refreshed by the scheduled job)
   - /api/candles         -> Binance spot public mirror, straight from the browser
   - /api/trend_candles   -> same, daily candles
   - /api/history?key=    -> looked up in data/history.json
   - /api/sniper_ticks    -> empty (the live sniper needs a running PC)
   - POST /api/close      -> refused (hosted paper trades are managed by the job) */
(function () {
  var _f = window.fetch.bind(window);
  var SPOT = 'https://data-api.binance.vision/api/v3/klines';
  function json(o, code) { return Promise.resolve(new Response(JSON.stringify(o), { status: code || 200, headers: { 'Content-Type': 'application/json' } })); }
  function kl(sym, tf, from, limit) {
    var u = SPOT + '?symbol=' + sym + 'USDT&interval=' + tf + '&limit=' + limit + (from ? '&startTime=' + Math.floor(from) : '');
    return _f(u).then(function (r) { return r.json(); }).then(function (d) {
      return json(Array.isArray(d) ? d.map(function (x) { return [x[0], +x[1], +x[2], +x[3], +x[4]]; }) : []);
    });
  }
  window.fetch = function (input, opts) {
    var s = typeof input === 'string' ? input : (input && input.url) || '';
    try { s = s.replace(location.origin, ''); } catch (e) {}
    var m = s.match(/^\/?api\/([a-z_]+)(?:\?(.*))?$/);
    if (!m) return _f(input, opts);
    var name = m[1], q = new URLSearchParams(m[2] || '');
    if (name === 'candles') return kl(q.get('symbol'), q.get('tf'), +q.get('from'), 500);
    if (name === 'trend_candles') return kl(q.get('symbol'), '1d', 0, 170);
    if (name === 'close') return json({ ok: false, hosted: true });
    if (name === 'sniper_ticks') return json([]);
    if (name === 'history') {
      return _f('data/history.json?t=' + Date.now()).then(function (r) { return r.json(); }).then(function (h) { return json(h[q.get('key')] || {}); });
    }
    return _f('data/' + name + '.json?t=' + Date.now(), { cache: 'no-store' });
  };
})();
