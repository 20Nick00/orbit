/* Independent port of the prototype waterfall; data is never interpreted as HTML. */
(() => {
  'use strict';
  const mk = (tag, attrs) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  const el = (tag, cls) => {
    const node = document.createElement(tag);
    node.className = cls;
    return node;
  };
  const fmtK = v => Math.abs(v) >= 1000 ? '$' + (Math.abs(v) / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : '$' + Math.round(Math.abs(v));
  const fmtS = v => (v < 0 ? '−' : '+') + Math.abs(v).toLocaleString('en-US', {style: 'currency', currency: 'USD'});
  function waterfall(host, steps) {
    const W = 600, H = 230, pL = 4, pR = 4, pT = 20, pB = 42;
    let run = 0, lo = 0, hi = 0;
    const pts = steps.map(s => {
      const v = s.missing ? null : Number(s.v);
      const a = s.missing ? run : s.total ? 0 : run;
      if (!s.missing) run = s.total ? v : run + v;
      lo = Math.min(lo, run, a); hi = Math.max(hi, run, a);
      return [a, run, {...s, v}];
    });
    const span = (hi - lo) || 1;
    const y = v => pT + (1 - (v - lo) / span) * (H - pT - pB);
    const n = steps.length, gw = (W - pL - pR) / n, bw = Math.min(44, gw - 14);
    const svg = mk('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Gross pay to net worth waterfall'});
    svg.appendChild(mk('line', {x1: pL, x2: W - pR, y1: y(0), y2: y(0), stroke: 'var(--line-strong)', 'stroke-width': 1}));
    host.replaceChildren(svg);
    const tip = el('div', 'tip'); host.appendChild(tip);
    pts.forEach(([a, b, s], i) => {
      const x = pL + i * gw + gw / 2 - bw / 2;
      const top = s.missing ? y(a) - 5 : y(Math.max(a, b));
      const h = s.missing ? 10 : Math.max(2, Math.abs(y(a) - y(b)));
      const col = s.total ? 'var(--fg)' : s.v >= 0 ? 'var(--s1)' : 'var(--s2)';
      const r = mk('rect', {x, y: top, width: bw, height: h, rx: 2, fill: s.missing ? 'none' : col});
      if (s.missing) {
        r.setAttribute('stroke', 'var(--fg-3)');
        r.setAttribute('stroke-dasharray', '3 3');
      }
      svg.appendChild(r);
      if (i < n - 1) svg.appendChild(mk('line', {x1: x + bw, x2: x + gw, y1: y(b), y2: y(b), stroke: 'var(--line-strong)', 'stroke-dasharray': '2 3'}));
      const amount = mk('text', {x: x + bw / 2, y: top - 4, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--fg-2)', 'font-family': 'var(--num)'});
      amount.textContent = s.missing ? '—' : (s.v < 0 ? '−' : s.total ? '' : '+') + fmtK(Math.abs(s.v));
      svg.appendChild(amount);
      const names = {
        'Pre-tax savings': ['Pre-tax', 'savings'], 'Pre-tax back in': ['Pre-tax', 'back in'],
        'Employer match': ['Employer', 'match'], 'Market & home': ['Market', '& home'],
        'Net worth change': ['Net worth', 'change']
      };
      const labels = names[s.l] || [s.l, s.l2];
      labels.forEach((label, j) => {
        if (!label) return;
        const text = mk('text', {x: x + bw / 2, y: H - 24 + j * 12, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--fg-3)'});
        text.textContent = label; svg.appendChild(text);
      });
      const show = () => {
        tip.textContent = s.l + (s.l2 ? ' ' + s.l2 : '') + ' ' + (s.missing ? '— (missing data)' : fmtS(s.v));
        tip.style.left = ((x + bw / 2) / W * 100) + '%';
        tip.style.top = (top / H * 100) + '%'; tip.classList.add('on');
      };
      r.setAttribute('tabindex', '0');
      r.setAttribute('aria-label', s.l + ': ' + (s.missing ? 'missing data' : fmtS(s.v)));
      r.addEventListener('pointerenter', show); r.addEventListener('focus', show);
      r.addEventListener('pointerleave', () => tip.classList.remove('on'));
      r.addEventListener('blur', () => tip.classList.remove('on'));
    });
  }
  const host = document.getElementById('wf');
  const data = document.getElementById('paycheck-data');
  if (host && data) waterfall(host, JSON.parse(data.textContent));
})();
