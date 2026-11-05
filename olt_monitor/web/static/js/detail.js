/* Detail pages: draw an ONU optical-power chart over the last N hours. */
(function () {
  "use strict";

  var canvas = document.getElementById("onu-chart");
  if (!canvas || typeof Chart === "undefined") {
    return;
  }

  var olt = canvas.getAttribute("data-olt") || "";
  var ifindex = canvas.getAttribute("data-ifindex") || "";
  if (!ifindex) {
    return;
  }

  fetch("/api/trend/onu/" + encodeURIComponent(ifindex) + "?olt=" + encodeURIComponent(olt) + "&hours=24")
    .then(function (r) { return r.json(); })
    .then(function (rows) {
      rows = (rows || []).slice().reverse();  // the API returns newest first
      if (!rows.length) {
        canvas.parentNode.insertAdjacentHTML(
          "beforeend",
          '<p class="small text-secondary mb-0 mt-2">No history yet - it fills up as the OLT is polled.</p>'
        );
        canvas.remove();
        return;
      }

      // Keep the chart light even if history retention is long.  The API
      // already caps at 288 rows, but a bigger window / shorter poll interval
      // should never make the canvas slow.
      if (rows.length > 360) {
        var step = Math.ceil(rows.length / 360);
        rows = rows.filter(function (_, index) {
          return index % step === 0 || index === rows.length - 1;
        });
      }

      var labels = rows.map(function (row) {
        return String(row.ts || "").replace("T", " ").slice(5, 16);
      });      var rx = rows.map(function (row) { return row.rx_power_dbm; });
      var tx = rows.map(function (row) { return row.tx_power_dbm; });

      new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Rx dBm",
              data: rx,
              borderColor: "#0d6efd",
              backgroundColor: "rgba(13,110,253,0.08)",
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.25,
              spanGaps: true
            },
            {
              label: "Tx dBm",
              data: tx,
              borderColor: "#198754",
              backgroundColor: "rgba(25,135,84,0.08)",
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.25,
              spanGaps: true
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            y: { title: { display: true, text: "dBm" }, ticks: { maxTicksLimit: 6 } },
            x: { ticks: { maxTicksLimit: 8, maxRotation: 0, autoSkip: true } }
          },
          plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } }
        }
      });
    })
    .catch(function () {
      /* keep the page usable if the chart fails */
    });
})();
