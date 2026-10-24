(function () {
  let ENG = ['Prometheus', 'Selene', 'Helios', 'Typhon'];
  const ECOL = (e) => 'var(--e' + ENG.indexOf(e) + ')';
  const META = JSON.parse(document.getElementById('d-meta').textContent);
  const csv = (key, id) => d3.csvParse(document.getElementById('d-' + key + '-' + id).textContent.trim());
  let D = {}, M = {}, which = 'p', basis = '6';
  function load() {
    const key = which === 'p' ? 'p' : 'j' + basis; D = {};
    ['metrics', 'equity', 'monthly', 'yearly', 'engines', 'trades', 'skipped'].forEach((k) => { D[k] = csv(key, k); });
    M = {}; D.metrics.forEach((r) => { M[r.metric] = r.value; });
    ENG = META[key].engines;
    document.getElementById('engnames').textContent = ENG.slice(0, -1).join(', ') + ' and ' + ENG[ENG.length - 1];
    document.getElementById('formulas').innerHTML = META[key].formulas; document.getElementById('slip-body').innerHTML = META[key].slip;
    document.getElementById('swap-hint').textContent = which === 'p' ? 'Prometheus runs the crude oil trades with two lots a unit. Switch to Janus to replace it with Janus at three lots a unit; the other three engines are unchanged.' : 'Janus replaces Prometheus on crude oil, three lots a unit. Prices times 10 barrels a lot, divided by 3, then times 6 scales Prometheus\'s margin per lot to three lots; times 4 keeps the margin of a two-lot Prometheus unit.'; document.getElementById('jnote').hidden = which === 'p'; document.getElementById('basis-wrap').hidden = which === 'p';
    const sel = document.getElementById('eng'); sel.innerHTML = ['All'].concat(ENG).map((n) => '<option>' + n + '</option>').join(''); tr.eng = 'All';
  }
  const INR = (n) => Math.round(Math.abs(n)).toLocaleString('en-IN');
  const fm = {
    inr: (n) => (n < 0 ? '−' : '') + 'Rs ' + INR(n),
    pct: (n) => (n < 0 ? '−' : '') + Math.abs(n).toFixed(1) + '%',
    x: (n) => n.toFixed(1) + 'x', num1: (n) => n.toFixed(1), num2: (n) => n.toFixed(2),
    int: (n) => Math.round(n).toLocaleString('en-IN'),
    days: (n) => Math.round(n).toLocaleString('en-IN') + ' days',
  };
  const compact = (v) => v >= 1e7 ? +(v / 1e7).toFixed(2) + ' Cr' : v >= 1e5 ? +(v / 1e5).toFixed(1) + ' L' : v.toLocaleString('en-IN');
  const parseT = (s) => new Date(s.replace(' ', 'T') + ':00Z');
  const fDay = d3.utcFormat('%-d %b %Y'), fMon = d3.utcFormat('%b %y'), fTick = d3.utcFormat('%b %Y');
  const root = document.getElementById('app');
  const tipEl = document.getElementById('tip');
  const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

  function showTip(ev, html) {
    tipEl.innerHTML = html; tipEl.hidden = false;
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight; let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14; if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
    tipEl.style.left = Math.max(8, x) + 'px'; tipEl.style.top = Math.max(8, y) + 'px';
  }
  const hideTip = () => { tipEl.hidden = true; };
  function measure(svg, labels) {
    let w = 0; labels.forEach((s) => { const t = svg.append('text').text(s); w = Math.max(w, t.node().getComputedTextLength()); t.remove(); }); return w;
  }
  function mkSvg(box, h) {
    box.replaceChildren(); const w = Math.max(280, box.clientWidth);
    const svg = d3.select(box).append('svg').attr('viewBox', `0 0 ${w} ${h}`); return { svg, w };
  }
  function gridLine(S, x1, x2, y, axis) { S.svg.append('line').attr('class', axis ? 'axisline' : 'grid').attr('x1', x1).attr('x2', x2).attr('y1', y).attr('y2', y); }

  function marks() {
    root.querySelectorAll('[data-m]').forEach((el) => { const v = M[el.dataset.m]; if (v === undefined) return; el.textContent = el.dataset.f === 'text' ? v : fm[el.dataset.f](Number(v)); });
    document.getElementById('lede').innerHTML = 'Rs 4,00,000 on 3 April 2023 becomes <b>Rs ' + INR(+M.end_equity) + '</b> by {{END_FULL}} after costs, ' + (+M.multiple).toFixed(1) + ' times the capital. That is ' + (+M.cagr_pct).toFixed(0) + '% a year over ' + (+M.years).toFixed(1) + ' years, with a deepest closed-trade drop of ' + Math.abs(+M.max_dd_pct).toFixed(1) + '%.';
  }

  let mode = 'log';
  function drawEquity() {
    const eb = document.getElementById('c-eq'), db = document.getElementById('c-dd');
    const rows = [{ t: parseT('2023-04-03 00:00'), v: 400000, dd: 0, n: 0, e: '' }].concat(D.equity.map((r) => ({ t: parseT(r.exit_ts), v: +r.cum, dd: +r.dd_pct, n: +r.trade_no, e: r.engine })));
    const H1 = 300, H2 = 130, top = 8, bot = 22, right = 12;
    const A = mkSvg(eb, H1), B = mkSvg(db, H2);
    const vmin = d3.min(rows, (d) => d.v), vmax = d3.max(rows, (d) => d.v);
    let y, ticks;
    if (mode === 'log') { y = d3.scaleLog([vmin * 0.9, vmax * 1.08], [H1 - bot, top]); ticks = [4e5, 6e5, 1e6, 2e6, 4e6, 6e6, 1e7].filter((v) => v >= y.domain()[0] && v <= y.domain()[1]); }
    else { y = d3.scaleLinear([0, vmax * 1.05], [H1 - bot, top]).nice(); ticks = y.ticks(6); }
    const yd = d3.scaleLinear([Math.floor(d3.min(rows, (d) => d.dd) / 5) * 5, 0], [H2 - bot, top]);
    const ddTicks = d3.range(0, yd.domain()[0] - 1, -5);
    const left = Math.max(measure(A.svg, ticks.map(compact)), measure(B.svg, ddTicks.map((t) => t + '%'))) + 14;
    const x = d3.scaleUtc(d3.extent(rows, (d) => d.t), [left, A.w - right]);
    const xB = d3.scaleUtc(x.domain(), [left, B.w - right]);
    const xt = x.ticks(7);
    [[A, x, y, ticks, compact, H1], [B, xB, yd, ddTicks, (t) => t + '%', H2]].forEach(([S, xs, ys, tk, f, H]) => {
      tk.forEach((t) => { gridLine(S, left, S.w - right, ys(t)); S.svg.append('text').attr('x', left - 8).attr('y', ys(t) + 4).attr('text-anchor', 'end').text(f(t)); });
      xt.forEach((t) => S.svg.append('text').attr('x', xs(t)).attr('y', H - 6).attr('text-anchor', 'middle').text(fTick(t)));
    });
    A.svg.append('path').datum(rows).attr('class', 'ln').attr('d', d3.line().x((d) => x(d.t)).y((d) => y(d.v)));
    B.svg.append('path').datum(rows).attr('fill', 'var(--neg)').attr('fill-opacity', 0.25).attr('stroke', 'var(--neg)').attr('stroke-width', 1.5).attr('d', d3.area().x((d) => xB(d.t)).y0(yd(0)).y1((d) => yd(d.dd)));
    const bis = d3.bisector((d) => d.t).center;
    [[A, x, H1], [B, xB, H2]].forEach(([S, xs, H]) => {
      const rule = S.svg.append('line').attr('class', 'cross').attr('y1', top).attr('y2', H - bot).attr('visibility', 'hidden');
      S.svg.append('rect').attr('x', left).attr('y', 0).attr('width', S.w - left - right).attr('height', H - bot).attr('fill', 'transparent')
        .on('mousemove', (ev) => {
          const d = rows[bis(rows, xs.invert(d3.pointer(ev, S.svg.node())[0]))];
          rule.attr('x1', xs(d.t)).attr('x2', xs(d.t)).attr('visibility', 'visible');
          showTip(ev, '<div class="tl">' + fDay(d.t) + (d.n ? ' · trade ' + d.n + ' (' + esc(d.e) + ')' : ' · start') + '</div><div class="tv">Rs ' + INR(d.v) + '</div><div class="tl">From peak ' + d.dd.toFixed(1) + '%</div>');
        }).on('mouseleave', () => { hideTip(); rule.attr('visibility', 'hidden'); });
    });
  }

  function drawMonthly() {
    const box = document.getElementById('c-mon'); const rows = D.monthly.map((r) => ({ m: r.month, v: +r.return_pct, p: +r.pnl }));
    const H = 260, top = 8, bot = 22, right = 8; const S = mkSvg(box, H);
    const ext = d3.extent(rows, (d) => d.v); const y = d3.scaleLinear([Math.min(ext[0], 0), ext[1]], [H - bot, top]).nice(); const yt = y.ticks(6);
    const left = measure(S.svg, yt.map((t) => t + '%')) + 14;
    const x = d3.scaleBand(rows.map((d) => d.m), [left, S.w - right]).padding(0.25);
    yt.forEach((t) => { gridLine(S, left, S.w - right, y(t), t === 0); S.svg.append('text').attr('x', left - 8).attr('y', y(t) + 4).attr('text-anchor', 'end').text(t + '%'); });
    const every = Math.ceil(rows.length / Math.max(2, Math.floor((S.w - left) / 60)));
    rows.forEach((d, i) => {
      if (i % every === 0) S.svg.append('text').attr('x', x(d.m) + x.bandwidth() / 2).attr('y', H - 6).attr('text-anchor', 'middle').text(fMon(new Date(d.m + '-01T00:00:00Z')));
      S.svg.append('rect').attr('class', 'bar ' + (d.v >= 0 ? 'pos' : 'neg')).attr('x', x(d.m)).attr('width', x.bandwidth()).attr('y', Math.min(y(0), y(d.v))).attr('height', Math.max(1, Math.abs(y(d.v) - y(0)))).attr('rx', 2)
        .on('mousemove', (ev) => showTip(ev, '<div class="tl">' + fMon(new Date(d.m + '-01T00:00:00Z')) + '</div><div class="tv">' + (d.v < 0 ? '−' : '') + Math.abs(d.v).toFixed(1) + '%</div><div class="tl">' + (d.p < 0 ? '−' : '') + 'Rs ' + INR(d.p) + '</div>'))
        .on('mouseleave', hideTip);
    });
  }

  function drawUnits() {
    const box = document.getElementById('c-units'); const by = {}; ENG.forEach((e) => { by[e] = []; });
    D.trades.forEach((r) => { if (by[r.engine]) by[r.engine].push({ t: parseT(r.entry_ts), u: +r.units }); });
    ENG.forEach((e) => by[e].sort((a, b) => a.t - b.t));
    const H = 260, top = 8, bot = 22, right = 86; const S = mkSvg(box, H);
    const y = d3.scaleLinear([0, d3.max(ENG, (e) => d3.max(by[e], (d) => d.u))], [H - bot, top]).nice(); const yt = y.ticks(6);
    const left = measure(S.svg, yt.map(String)) + 14;
    const x = d3.scaleUtc(d3.extent(D.trades, (r) => parseT(r.entry_ts)), [left, S.w - right]);
    yt.forEach((t) => { gridLine(S, left, S.w - right, y(t)); S.svg.append('text').attr('x', left - 8).attr('y', y(t) + 4).attr('text-anchor', 'end').text(t); });
    x.ticks(5).forEach((t) => S.svg.append('text').attr('x', x(t)).attr('y', H - 6).attr('text-anchor', 'middle').text(fTick(t)));
    const ends = [];
    ENG.forEach((e) => {
      S.svg.append('path').datum(by[e]).attr('fill', 'none').style('stroke', ECOL(e)).attr('stroke-width', 1.5).attr('d', d3.line().curve(d3.curveStepAfter).x((d) => x(d.t)).y((d) => y(d.u)));
      const last = by[e][by[e].length - 1]; if (last) ends.push({ e, y: y(last.u) });
    });
    ends.sort((a, b) => a.y - b.y); for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
    ends.forEach((d) => S.svg.append('text').attr('x', S.w - right + 6).attr('y', d.y + 4).style('fill', ECOL(d.e)).style('font-weight', 600).text(d.e));
    const rule = S.svg.append('line').attr('class', 'cross').attr('y1', top).attr('y2', H - bot).attr('visibility', 'hidden'); const bis = d3.bisector((d) => d.t).right;
    S.svg.append('rect').attr('x', left).attr('y', 0).attr('width', S.w - left - right).attr('height', H - bot).attr('fill', 'transparent')
      .on('mousemove', (ev) => {
        const t = x.invert(d3.pointer(ev, S.svg.node())[0]); rule.attr('x1', x(t)).attr('x2', x(t)).attr('visibility', 'visible');
        showTip(ev, '<div class="tl">' + fDay(t) + '</div>' + ENG.map((e) => { const i = bis(by[e], t) - 1; return '<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:' + ECOL(e) + '"></i>' + e + ' <b>' + (i >= 0 ? by[e][i].u : '—') + '</b></div>'; }).join(''));
      }).on('mouseleave', () => { hideTip(); rule.attr('visibility', 'hidden'); });
  }

  function fillTable(el, rows, cols) {
    const frag = document.createDocumentFragment();
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      cols.forEach((c) => {
        const td = document.createElement('td'); const raw = r[c.f]; td.textContent = c.fmt && raw !== '' ? fm[c.fmt](Number(raw)) : raw;
        if (c.n) td.className = 'n'; if (c.sign && Number(raw) < 0) td.classList.add('neg'); tr.appendChild(td);
      });
      frag.appendChild(tr);
    });
    el.querySelector('tbody').replaceChildren(frag);
  }
  function smallTables() {
    fillTable(document.getElementById('t-year'), D.yearly, [{ f: 'label' }, { f: 'start_equity', fmt: 'inr', n: 1 }, { f: 'end_equity', fmt: 'inr', n: 1 }, { f: 'pnl', fmt: 'inr', n: 1, sign: 1 }, { f: 'return_pct', fmt: 'pct', n: 1, sign: 1 }, { f: 'max_dd_pct', fmt: 'pct', n: 1, sign: 1 }, { f: 'trades', fmt: 'int', n: 1 }, { f: 'costs', fmt: 'inr', n: 1 }, { f: 'share_of_profit_pct', fmt: 'pct', n: 1 }]);
    const last = D.yearly[D.yearly.length - 1];
    document.getElementById('yearnote').textContent = last.label + ' supplied ' + Number(last.share_of_profit_pct).toFixed(0) + '% of all the profit, on an account that was already ' + (Number(last.start_equity) / 4e5).toFixed(1) + ' times the starting capital.';
    fillTable(document.getElementById('t-eng'), D.engines, [{ f: 'engine' }, { f: 'trades', fmt: 'int', n: 1 }, { f: 'skipped', fmt: 'int', n: 1 }, { f: 'win_rate_pct', fmt: 'pct', n: 1 }, { f: 'pnl', fmt: 'inr', n: 1, sign: 1 }, { f: 'share_of_profit_pct', fmt: 'pct', n: 1 }, { f: 'profit_factor', fmt: 'num2', n: 1 }, { f: 'avg_units', fmt: 'num1', n: 1 }, { f: 'peak_units', fmt: 'int', n: 1 }, { f: 'costs', fmt: 'inr', n: 1 }]);
    fillTable(document.getElementById('t-skip'), D.skipped, [{ f: 'n', n: 1 }, { f: 'engine' }, { f: 'direction' }, { f: 'entry_ts' }, { f: 'entry', fmt: 'num1', n: 1 }, { f: 'equity_before', fmt: 'inr', n: 1 }, { f: 'share', fmt: 'inr', n: 1 }, { f: 'mpu', fmt: 'inr', n: 1 }]);
  }

  const tr = { q: '', eng: 'All', key: 'trade_no', dir: -1, shown: 200 };
  const NUM = { trade_no: 1, entry: 1, units: 1, mpu: 1, equity_before: 1, rs_unit: 1, gross: 1, orders: 1, charges: 1, slippage: 1, pnl: 1 };
  function drawTrades() {
    const q = tr.q.trim().toLowerCase();
    let rows = D.trades.filter((r) => (tr.eng === 'All' || r.engine === tr.eng) && (!q || (r.engine + ' ' + r.reason + ' ' + r.direction + ' ' + r.entry_ts + ' ' + r.exit_ts).toLowerCase().includes(q)));
    rows = rows.slice().sort((a, b) => { const A = NUM[tr.key] ? Number(a[tr.key]) : a[tr.key], B = NUM[tr.key] ? Number(b[tr.key]) : b[tr.key]; return (A < B ? -1 : A > B ? 1 : 0) * tr.dir; });
    const shown = rows.slice(0, tr.shown);
    fillTable(document.getElementById('t-trades'), shown, [{ f: 'trade_no', n: 1 }, { f: 'engine' }, { f: 'direction' }, { f: 'entry_ts' }, { f: 'exit_ts' }, { f: 'entry', fmt: 'num2', n: 1 }, { f: 'units', fmt: 'int', n: 1 }, { f: 'mpu', fmt: 'int', n: 1 }, { f: 'equity_before', fmt: 'int', n: 1 }, { f: 'rs_unit', fmt: 'inr', n: 1, sign: 1 }, { f: 'gross', fmt: 'inr', n: 1, sign: 1 }, { f: 'orders', fmt: 'int', n: 1 }, { f: 'charges', fmt: 'inr', n: 1 }, { f: 'slippage', fmt: 'inr', n: 1 }, { f: 'pnl', fmt: 'inr', n: 1, sign: 1 }, { f: 'reason' }]);
    document.getElementById('count').textContent = 'Showing ' + shown.length.toLocaleString('en-IN') + ' of ' + rows.length.toLocaleString('en-IN') + ' trades';
    document.getElementById('more').parentElement.hidden = shown.length >= rows.length;
    root.querySelectorAll('#t-trades th').forEach((th) => { const b = th.querySelector('button'); if (b) th.setAttribute('aria-sort', b.dataset.k === tr.key ? (tr.dir > 0 ? 'ascending' : 'descending') : 'none'); });
  }

  document.getElementById('q').addEventListener('input', (e) => { tr.q = e.target.value; tr.shown = 200; drawTrades(); });
  document.getElementById('eng').addEventListener('change', (e) => { tr.eng = e.target.value; tr.shown = 200; drawTrades(); });
  document.getElementById('more').addEventListener('click', () => { tr.shown += 200; drawTrades(); });
  root.querySelectorAll('#t-trades th button').forEach((b) => b.addEventListener('click', () => { const k = b.dataset.k; if (tr.key === k) tr.dir = -tr.dir; else { tr.key = k; tr.dir = NUM[k] ? -1 : 1; } drawTrades(); }));
  function setMode(m) { mode = m; document.getElementById('b-lin').setAttribute('aria-pressed', m === 'lin'); document.getElementById('b-log').setAttribute('aria-pressed', m === 'log'); drawEquity(); }
  document.getElementById('b-lin').addEventListener('click', () => setMode('lin'));
  document.getElementById('b-log').addEventListener('click', () => setMode('log'));

  function charts() { drawEquity(); drawMonthly(); drawUnits(); }
  function render() { load(); tr.shown = 200; marks(); smallTables(); drawTrades(); charts(); }
  function setPress(ids, on) { ids.forEach((id) => document.getElementById(id).setAttribute('aria-pressed', id === on)); }
  document.getElementById('s-p').addEventListener('click', () => { which = 'p'; setPress(['s-p', 's-j'], 's-p'); render(); });
  document.getElementById('s-j').addEventListener('click', () => { which = 'j'; setPress(['s-p', 's-j'], 's-j'); render(); });
  document.getElementById('b-6').addEventListener('click', () => { basis = '6'; setPress(['b-6', 'b-4'], 'b-6'); render(); });
  document.getElementById('b-4').addEventListener('click', () => { basis = '4'; setPress(['b-6', 'b-4'], 'b-4'); render(); });
  render();
  let rt; addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(charts, 150); });
})();
