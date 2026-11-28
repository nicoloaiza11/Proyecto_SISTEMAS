/*
 * Gráficos de SOLTAT (Chart.js 4).
 *
 * El template incluye los datos con {{ graficos|json_script:"datos-graficos" }}
 * y un <canvas data-grafico="nombre"> por gráfico. Este archivo busca cada
 * canvas y lo dibuja con el constructor del mismo nombre.
 *
 * Colores: paleta categórica validada (daltonismo y contraste sobre blanco),
 * asignada siempre en el mismo orden.
 */
(function () {
  'use strict';

  if (typeof Chart === 'undefined') return;
  const fuente = document.getElementById('datos-graficos');
  if (!fuente) return;
  const datos = JSON.parse(fuente.textContent);

  const SERIE = ['#1f8a68', '#c08a1e', '#3f72c0', '#c4553a'];
  const TEXTO = '#263238';
  const TEXTO_SUAVE = '#6b7377';
  const GRILLA = 'rgba(38, 50, 56, 0.08)';
  const REFERENCIA = '#8a9296';

  Chart.defaults.font.family = "'Roboto', sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = TEXTO_SUAVE;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.legend.labels.color = TEXTO;
  Chart.defaults.plugins.tooltip.backgroundColor = '#173F35';
  Chart.defaults.plugins.tooltip.padding = 10;
  Chart.defaults.maintainAspectRatio = false;

  const nf = new Intl.NumberFormat('es-AR');
  const kg = (v) => (v == null ? '—' : nf.format(v) + ' kg');
  const min = (v) => (v == null ? '—' : nf.format(v) + ' min');
  // 100 -> '1 h 40 min', 45 -> '45 min'
  const duracion = (v) => {
    if (v == null) return '—';
    const m = Math.round(v);
    const h = Math.floor(m / 60);
    return h ? `${h} h ${m % 60} min` : `${m} min`;
  };
  // Montos en pesos argentinos, como el comprobante.
  const pesos = (v) => (v == null ? '—' : '$ ' + nf.format(Math.round(v)));
  const pesosKg = (v) => (v == null ? '—' : '$ ' + v.toLocaleString('es-AR', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }) + '/kg');

  const ejeY = (formato) => ({
    beginAtZero: true,
    grid: { color: GRILLA },
    border: { display: false },
    ticks: { callback: (v) => formato(v), maxTicksLimit: 6 },
  });
  const ejeX = { grid: { display: false }, border: { color: GRILLA } };

  const barra = (label, data, color) => ({
    type: 'bar',
    label,
    data,
    backgroundColor: color,
    borderRadius: { topLeft: 4, topRight: 4 },
    borderSkipped: 'bottom',
    maxBarThickness: 28,
    categoryPercentage: 0.7,
    barPercentage: 0.9,
  });

  // Línea punteada de referencia (capacidad), en la misma unidad que las barras.
  // `valor` puede ser un número (igual para todas las barras) o una lista.
  const referencia = (label, valor, largo) => ({
    type: 'line',
    label,
    data: Array.isArray(valor) ? valor : Array(largo).fill(valor),
    stepped: Array.isArray(valor) ? 'middle' : false,
    borderColor: REFERENCIA,
    borderWidth: 2,
    borderDash: [6, 4],
    pointRadius: 0,
    pointHoverRadius: 0,
    fill: false,
  });

  const anillo = (d, formato) => ({
    type: 'doughnut',
    data: {
      labels: d.etiquetas,
      datasets: [{
        data: d.valores,
        backgroundColor: SERIE.slice(0, d.valores.length),
        borderColor: '#ffffff',
        borderWidth: 2,
        hoverOffset: 6,
      }],
    },
    options: {
      cutout: '62%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            generateLabels: (chart) => Chart.overrides.doughnut.plugins.legend.labels
              .generateLabels(chart)
              .map((item, i) => ({ ...item, text: `${item.text}: ${formato(d.valores[i])}` })),
          },
        },
        tooltip: { callbacks: { label: (c) => ` ${c.label}: ${formato(c.parsed)}` } },
      },
    },
  });

  const constructores = {
    // ---------------- Productor ----------------
    productorKilos: (d) => ({
      type: 'bar',
      data: {
        labels: d.etiquetas,
        datasets: [barra('Estimados', d.estimados, SERIE[0]), barra('Reales', d.reales, SERIE[1])],
      },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: { x: ejeX, y: ejeY(kg) },
        plugins: {
          legend: { position: 'top', align: 'end' },
          tooltip: {
            callbacks: {
              title: (items) => d.detalle[items[0].dataIndex],
              label: (c) => ` ${c.dataset.label}: ${kg(c.parsed.y)}`,
              afterBody: (items) => {
                const i = items[0].dataIndex;
                if (!d.estimados[i]) return '';
                const desvio = ((d.reales[i] - d.estimados[i]) * 100) / d.estimados[i];
                return `Desvío: ${desvio > 0 ? '+' : ''}${desvio.toFixed(1)} %`;
              },
            },
          },
        },
      },
    }),
    productorCortes: (d) => anillo(d, kg),
    // Liquidación y precio usan dos gráficos (uno por unidad), nunca un doble eje.
    productorLiquidacion: (d) => ({
      type: 'bar',
      data: { labels: d.etiquetas, datasets: [barra('Liquidación', d.importes, SERIE[0])] },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: { x: ejeX, y: ejeY(pesos) },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => d.detalle[items[0].dataIndex],
              label: (c) => ` Liquidación: ${pesos(c.parsed.y)}`,
              afterLabel: (c) => [
                ` Kilos: ${kg(d.kilos[c.dataIndex])}`,
                ` Precio: ${pesosKg(d.precios[c.dataIndex])}`,
              ],
            },
          },
        },
      },
    }),
    productorPrecio: (d) => ({
      type: 'line',
      data: {
        labels: d.etiquetas,
        datasets: [{
          label: 'Precio por kilo',
          data: d.precios,
          borderColor: SERIE[1],
          backgroundColor: SERIE[1],
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBorderColor: '#ffffff',
          pointBorderWidth: 2,
        }],
      },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: ejeX,
          y: { ...ejeY(pesosKg), beginAtZero: false, grace: '10%' },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => d.detalle[items[0].dataIndex],
              label: (c) => ` Precio: ${pesosKg(c.parsed.y)}`,
            },
          },
        },
      },
    }),
    // Simple para el productor: una barra por entrega y una línea con su promedio.
    productorEspera: (d) => {
      const validos = d.minutos.filter((v) => v != null);
      const promedio = validos.length ? validos.reduce((a, b) => a + b, 0) / validos.length : null;
      const datasets = [barra('Tiempo de espera', d.minutos, SERIE[0])];
      if (promedio != null) {
        datasets.push(referencia(`Su promedio: ${duracion(promedio)}`, Math.round(promedio), d.etiquetas.length));
      }
      return {
        type: 'bar',
        data: { labels: d.etiquetas, datasets },
        options: {
          interaction: { mode: 'index', intersect: false },
          scales: { x: ejeX, y: ejeY(min) },
          plugins: {
            legend: { position: 'top', align: 'end' },
            tooltip: {
              filter: (item) => item.datasetIndex === 0,
              callbacks: {
                title: (items) => d.detalle[items[0].dataIndex],
                label: (c) => ` Esperó ${duracion(c.parsed.y)}`,
              },
            },
          },
        },
      };
    },

    // ---------------- Empresa ----------------
    empresaDias: (d) => ({
      type: 'bar',
      data: {
        labels: d.etiquetas,
        datasets: [
          barra('Kilos recibidos', d.kilos, SERIE[0]),
          referencia('Capacidad diaria', d.capacidad, d.etiquetas.length),
        ],
      },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: { x: ejeX, y: ejeY(kg) },
        plugins: {
          legend: { position: 'top', align: 'end' },
          tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${kg(c.parsed.y)}` } },
        },
      },
    }),
    empresaHoras: (d) => ({
      type: 'bar',
      data: {
        labels: d.etiquetas,
        datasets: [
          barra('Kilos programados', d.kilos, SERIE[0]),
          referencia('Capacidad según sus rampas disponibles', d.capacidad, d.etiquetas.length),
        ],
      },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: { x: ejeX, y: ejeY(kg) },
        plugins: {
          legend: { position: 'top', align: 'end' },
          tooltip: {
            callbacks: {
              title: (items) => `De ${d.etiquetas[items[0].dataIndex]} a la hora siguiente`,
              label: (c) => ` ${c.dataset.label}: ${kg(c.parsed.y)}`,
            },
          },
        },
      },
    }),
    empresaCortes: (d) => anillo(d, kg),
  };

  document.querySelectorAll('canvas[data-grafico]').forEach((canvas) => {
    const nombre = canvas.dataset.grafico;
    const d = datos[canvas.dataset.datos || nombre];
    const crear = constructores[nombre];
    if (!d || !crear) return;
    const vacio = Array.isArray(d.valores) ? d.valores.every((v) => !v)
      : Array.isArray(d.etiquetas) && d.etiquetas.length === 0;
    if (vacio) {
      canvas.closest('.grafico-caja').innerHTML =
        '<p class="text-muted small text-center my-5">Todavía no hay datos para este gráfico.</p>';
      return;
    }
    new Chart(canvas, crear(d));
  });
})();
