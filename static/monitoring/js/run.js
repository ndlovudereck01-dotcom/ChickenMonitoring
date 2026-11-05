/* Run detail page: chart + live progress polling. */
(function () {
  "use strict";

  var chartData = document.getElementById("run-chart-data");
  var chartCanvas = document.getElementById("run-chart");
  if (chartData && chartCanvas) {
    var points = [];
    try {
      points = JSON.parse(chartData.textContent || "[]");
    } catch (error) {
      points = [];
    }
    window.CVChart.mount(
      chartCanvas,
      [
        {
          label: "Birds in frame",
          color: window.CVChart.palette[0],
          points: points.map(function (point) {
            return { t: point.timestamp_seconds, v: point.occupancy };
          }),
        },
        {
          label: "Unique birds (cumulative)",
          color: window.CVChart.palette[2],
          points: points.map(function (point) {
            return { t: point.timestamp_seconds, v: point.unique_total };
          }),
        },
      ],
      { fill: true, yMax: null }
    );
  }

  var pollNode = document.querySelector("[data-run-progress]");
  if (!pollNode) return;

  var url = pollNode.getAttribute("data-progress-url");
  var finishedReload = false;

  function setField(name, value, text) {
    var nodes = document.querySelectorAll('[data-live="' + name + '"]');
    Array.prototype.forEach.call(nodes, function (node) {
      if (text !== undefined) node.textContent = text;
      else node.textContent = window.CV.formatNumber(value);
    });
  }

  function poll() {
    window.CV.getJSON(url)
      .then(function (payload) {
        var run = payload;
        setField("unique", run.unique_birds);
        setField("peak", run.peak_occupancy);
        setField("occupancy", run.occupancy);
        setField("in", run.line_in);
        setField("out", run.line_out);
        setField("fps", run.processing_fps, Number(run.processing_fps || 0).toFixed(1));
        setField("frames", run.frames_processed);
        setField("elapsed", run.elapsed, run.elapsed);

        var bar = document.querySelector("[data-progress-bar]");
        if (bar) bar.style.width = Math.max(0, Math.min(100, run.progress)) + "%";
        var label = document.querySelector("[data-progress-label]");
        if (label) label.textContent = run.progress + "%";

        if (run.finished && !finishedReload) {
          finishedReload = true;
          window.setTimeout(function () {
            window.location.reload();
          }, 1200);
          return;
        }
        window.setTimeout(poll, 2500);
      })
      .catch(function () {
        window.setTimeout(poll, 5000);
      });
  }

  poll();
})();
