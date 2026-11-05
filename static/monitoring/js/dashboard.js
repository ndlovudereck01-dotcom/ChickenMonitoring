/* Dashboard population trend. */
(function () {
  "use strict";
  var dataNode = document.getElementById("dashboard-trend-data");
  var canvas = document.getElementById("dashboard-trend");
  if (!dataNode || !canvas) return;

  var series = [];
  try {
    series = JSON.parse(dataNode.textContent || "[]");
  } catch (error) {
    series = [];
  }
  series = series.map(function (line, index) {
    return {
      label: line.label,
      color: window.CVChart.palette[index % window.CVChart.palette.length],
      points: (line.points || []).map(function (point) {
        return { t: point.t, v: point.v };
      }),
    };
  });

  var hasPoints = series.some(function (line) {
    return line.points.length > 0;
  });
  if (!hasPoints) {
    var empty = document.querySelector("[data-trend-empty]");
    if (empty) empty.hidden = false;
    canvas.hidden = true;
    return;
  }
  window.CVChart.mount(canvas, series, {
    fill: series.length === 1,
    yMax: null,
    emptyText: "No live samples yet",
  });
})();
