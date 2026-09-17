/* RamAI — Chart.js Initialization */
(function () {
  'use strict';

  var COLORS = {
    teal500: '#06B6D4', teal400: '#22D3EE', teal300: '#67E8F9',
    orange500: '#F97316', orange400: '#FB923C',
    coral400: '#F87171', green500: '#22C55E',
    navy900: '#0F172A', muted500: '#64748B', muted400: '#94A3B8',
  };

  function getJsonData(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function applyDefaults() {
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = COLORS.muted500;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.tooltip.backgroundColor = COLORS.navy900;
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.elements.bar.borderRadius = 4;
    Chart.defaults.elements.line.tension = 0.3;
    Chart.defaults.scale.grid.color = 'rgba(226,232,240,0.6)';
    Chart.defaults.scale.border = { display: false };
  }

  function initDemandChart() {
    var raw = getJsonData('demand-chart-data');
    if (!raw) return;
    var data = raw.demand || raw;
    var ctx = document.getElementById('demandChart');
    if (!ctx) return;
    new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.timestamps,
        datasets: [
          {
            label: 'Order Masuk',
            data: data.orders,
            backgroundColor: 'rgba(6,182,212,0.6)',
            borderColor: COLORS.teal500,
            borderWidth: 1,
            borderRadius: 5,
            order: 2,
          },
          {
            label: 'Stok',
            data: data.stock,
            type: 'line',
            borderColor: COLORS.orange500,
            backgroundColor: 'transparent',
            borderWidth: 2,
            borderDash: [4, 4],
            pointRadius: 0,
            pointHoverRadius: 4,
            yAxisID: 'y1',
            order: 1,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top', align: 'end' } },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 45, font: { size: 10 } } },
          y: { beginAtZero: true, title: { display: true, text: 'Orders', font: { size: 11 } } },
          y1: { position: 'right', beginAtZero: true, grid: { display: false }, title: { display: true, text: 'Stok', font: { size: 11 }, color: COLORS.orange500 }, ticks: { color: COLORS.orange400 } },
        },
        animation: { duration: 800, easing: 'easeOutQuart' },
      },
    });
  }

  function initForecastChart() {
    var raw = getJsonData('forecast-chart-data');
    if (!raw) return;
    var data = raw.forecast || raw;
    var ctx = document.getElementById('forecastChart');
    if (!ctx) return;
    new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [
          { label: 'Pesimis (P10)', data: data.p10, backgroundColor: 'rgba(148,163,184,0.5)', borderColor: COLORS.muted400, borderWidth: 1 },
          { label: 'Wajar (P50)', data: data.p50, backgroundColor: 'rgba(6,182,212,0.6)', borderColor: COLORS.teal500, borderWidth: 1 },
          { label: 'Optimis (P90)', data: data.p90, backgroundColor: 'rgba(249,115,22,0.5)', borderColor: COLORS.orange500, borderWidth: 1 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top', align: 'end' } },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, title: { display: true, text: 'Unit (kumulatif)', font: { size: 11 } } },
        },
        animation: { duration: 1000, easing: 'easeOutQuart' },
      },
    });
  }

  function destroyExisting() {
    Object.keys(Chart.instances).forEach(function(key) {
      try { Chart.instances[key].destroy(); } catch(e) {}
    });
  }

  function init() {
    if (typeof Chart === 'undefined') return;
    destroyExisting();
    applyDefaults();
    initDemandChart();
    initForecastChart();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  document.addEventListener('htmx:afterSettle', function () {
    setTimeout(init, 50);
  });
})();
