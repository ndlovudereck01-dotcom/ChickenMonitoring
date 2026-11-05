/* Live camera page: MJPEG stream, live metrics and the geometry editor. */
(function () {
  "use strict";

  var stage = document.querySelector("[data-live-stage]");
  if (!stage) return;

  var img = document.getElementById("live-feed");
  var canvas = document.getElementById("geometry-canvas");
  var overlay = document.getElementById("stage-overlay");
  var statusUrl = stage.getAttribute("data-stats-url");
  var streamUrl = stage.getAttribute("data-stream-url");
  var isRunning = stage.getAttribute("data-running") === "1";
  var editorEnabled = stage.getAttribute("data-editor") === "1";

  /* ------------------------------------------------------------- live feed */
  function connectStream() {
    if (!isRunning || !streamUrl) return;
    img.src = streamUrl + "?t=" + Date.now();
  }

  var retryTimer = null;
  img.addEventListener("load", function () {
    if (overlay) overlay.classList.add("is-hidden");
  });
  img.addEventListener("error", function () {
    if (!isRunning) return;
    if (overlay) overlay.classList.remove("is-hidden");
    if (retryTimer) window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(connectStream, 2500);
  });
  connectStream();

  /* --------------------------------------------------------------- metrics */
  var reloadTimer = null;
  var ended = false;

  function setText(name, value) {
    var nodes = document.querySelectorAll('[data-live="' + name + '"]');
    Array.prototype.forEach.call(nodes, function (node) {
      node.textContent = value;
    });
  }

  function pollMetrics() {
    if (!statusUrl) return;
    window.CV.getJSON(statusUrl)
      .then(function (payload) {
        var run = payload.run;
        var age = payload.frame_age;
        setText("frame-age", age === null || age === undefined ? "—" : age.toFixed(1) + "s ago");
        if (!run) {
          setText("occupancy", "0");
          setText("unique", "0");
          setText("in", "0");
          setText("out", "0");
          return;
        }
        setText("occupancy", window.CV.formatNumber(run.occupancy));
        setText("unique", window.CV.formatNumber(run.unique_birds));
        setText("in", window.CV.formatNumber(run.line_in));
        setText("out", window.CV.formatNumber(run.line_out));
        setText("fps", Number(run.processing_fps || 0).toFixed(1));
        setText("frames", window.CV.formatNumber(run.frames_processed));

        if (run.finished && !ended) {
          ended = true;
          reloadTimer = window.setTimeout(function () {
            window.location.reload();
          }, 1800);
          return;
        }
        window.setTimeout(pollMetrics, 2500);
      })
      .catch(function () {
        window.setTimeout(pollMetrics, 5000);
      });
  }
  pollMetrics();

  /* ------------------------------------------------------------- geometry */
  if (!canvas || !editorEnabled) return;

  var dataNode = document.getElementById("geometry-data");
  var saved = {};
  try {
    saved = JSON.parse((dataNode && dataNode.textContent) || "{}");
  } catch (error) {
    saved = {};
  }

  var drafts = {
    roi: Array.isArray(saved.roi) ? saved.roi.slice() : [],
    line: Array.isArray(saved.counting_line) ? saved.counting_line.slice() : [],
  };
  var mode = "roi";
  var editing = false;
  var invertBox = document.querySelector("[data-invert-line]");
  var roiOnlyBox = document.querySelector("[data-roi-only]");
  var statusLabel = document.querySelector("[data-geometry-status]");

  function stageRect() {
    return stage.getBoundingClientRect();
  }

  function imageRect() {
    var box = stageRect();
    var naturalWidth = img.naturalWidth || 0;
    var naturalHeight = img.naturalHeight || 0;
    if (!naturalWidth || !naturalHeight) {
      return { x: 0, y: 0, w: box.width, h: box.height };
    }
    var scale = Math.min(box.width / naturalWidth, box.height / naturalHeight);
    var w = naturalWidth * scale;
    var h = naturalHeight * scale;
    return { x: (box.width - w) / 2, y: (box.height - h) / 2, w: w, h: h };
  }

  function resizeCanvas() {
    var box = stageRect();
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(box.width * ratio));
    canvas.height = Math.max(1, Math.round(box.height * ratio));
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function draw() {
    var ctx = canvas.getContext("2d");
    var box = stageRect();
    ctx.clearRect(0, 0, box.width, box.height);
    if (!editing) return;
    var rect = imageRect();

    function px(point) {
      return [rect.x + point[0] * rect.w, rect.y + point[1] * rect.h];
    }

    // ROI polygon
    if (drafts.roi.length) {
      ctx.beginPath();
      drafts.roi.forEach(function (point, index) {
        var p = px(point);
        if (index === 0) ctx.moveTo(p[0], p[1]);
        else ctx.lineTo(p[0], p[1]);
      });
      if (drafts.roi.length >= 3) ctx.closePath();
      ctx.fillStyle = "rgba(34, 197, 129, 0.15)";
      if (drafts.roi.length >= 3) ctx.fill();
      ctx.strokeStyle = "#2fd08f";
      ctx.lineWidth = 2;
      ctx.stroke();
      drafts.roi.forEach(function (point) {
        var p = px(point);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 4, 0, Math.PI * 2);
        ctx.fillStyle = "#2fd08f";
        ctx.fill();
      });
    }

    // Counting line
    if (drafts.line.length) {
      ctx.beginPath();
      drafts.line.forEach(function (point, index) {
        var p = px(point);
        if (index === 0) ctx.moveTo(p[0], p[1]);
        else ctx.lineTo(p[0], p[1]);
      });
      ctx.strokeStyle = "#59c7ff";
      ctx.lineWidth = 2;
      ctx.stroke();
      drafts.line.forEach(function (point) {
        var p = px(point);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 4.5, 0, Math.PI * 2);
        ctx.fillStyle = "#59c7ff";
        ctx.fill();
      });
      if (drafts.line.length === 2) {
        var a = px(drafts.line[0]);
        var b = px(drafts.line[1]);
        ctx.fillStyle = "rgba(10, 22, 16, 0.72)";
        ctx.fillRect((a[0] + b[0]) / 2 - 44, (a[1] + b[1]) / 2 - 12, 88, 20);
        ctx.fillStyle = "#cfefff";
        ctx.font = "11px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("IN / OUT line", (a[0] + b[0]) / 2, (a[1] + b[1]) / 2 - 2);
      }
    }

    // Draft point hint
    if (mode === "roi" && drafts.roi.length && drafts.roi.length < 3) {
      var last = px(drafts.roi[drafts.roi.length - 1]);
      ctx.fillStyle = "#d9f5e6";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText("Add at least 3 points", last[0] + 8, last[1] - 8);
    }
  }

  function setEditing(next) {
    editing = next;
    canvas.hidden = !next;
    canvas.style.pointerEvents = next ? "auto" : "none";
    if (statusLabel && next) statusLabel.textContent = "Click the frame to place points";
    resizeCanvas();
  }

  canvas.addEventListener("click", function (event) {
    if (!editing) return;
    var box = stageRect();
    var rect = imageRect();
    var x = (event.clientX - box.left - rect.x) / rect.w;
    var y = (event.clientY - box.top - rect.y) / rect.h;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    var point = [Math.round(x * 10000) / 10000, Math.round(y * 10000) / 10000];
    if (mode === "roi") {
      if (drafts.roi.length >= 64) drafts.roi.shift();
      drafts.roi.push(point);
    } else {
      if (drafts.line.length >= 2) drafts.line = [point];
      else drafts.line.push(point);
    }
    draw();
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-geometry-mode]"), function (button) {
    button.addEventListener("click", function () {
      mode = button.getAttribute("data-geometry-mode");
      Array.prototype.forEach.call(
        document.querySelectorAll("[data-geometry-mode]"),
        function (other) {
          other.classList.toggle("is-active", other === button);
        }
      );
      setEditing(true);
    });
  });

  var clearButton = document.querySelector("[data-geometry-clear]");
  if (clearButton) {
    clearButton.addEventListener("click", function () {
      if (!editing) setEditing(true);
      drafts[mode] = [];
      if (statusLabel) statusLabel.textContent = "Cleared " + (mode === "roi" ? "area" : "line");
      draw();
    });
  }

  var saveButton = document.querySelector("[data-geometry-save]");
  if (saveButton) {
    saveButton.addEventListener("click", function () {
      if (!editing) setEditing(true);
      if (drafts.roi.length === 1 || drafts.roi.length === 2) {
        if (statusLabel) statusLabel.textContent = "An area needs at least 3 points";
        return;
      }
      if (drafts.line.length === 1) {
        if (statusLabel) statusLabel.textContent = "A counting line needs 2 points";
        return;
      }
      saveButton.disabled = true;
      if (statusLabel) statusLabel.textContent = "Saving…";
      window.CV.postJSON(stage.getAttribute("data-geometry-url"), {
        roi: drafts.roi.length >= 3 ? drafts.roi : null,
        counting_line: drafts.line.length === 2 ? drafts.line : null,
        invert_line: !!(invertBox && invertBox.checked),
        roi_only: !!(roiOnlyBox && roiOnlyBox.checked),
      })
        .then(function (result) {
          saveButton.disabled = false;
          if (result.ok && result.data.ok) {
            if (statusLabel) statusLabel.textContent = "Geometry saved";
          } else if (statusLabel) {
            statusLabel.textContent = "Could not save — check the points";
          }
        })
        .catch(function () {
          saveButton.disabled = false;
          if (statusLabel) statusLabel.textContent = "Network error while saving";
        });
    });
  }

  var editButton = document.querySelector("[data-geometry-edit]");
  if (editButton) {
    editButton.addEventListener("click", function () {
      setEditing(!editing);
      editButton.textContent = editing ? "Done editing" : "Edit geometry";
    });
  }

  if (window.ResizeObserver) {
    var observer = new ResizeObserver(function () {
      resizeCanvas();
    });
    observer.observe(stage);
  }
  window.addEventListener("orientationchange", resizeCanvas);
})();
