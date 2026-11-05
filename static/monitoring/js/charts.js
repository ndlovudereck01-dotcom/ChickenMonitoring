/* Lightweight canvas line chart (no external dependencies, CSP friendly). */
(function () {
  "use strict";

  var PALETTE = ["#167a52", "#e9a11b", "#2f7fd1", "#b4529a", "#5c8f3f"];

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || "").trim() || fallback;
  }

  function niceMax(value) {
    if (!value || value < 1) return 4;
    var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    var normalised = value / magnitude;
    var step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
    return step * magnitude;
  }

  function formatTime(seconds) {
    var total = Math.max(0, Math.round(seconds));
    var minutes = Math.floor(total / 60);
    var secs = total % 60;
    if (minutes >= 60) {
      var hours = Math.floor(minutes / 60);
      return hours + "h" + String(minutes % 60).padStart(2, "0");
    }
    return minutes + ":" + String(secs).padStart(2, "0");
  }

  function prepare(canvas) {
    var rect = canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx: ctx, width: rect.width, height: rect.height };
  }

  function render(canvas, series, options) {
    if (!canvas) return;
    options = options || {};
    var prepared = prepare(canvas);
    if (!prepared) return;
    var ctx = prepared.ctx;
    var width = prepared.width;
    var height = prepared.height;

    var colours = {
      grid: cssVar("--line", "#e1e9e3"),
      axis: cssVar("--muted", "#64776b"),
      ink: cssVar("--ink-soft", "#33453b"),
    };

    var pad = { top: 12, right: 12, bottom: 24, left: 40 };
    var plotW = Math.max(10, width - pad.left - pad.right);
    var plotH = Math.max(10, height - pad.top - pad.bottom);

    ctx.clearRect(0, 0, width, height);

    var allPoints = [];
    series.forEach(function (line) {
      (line.points || []).forEach(function (point) {
        allPoints.push(point);
      });
    });
    if (allPoints.length === 0) {
      ctx.fillStyle = colours.axis;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(options.emptyText || "No data yet", width / 2, height / 2);
      return;
    }

    var maxT = 0;
    var maxV = 0;
    allPoints.forEach(function (point) {
      if (point.t > maxT) maxT = point.t;
      if (point.v > maxV) maxV = point.v;
    });
    var yMax = options.yMax || niceMax(maxV * 1.15 || 1);
    if (maxT <= 0) maxT = 1;

    function x(t) {
      return pad.left + (t / maxT) * plotW;
    }
    function y(v) {
      return pad.top + plotH - (Math.min(v, yMax) / yMax) * plotH;
    }

    // Grid + y labels
    ctx.font = "11px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    var steps = 4;
    for (var i = 0; i <= steps; i += 1) {
      var value = (yMax / steps) * i;
      var gy = y(value);
      ctx.strokeStyle = colours.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, gy + 0.5);
      ctx.lineTo(pad.left + plotW, gy + 0.5);
      ctx.stroke();
      ctx.fillStyle = colours.axis;
      ctx.fillText(String(Math.round(value)), pad.left - 8, gy);
    }

    // X labels
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var ticks = 4;
    for (var j = 0; j <= ticks; j += 1) {
      var t = (maxT / ticks) * j;
      ctx.fillStyle = colours.axis;
      ctx.fillText(formatTime(t), x(t), pad.top + plotH + 7);
    }

    // Series
    series.forEach(function (line, index) {
      var points = (line.points || []).slice();
      if (points.length === 0) return;
      points.sort(function (a, b) {
        return a.t - b.t;
      });
      var colour = line.color || PALETTE[index % PALETTE.length];
      if (points.length === 1) points.push({ t: points[0].t + 0.5, v: points[0].v });

      if (options.fill && index === 0) {
        var gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
        gradient.addColorStop(0, "rgba(22, 122, 82, 0.22)");
        gradient.addColorStop(1, "rgba(22, 122, 82, 0.01)");
        ctx.beginPath();
        ctx.moveTo(x(points[0].t), y(points[0].v));
        points.forEach(function (point) {
          ctx.lineTo(x(point.t), y(point.v));
        });
        ctx.lineTo(x(points[points.length - 1].t), pad.top + plotH);
        ctx.lineTo(x(points[0].t), pad.top + plotH);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();
      }

      ctx.beginPath();
      points.forEach(function (point, pointIndex) {
        var px = x(point.t);
        var py = y(point.v);
        if (pointIndex === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = colour;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();

      var last = points[points.length - 1];
      ctx.beginPath();
      ctx.arc(x(last.t), y(last.v), 3, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.fill();
    });
  }

  function renderLegend(container, series) {
    if (!container) return;
    container.textContent = "";
    series.forEach(function (line, index) {
      var key = document.createElement("span");
      key.className = "key";
      var swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = line.color || PALETTE[index % PALETTE.length];
      var label = document.createElement("span");
      label.textContent = line.label || "Series " + (index + 1);
      key.appendChild(swatch);
      key.appendChild(label);
      container.appendChild(key);
    });
  }

  function mount(canvas, series, options) {
    options = options || {};
    var legendId = canvas.getAttribute("data-legend");
    render(canvas, series, options);
    if (legendId) renderLegend(document.getElementById(legendId), series);
    if (window.ResizeObserver) {
      var frame = null;
      var observer = new ResizeObserver(function () {
        if (frame) window.cancelAnimationFrame(frame);
        frame = window.requestAnimationFrame(function () {
          render(canvas, series, options);
        });
      });
      observer.observe(canvas.parentElement || canvas);
    }
  }

  window.CVChart = { render: render, mount: mount, palette: PALETTE };
})();
