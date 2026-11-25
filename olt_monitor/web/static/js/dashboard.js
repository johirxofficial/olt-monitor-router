/* Dashboard: ONU optical power distribution. One quiet chart, colour only
   where the reading needs attention. */
(function () {
  "use strict";

  var canvas = document.getElementById("powerChart");
  if (!canvas || typeof Chart === "undefined") {
    return;
  }

  var buckets = [
    { label: ">-5", min: -5, max: 99, tone: "ok" },
    { label: "-5..-15", min: -15, max: -5, tone: "ok" },
    { label: "-15..-20", min: -20, max: -15, tone: "ok" },
    { label: "-20..-24", min: -24, max: -20, tone: "ok" },
    { label: "-24..-27", min: -27, max: -24, tone: "warn" },
    { label: "-27..-30", min: -30, max: -27, tone: "bad" },
    { label: "<-30", min: -999, max: -30, tone: "bad" }
  ];

  var chart = null;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

  function token(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
  }

  function palette() {
    var dark = document.documentElement.getAttribute("data-bs-theme") === "dark";
    return {
      neutral: dark ? "#39424c" : "#c7ced5",
      warn: token("--accent-bright"),
      bad: token("--bad"),
      text: token("--text-2"),
      muted: token("--muted"),
      grid: dark ? "rgba(233,236,239,0.08)" : "rgba(18,26,34,0.08)"
    };
  }

  function draw(rows) {
    var colours = palette();
    var counts = buckets.map(function () { return 0; });
    rows.forEach(function (row) {
      var value = row.rx_power_dbm;
      if (value === null || value === undefined) { return; }
      for (var i = 0; i < buckets.length; i += 1) {
        if (value > buckets[i].min && value <= buckets[i].max) {
          counts[i] += 1;
          break;
        }
      }
    });

    if (!rows.length) {
      canvas.parentNode.innerHTML =
        '<p class="text-secondary small mb-0">No optical readings yet. ' +
        'Set the ONU optical OIDs in <a href="/settings">Settings</a>.</p>';
      return;
    }

    if (chart) {
      chart.data.datasets[0].data = counts;
      chart.data.datasets[0].backgroundColor = buckets.map(function (b) {
        return b.tone === "bad" ? colours.bad : (b.tone === "warn" ? colours.warn : colours.neutral);
      });
      chart.options.scales.x.ticks.color = colours.muted;
      chart.options.scales.y.ticks.color = colours.muted;
      chart.options.scales.x.title.color = colours.muted;
      chart.options.scales.y.grid.color = colours.grid;
      chart.update("none");
      return;
    }

    chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: buckets.map(function (b) { return b.label; }),
        datasets: [{
          label: "ONUs",
          data: counts,
          backgroundColor: buckets.map(function (b) {
            return b.tone === "bad" ? colours.bad : (b.tone === "warn" ? colours.warn : colours.neutral);
          }),
          borderRadius: 2,
          borderSkipped: false,
          maxBarThickness: 42
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 260 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: token("--surface"),
            titleColor: token("--text"),
            bodyColor: token("--text-2"),
            borderColor: token("--line-strong"),
            borderWidth: 1,
            padding: 8,
            displayColors: false,
            callbacks: {
              label: function (item) {
                return item.parsed.y + " ONUs";
              }
            }
          }
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: { precision: 0, color: colours.muted, font: { size: 11 } },
            grid: { color: colours.grid, drawBorder: false }
          },
          x: {
            ticks: { color: colours.muted, font: { size: 11 } },
            grid: { display: false },
            title: {
              display: true,
              text: "Rx power (dBm)",
              color: colours.muted,
              font: { size: 11 }
            }
          }
        }
      }
    });
  }

  fetch(window.POWER_DATA_URL || "/api/onu-power")
    .then(function (response) { return response.json(); })
    .then(function (rows) {
      draw(rows || []);
      document.addEventListener("olt:themechange", function () {
        draw(rows || []);
      });
    })
    .catch(function (err) {
      console.error("power chart failed", err);
    });
})();
