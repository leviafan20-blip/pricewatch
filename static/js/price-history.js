/* Chart.js is used only for drawing. All monetary statistics come from Django. */
(() => {
  "use strict";
  const canvas = document.getElementById("price-chart");
  if (!canvas) return;
  const status = document.getElementById("chart-status");
  const locale = document.documentElement.lang || "en";
  try {
    const payload = JSON.parse(document.getElementById("price-chart-data").textContent);
    const dates = new Intl.DateTimeFormat(locale, {timeZone: payload.timezone, dateStyle: "short", timeStyle: "medium"});
    const days = new Intl.DateTimeFormat(locale, {timeZone: payload.timezone, day: "2-digit", month: "short", year: "2-digit"});
    const timestamps = payload.series.flatMap(series => series.points.map(point => Date.parse(point.at)));
    const earliest = Math.min(...timestamps);
    const latest = Math.max(...timestamps);
    const tickDates = latest - earliest < 172800000
      ? new Intl.DateTimeFormat(locale, {timeZone: payload.timezone, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"})
      : days;
    const colors = ["#168078", "#4365b5", "#c06c32", "#9255a4", "#65753d"];
    const numberParts = new Intl.NumberFormat(locale).formatToParts(1234.5);
    const group = numberParts.find(part => part.type === "group").value;
    const decimal = numberParts.find(part => part.type === "decimal").value;
    const exactAmount = value => {
      const [integer, fraction = ""] = value.split(".");
      const tail = fraction.replace(/0+$/, "");
      return integer.replace(/\B(?=(\d{3})+(?!\d))/g, group) + (tail ? decimal + tail : "");
    };
    new Chart(canvas, {
      type: "line",
      data: {datasets: payload.series.map((series, index) => ({
        label: series.label,
        // The only conversion of monetary strings to binary numbers: visualization.
        data: series.points.map(point => ({x: Date.parse(point.at), y: point.price === null ? null : Number(point.price), exactPrice: point.price})),
        borderColor: colors[index % colors.length], backgroundColor: colors[index % colors.length],
        borderWidth: 2, pointRadius: series.sampled ? 2 : 3, pointHoverRadius: 6,
        pointHitRadius: 14, tension: 0, spanGaps: false, showLine: !series.sampled,
      }))},
      options: {
        locale, responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "nearest", intersect: false},
        scales: {
          x: {type: "linear", min: earliest === latest ? earliest - 3600000 : earliest,
            max: earliest === latest ? latest + 3600000 : latest,
            ticks: {maxTicksLimit: 7, callback: value => tickDates.format(new Date(value))}, grid: {display: false}},
          y: {title: {display: true, text: payload.currency}, grace: "8%", ticks: {maxTicksLimit: 6}},
        },
        plugins: {
          legend: {position: "bottom", labels: {usePointStyle: true, padding: 24}},
          tooltip: {callbacks: {
            title: items => dates.format(new Date(items[0].parsed.x)),
            label: item => `${item.dataset.label}: ${exactAmount(item.raw.exactPrice)} ${payload.currency}`,
          }},
        },
      },
    });
    canvas.dataset.rendered = "true";
  } catch (error) {
    status.textContent = canvas.dataset.errorMessage;
    console.error("PriceWatch chart could not be rendered", error);
  }
})();
