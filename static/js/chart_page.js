// chart_page.js - external Chart.js page script (UTF-8 plain text)
// Clean ASCII-friendly version (uses \u00B0 for degree symbol)

(function(){
  function getTiltFromPath() {
    const parts = window.location.pathname.split('/').filter(Boolean);
    if (parts.length >= 2 && parts[0].toLowerCase() === 'chart') return parts[1];
    const params = new URLSearchParams(window.location.search);
    return params.get('color') || 'Unknown';
  }
  function fmtLocal(ms) { return ms ? new Date(ms).toLocaleString() : ''; }
  function fmtTickLabel(ms) {
    if (!ms) return '';
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: 'numeric' });
  }

  const tiltColor = getTiltFromPath();

  const titleEl = document.getElementById('title');
  const metaEl = document.getElementById('meta');
  const statusEl = document.getElementById('status');
  const limitEl = document.getElementById('limit');
  const reloadBtn = document.getElementById('reload');
  const chartCanvas = document.getElementById('chart');

  if (titleEl) titleEl.textContent = `Tilt Chart \u2014 ${tiltColor}`;

  let chart = null;

  function createChart() {
    if (!chartCanvas) return;
    const ctx = chartCanvas.getContext('2d');
    const cfg = {
      type: 'line',
      data: { datasets: [] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        parsing: false,
        normalized: true,
        plugins: {
          legend: { display: true, position: 'top' },
          tooltip: {
            callbacks: {
              title: (items) => {
                const raw = items && items[0] && items[0].raw;
                return raw ? fmtLocal(raw.x) : '';
              },
              label: (ctx) => {
                const raw = ctx.raw || {};
                const v = raw.y !== undefined ? raw.y : ctx.parsed.y;
                const g = raw.gravity !== undefined ? ` SG:${raw.gravity}` : '';
                return `Temp: ${v}\u00B0F${g}`;
              }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Time' },
            ticks: {
              callback: function(tickValue) {
                try {
                  let v = tickValue;
                  if (typeof v === 'object' && v !== null) {
                    if ('value' in v) v = v.value;
                    else if ('tick' in v) v = v.tick;
                  }
                  const num = Number(v);
                  if (!Number.isFinite(num)) return '';
                  return fmtTickLabel(num);
                } catch (e) { return ''; }
              },
              maxTicksLimit: 10
            }
          },
          temp: { type: 'linear', position: 'left', title: { display: true, text: 'Temperature (\u00B0F)' } },
          gravity: { type: 'linear', position: 'right', title: { display: true, text: 'Gravity (SG)' }, grid: { drawOnChartArea: false } }
        }
      }
    };
    if (chart) chart.destroy();
    chart = new Chart(ctx, cfg);
  }

  function buildPointArrays(rawPoints) {
    const tempPoints = [], gravityPoints = [];
    for (const p of (rawPoints || [])) {
      const tsRaw = p.timestamp !== undefined ? p.timestamp : (p.date && p.time ? `${p.date}T${p.time}Z` : p.time || null);
      let ms = null;
      if (tsRaw !== null && tsRaw !== undefined && tsRaw !== '') {
        if (typeof tsRaw === 'number' && Number.isFinite(tsRaw)) {
          ms = tsRaw;
          if (ms < 1e11) ms = Math.round(ms * 1000);
        } else {
          const parsed = Date.parse(String(tsRaw));
          ms = Number.isFinite(parsed) ? parsed : null;
        }
      }
      let t = null;
      if (p.temp_f !== undefined && p.temp_f !== null) t = Number(p.temp_f);
      else if (p.current_temp !== undefined && p.current_temp !== null) t = Number(p.current_temp);
      else if (p.temp !== undefined && p.temp !== null) t = Number(p.temp);

      let g = null;
      if (p.gravity !== undefined && p.gravity !== null) g = Number(p.gravity);
      else if (p.sg !== undefined && p.sg !== null) g = Number(p.sg);

      if (ms !== null && Number.isFinite(t)) tempPoints.push({ x: ms, y: t, gravity: (Number.isFinite(g) ? g : null) });
      if (ms !== null && Number.isFinite(g)) gravityPoints.push({ x: ms, y: g });
    }
    tempPoints.sort((a,b) => a.x - b.x);
    gravityPoints.sort((a,b) => a.x - b.x);
    return { tempPoints, gravityPoints };
  }

  function makeSafeDatasets(tiltColor, tempPoints, gravityPoints) {
    const tempDataset = {
      label: String(tiltColor + ' Temp (\u00B0F)'),
      data: tempPoints.map(function(p){ return { x: Number(p.x), y: Number(p.y), gravity: (p.gravity !== undefined ? Number(p.gravity) : null) }; }),
      borderColor: '#e04b4b',
      backgroundColor: 'rgba(224,75,75,0.12)',
      pointRadius: 2,
      showLine: true,
      tension: 0.12,
      yAxisID: 'temp'
    };
    const gravityDataset = {
      label: String(tiltColor + ' Gravity (SG)'),
      data: gravityPoints.map(function(p){ return { x: Number(p.x), y: Number(p.y) }; }),
      borderColor: '#2a9d8f',
      backgroundColor: 'rgba(42,157,143,0.08)',
      pointRadius: 2,
      showLine: false,
      tension: 0.12,
      yAxisID: 'gravity'
    };
    const ds = [tempDataset];
    if (gravityPoints.length > 0) ds.push(gravityDataset);
    return ds;
  }

  async function loadData() {
    const limit = encodeURIComponent((limitEl && limitEl.value) || '100');
    if (statusEl) statusEl.textContent = 'Loading\u2026';
    try {
      const res = await fetch(`/chart_data/${encodeURIComponent(tiltColor)}?limit=${limit}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (metaEl) metaEl.textContent = `Matched: ${data.matched || 0}  Truncated: ${data.truncated ? 'yes' : 'no'}`;
      const { tempPoints, gravityPoints } = buildPointArrays(data.points || []);
      if (!chart) createChart();
      chart.data.datasets = makeSafeDatasets(tiltColor, tempPoints, gravityPoints);
      try { chart.update(); } catch(e) { console.error('chart.update failed', e); }
      if (statusEl) statusEl.textContent = `Loaded ${tempPoints.length} temp points, ${gravityPoints.length} gravity points`;
    } catch (err) {
      if (statusEl) statusEl.textContent = `Error loading data: ${err && err.message ? err.message : err}`;
      console.error('loadData error', err);
    }
  }

  createChart();
  loadData();
  if (reloadBtn) reloadBtn.addEventListener('click', () => loadData());
  if (limitEl) limitEl.addEventListener('change', () => loadData());
  setInterval(() => { if (document.visibilityState === 'visible') loadData(); }, 60000);

})();
