/* Dashboard: ONU optical power distribution chart. */
(function () {
  "use strict";

  var canvas = document.getElementById("powerChart");
  if (!canvas || typeof Chart === "undefined") {
    return;
  }

  var buckets = [
    { label: "> -5", min: -5, max: 99 },
    { label: "-5..-15", min: -15, max: -5 },
    { label: "-15..-20", min: -20, max: -15 },
    { label: "-20..-24", min: -24, max: -20 },
    { label: "-24..-27", min: -27, max: -24 },
    { label: "-27..-30", min: -30, max: -27 },
    { label: "< -30", min: -999, max: -30 }
  ];

  fetch(window.POWER_DATA_URL || "/api/onu-power")
    .then(function (response) { return response.json(); })
    .then(function (rows) {
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
          '<div class="text-secondary small">No optical readings yet. ' +
          'Configure <code>olt.onu_tables</code> in config.yaml (see Settings).</div>';
        return;
      }

      new Chart(canvas, {
        type: "bar",
        data: {
          labels: buckets.map(function (b) { return b.label; }),
          datasets: [{
            label: "ONUs",
            data: counts,
            backgroundColor: ["#0d6efd", "#198754", "#20c997", "#ffc107", "#fd7e14", "#dc3545", "#6f42c1"]
          }]
        },
        options: {
          plugins: { legend: { display: false } },
          scales: {
            y: { beginAtZero: true, ticks: { precision: 0 } },
            x: { title: { display: true, text: "Rx power (dBm)" } }
          }
        }
      });
    })
    .catch(function (err) {
      console.error("power chart failed", err);
    });
})();
