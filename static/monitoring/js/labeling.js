/* Annotation editor: correct YOLO boxes in place, on the dataset the trainer reads. */
(function () {
  "use strict";

  var root = document.getElementById("label-app");
  var canvas = document.getElementById("label-canvas");
  if (!root || !canvas || !window.CV) return;

  var ctx = canvas.getContext("2d");
  var imagesUrl = root.getAttribute("data-images-url");
  var saveUrl = root.getAttribute("data-save-url");

  var counter = root.querySelector("[data-label-counter]");
  var stateBadge = root.querySelector("[data-label-state]");
  var fileLabel = root.querySelector("[data-label-file]");
  var info = root.querySelector("[data-label-info]");
  var prevBtn = root.querySelector("[data-label-prev]");
  var nextBtn = root.querySelector("[data-label-next]");
  var deleteBtn = root.querySelector("[data-label-delete]");
  var saveBtn = root.querySelector("[data-label-save]");
  var approveBtn = root.querySelector("[data-label-approve]");

  var ACCENT = "#00c8ff";
  var ACCENT_SOFT = "rgba(0, 200, 255, 0.16)";
  var HANDLE = 5;

  var items = [];
  var index = 0;
  var boxes = [];
  var selected = -1;
  var dirty = false;
  var busy = false;
  var image = null;
  var view = { scale: 1, x: 0, y: 0, width: 960, height: 540 };
  var drag = null;

  // ------------------------------------------------------------------ helpers
  // The API sends boxes as [cx, cy, w, h]; editing works on objects.
  function clone(box) {
    if (Array.isArray(box)) {
      return { cx: box[0], cy: box[1], w: box[2], h: box[3] };
    }
    return { cx: box.cx, cy: box.cy, w: box.w, h: box.h };
  }

  function round6(value) {
    return Math.round(value * 1e6) / 1e6;
  }

  function clamp01(value) {
    return Math.min(1, Math.max(0, value));
  }

  function setInfo(text) {
    if (info) info.textContent = text;
  }

  function markDirty() {
    dirty = true;
    if (stateBadge) {
      stateBadge.textContent = "unsaved changes";
      stateBadge.className = "badge is-amber";
    }
  }

  function localPoint(event) {
    var rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function toNorm(point) {
    var width = image ? image.naturalWidth : 1;
    var height = image ? image.naturalHeight : 1;
    return {
      x: clamp01((point.x - view.x) / view.scale / width),
      y: clamp01((point.y - view.y) / view.scale / height),
    };
  }

  function boxRect(box) {
    var width = image.naturalWidth;
    var height = image.naturalHeight;
    return {
      x: view.x + (box.cx - box.w / 2) * width * view.scale,
      y: view.y + (box.cy - box.h / 2) * height * view.scale,
      w: box.w * width * view.scale,
      h: box.h * height * view.scale,
    };
  }

  function clampBox(box) {
    box.w = Math.min(1, Math.max(0.002, box.w));
    box.h = Math.min(1, Math.max(0.002, box.h));
    box.cx = Math.min(1 - box.w / 2, Math.max(box.w / 2, box.cx));
    box.cy = Math.min(1 - box.h / 2, Math.max(box.h / 2, box.cy));
    return box;
  }

  // ------------------------------------------------------------------- render
  function resizeCanvas() {
    var imageWidth = image ? image.naturalWidth : 960;
    var imageHeight = image ? image.naturalHeight : 540;
    var cssWidth = Math.max(240, Math.min(canvas.parentElement.clientWidth, 1600));
    var cssHeight = Math.round((cssWidth * imageHeight) / imageWidth);
    var maxHeight = Math.round(window.innerHeight * 0.62);
    if (cssHeight > maxHeight) cssHeight = maxHeight;

    var dpr = window.devicePixelRatio || 1;
    canvas.style.width = cssWidth + "px";
    canvas.style.height = cssHeight + "px";
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    var scale = Math.min(cssWidth / imageWidth, cssHeight / imageHeight);
    view.scale = scale;
    view.width = cssWidth;
    view.height = cssHeight;
    view.x = (cssWidth - imageWidth * scale) / 2;
    view.y = (cssHeight - imageHeight * scale) / 2;
    render();
  }

  function drawBox(box, position, isSelected) {
    var rect = boxRect(box);
    ctx.lineWidth = isSelected ? 3 : 2;
    ctx.strokeStyle = ACCENT;
    if (isSelected) {
      ctx.fillStyle = ACCENT_SOFT;
      ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    }
    ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);

    ctx.font = "12px system-ui, sans-serif";
    var label = String(position + 1);
    var metrics = ctx.measureText(label);
    var labelWidth = metrics.width + 8;
    ctx.fillStyle = "rgba(10, 18, 16, 0.78)";
    ctx.fillRect(rect.x, Math.max(0, rect.y - 16), labelWidth, 15);
    ctx.fillStyle = ACCENT;
    ctx.fillText(label, rect.x + 4, Math.max(11, rect.y - 5));

    if (!isSelected) return;
    var corners = [
      [rect.x, rect.y],
      [rect.x + rect.w, rect.y],
      [rect.x + rect.w, rect.y + rect.h],
      [rect.x, rect.y + rect.h],
    ];
    for (var i = 0; i < corners.length; i += 1) {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(
        corners[i][0] - HANDLE,
        corners[i][1] - HANDLE,
        HANDLE * 2,
        HANDLE * 2
      );
      ctx.strokeStyle = ACCENT;
      ctx.lineWidth = 1;
      ctx.strokeRect(
        corners[i][0] - HANDLE,
        corners[i][1] - HANDLE,
        HANDLE * 2,
        HANDLE * 2
      );
    }
  }

  function render() {
    ctx.fillStyle = "#0d1412";
    ctx.fillRect(0, 0, view.width, view.height);
    if (image && image.complete && image.naturalWidth) {
      ctx.drawImage(
        image,
        view.x,
        view.y,
        image.naturalWidth * view.scale,
        image.naturalHeight * view.scale
      );
    }
    for (var i = 0; i < boxes.length; i += 1) {
      drawBox(boxes[i], i, i === selected);
    }
  }

  // -------------------------------------------------------------- interaction
  function hitHandle(point, box) {
    var rect = boxRect(box);
    var corners = [
      [rect.x, rect.y],
      [rect.x + rect.w, rect.y],
      [rect.x + rect.w, rect.y + rect.h],
      [rect.x, rect.y + rect.h],
    ];
    for (var i = 0; i < corners.length; i += 1) {
      if (
        Math.abs(point.x - corners[i][0]) <= HANDLE + 4 &&
        Math.abs(point.y - corners[i][1]) <= HANDLE + 4
      ) {
        return i;
      }
    }
    return -1;
  }

  function hitBox(point) {
    for (var i = boxes.length - 1; i >= 0; i -= 1) {
      var rect = boxRect(boxes[i]);
      if (
        point.x >= rect.x &&
        point.x <= rect.x + rect.w &&
        point.y >= rect.y &&
        point.y <= rect.y + rect.h
      ) {
        return i;
      }
    }
    return -1;
  }

  function updateChrome() {
    if (counter) counter.textContent = (items.length ? index + 1 : 0) + " / " + items.length;
    var item = items[index];
    if (stateBadge && item) {
      if (dirty) {
        stateBadge.textContent = "unsaved changes";
        stateBadge.className = "badge is-amber";
      } else if (item.reviewed) {
        stateBadge.textContent = "reviewed";
        stateBadge.className = "badge is-online";
      } else {
        stateBadge.textContent = "not reviewed";
        stateBadge.className = "badge is-muted";
      }
    }
    if (prevBtn) prevBtn.disabled = index <= 0;
    if (nextBtn) nextBtn.disabled = index >= items.length - 1;
    if (deleteBtn) deleteBtn.disabled = selected < 0;
    if (saveBtn) saveBtn.disabled = !items.length;
    if (approveBtn) approveBtn.disabled = !items.length;
    if (fileLabel && item) fileLabel.textContent = item.split + "/" + item.stem;
    if (item) {
      setInfo(
        boxes.length +
          (boxes.length === 1 ? " box" : " boxes") +
          (item.reviewed ? " · approved" : "")
      );
    }
  }

  canvas.addEventListener("pointerdown", function (event) {
    if (!image || busy || !image.complete) return;
    canvas.focus();
    var point = localPoint(event);

    if (selected >= 0) {
      var handle = hitHandle(point, boxes[selected]);
      if (handle >= 0) {
        drag = {
          mode: "resize",
          handle: handle,
          start: toNorm(point),
          box: clone(boxes[selected]),
        };
        canvas.setPointerCapture(event.pointerId);
        event.preventDefault();
        return;
      }
    }

    var hit = hitBox(point);
    if (hit >= 0) {
      selected = hit;
      drag = {
        mode: "move",
        start: toNorm(point),
        box: clone(boxes[selected]),
        index: selected,
      };
      render();
      updateChrome();
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
      return;
    }

    var origin = toNorm(point);
    boxes.push({ cx: origin.x, cy: origin.y, w: 0.002, h: 0.002 });
    selected = boxes.length - 1;
    drag = { mode: "new", originX: origin.x, originY: origin.y };
    dirty = true;
    render();
    updateChrome();
    canvas.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  canvas.addEventListener("pointermove", function (event) {
    if (!drag || !image) return;
    var point = toNorm(localPoint(event));
    var box = boxes[selected];
    if (!box) return;

    if (drag.mode === "new") {
      var x1 = Math.min(drag.originX, point.x);
      var x2 = Math.max(drag.originX, point.x);
      var y1 = Math.min(drag.originY, point.y);
      var y2 = Math.max(drag.originY, point.y);
      box.cx = (x1 + x2) / 2;
      box.cy = (y1 + y2) / 2;
      box.w = Math.max(0.002, x2 - x1);
      box.h = Math.max(0.002, y2 - y1);
    } else if (drag.mode === "move") {
      box.cx = drag.box.cx + (point.x - drag.start.x);
      box.cy = drag.box.cy + (point.y - drag.start.y);
      clampBox(box);
    } else if (drag.mode === "resize") {
      var original = drag.box;
      var left = original.cx - original.w / 2;
      var right = original.cx + original.w / 2;
      var top = original.cy - original.h / 2;
      var bottom = original.cy + original.h / 2;
      if (drag.handle === 0) {
        left = Math.min(point.x, right - 0.004);
        top = Math.min(point.y, bottom - 0.004);
      } else if (drag.handle === 1) {
        right = Math.max(point.x, left + 0.004);
        top = Math.min(point.y, bottom - 0.004);
      } else if (drag.handle === 2) {
        right = Math.max(point.x, left + 0.004);
        bottom = Math.max(point.y, top + 0.004);
      } else {
        left = Math.min(point.x, right - 0.004);
        bottom = Math.max(point.y, top + 0.004);
      }
      box.cx = (left + right) / 2;
      box.cy = (top + bottom) / 2;
      box.w = Math.min(1, right - left);
      box.h = Math.min(1, bottom - top);
      clampBox(box);
    }

    dirty = true;
    render();
  });

  function endDrag(event) {
    if (!drag) return;
    if (drag.mode === "new") {
      var box = boxes[selected];
      if (box && (box.w < 0.004 || box.h < 0.004)) {
        boxes.splice(selected, 1);
        selected = -1;
        dirty = boxes.length !== items[index].boxes.length;
      }
    }
    drag = null;
    if (event && canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    markDirty();
    render();
    updateChrome();
  }

  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  canvas.addEventListener("dblclick", function () {
    if (selected < 0) return;
    boxes.splice(selected, 1);
    selected = -1;
    markDirty();
    render();
    updateChrome();
  });

  // ---------------------------------------------------------------- data flow
  function updateSummary(summary) {
    if (!summary) return;
    var mapping = {
      images: summary.images,
      reviewed: summary.reviewed,
      pending: summary.pending,
      boxes: summary.boxes,
    };
    Object.keys(mapping).forEach(function (key) {
      var node = root.querySelector('[data-label-stat="' + key + '"]');
      if (node && mapping[key] !== undefined && mapping[key] !== null) {
        node.textContent = mapping[key];
      }
    });
  }

  function load(nextIndex) {
    if (!items.length) return;
    index = Math.max(0, Math.min(items.length - 1, nextIndex));
    var item = items[index];
    boxes = (item.boxes || []).map(clone);
    selected = -1;
    dirty = false;
    drag = null;
    image = new Image();
    image.onload = function () {
      resizeCanvas();
      updateChrome();
    };
    image.onerror = function () {
      setInfo("Could not load this frame — run build_dataset again.");
    };
    image.src = item.url;
    updateChrome();
    setInfo("Loading frame…");
  }

  function save(reviewed) {
    if (!items.length || busy) return Promise.resolve(false);
    var item = items[index];
    busy = true;
    setInfo(reviewed ? "Approving…" : "Saving…");
    var payload = {
      split: item.split,
      stem: item.stem,
      reviewed: !!reviewed,
      boxes: boxes.map(function (box) {
        return [round6(box.cx), round6(box.cy), round6(box.w), round6(box.h)];
      }),
    };
    return window.CV.postJSON(saveUrl, payload)
      .then(function (result) {
        busy = false;
        if (!result.ok) {
          setInfo("Save failed: " + ((result.data && result.data.error) || result.status));
          return false;
        }
        item.boxes = result.data.boxes;
        item.reviewed = result.data.reviewed;
        boxes = item.boxes.map(clone);
        selected = -1;
        dirty = false;
        updateSummary(result.data.summary);
        updateChrome();
        if (reviewed) setInfo("Approved — ready for training");
        return true;
      })
      .catch(function () {
        busy = false;
        setInfo("Save failed: network error");
        return false;
      });
  }

  function go(delta) {
    if (busy) return;
    var target = index + delta;
    if (target < 0 || target >= items.length) return;
    if (dirty) {
      save(false).then(function (ok) {
        if (ok) load(target);
      });
    } else {
      load(target);
    }
  }

  function approve() {
    if (busy) return;
    save(true).then(function (ok) {
      if (ok && index < items.length - 1) load(index + 1);
    });
  }

  if (prevBtn) prevBtn.addEventListener("click", function () { go(-1); });
  if (nextBtn) nextBtn.addEventListener("click", function () { go(1); });
  if (saveBtn) saveBtn.addEventListener("click", function () { save(false); });
  if (approveBtn) approveBtn.addEventListener("click", approve);
  if (deleteBtn) {
    deleteBtn.addEventListener("click", function () {
      if (selected < 0) return;
      boxes.splice(selected, 1);
      selected = -1;
      markDirty();
      render();
      updateChrome();
    });
  }

  document.addEventListener("keydown", function (event) {
    var tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      go(1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      go(-1);
    } else if (event.key === "Delete" || event.key === "Backspace") {
      if (selected < 0) return;
      event.preventDefault();
      boxes.splice(selected, 1);
      selected = -1;
      markDirty();
      render();
      updateChrome();
    } else if (event.key === "s" || event.key === "S") {
      event.preventDefault();
      save(false);
    } else if (event.key === "a" || event.key === "A") {
      event.preventDefault();
      approve();
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  window.addEventListener("resize", function () {
    if (image && image.complete) resizeCanvas();
  });

  window.CV.getJSON(imagesUrl)
    .then(function (data) {
      items = data.images || [];
      updateSummary(data.summary);
      if (!items.length) {
        setInfo("No frames in the dataset yet.");
        updateChrome();
        return;
      }
      var firstPending = 0;
      for (var i = 0; i < items.length; i += 1) {
        if (!items[i].reviewed) { firstPending = i; break; }
      }
      load(firstPending);
    })
    .catch(function () {
      setInfo("Could not load the dataset — check the server log.");
    });
})();
