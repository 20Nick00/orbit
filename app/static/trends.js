/* Money becomes a JS number only at the drawing boundary. */
(() => {
  const {barChart} = window.OrbitCharts;
  const read = id => JSON.parse(document.getElementById(id).textContent);
  const months = read('trends-monthly');
  const mix = read('trends-mix');
  const categories = read('trends-categories');
  const value = v => v == null ? null : Number(v);
  const labels = rows => rows.map(r => new Date(r.month + '-01T12:00:00')
    .toLocaleDateString('en-US', {month: 'short'}));
  const draw = (id, rows, series, opts) => {
    if (series.flat().some(v => v !== null)) {
      barChart(document.getElementById(id), labels(rows), series, opts);
    }
  };
  draw('ioChart', months, [months.map(r => value(r.income)), months.map(r => value(r.spending))],
    {names: ['Income', 'Spending'], label: 'Income vs spending by month'});
  draw('nwChgChart', months, [months.map(r => value(r.net_worth_change))],
    {colors: v => v >= 0 ? 'var(--pos)' : 'var(--neg)', label: 'Monthly net worth change'});
  draw('mixChart', mix, [mix.map(r => value(r.investments)), mix.map(r => value(r.cash))],
    {names: ['Investments', 'Cash'], label: 'Investments vs cash by month'});
  const max = categories.length ? Number(categories[0].spending) : 0;
  document.querySelectorAll('#catList .track i').forEach((bar, i) => {
    bar.style.width = (Number(categories[i].spending) / max * 100) + '%';
  });
})();
