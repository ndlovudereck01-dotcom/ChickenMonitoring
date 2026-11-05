/* Global UI helpers: navigation drawer, flash messages, CSRF-aware fetch. */
(function () {
  "use strict";

  var sidebar = document.getElementById("sidebar");
  var toggle = document.querySelector("[data-nav-toggle]");

  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("is-open");
    });
    document.addEventListener("click", function (event) {
      if (!sidebar.classList.contains("is-open")) return;
      if (sidebar.contains(event.target) || toggle.contains(event.target)) return;
      sidebar.classList.remove("is-open");
    });
  }

  // Auto-fade informational flashes so they do not pile up.
  var flashes = document.querySelectorAll(".message.success, .message.info");
  Array.prototype.forEach.call(flashes, function (node) {
    window.setTimeout(function () {
      node.classList.add("is-fading");
      window.setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 320);
    }, 6500);
  });

  // Progress bars receive their width here because a strict CSP forbids inline styles.
  Array.prototype.forEach.call(document.querySelectorAll("[data-progress]"), function (node) {
    var value = Math.max(0, Math.min(100, Number(node.getAttribute("data-progress")) || 0));
    node.style.width = value + "%";
  });

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
        Accept: "application/json",
      },
      body: JSON.stringify(payload || {}),
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function getJSON(url) {
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    }).then(function (response) {
      if (!response.ok) throw new Error("Request failed: " + response.status);
      return response.json();
    });
  }

  function formatNumber(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString();
  }

  window.CV = {
    csrfToken: csrfToken,
    postJSON: postJSON,
    getJSON: getJSON,
    formatNumber: formatNumber,
  };
})();
