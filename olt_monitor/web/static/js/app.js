/* Shared front-end helpers: theme, navigation rail, clock, manual poll, auto refresh. */
(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  var pollTimer = null;

  // ---- theme -------------------------------------------------------------
  var toggle = document.getElementById("theme-toggle");
  var icon = document.getElementById("theme-icon");

  function paintTheme(theme) {
    root.setAttribute("data-bs-theme", theme);
    if (icon) {
      icon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon";
    }
    if (toggle) {
      toggle.setAttribute(
        "title",
        theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
      );
    }
    document.dispatchEvent(
      new CustomEvent("olt:themechange", { detail: { theme: theme } })
    );
  }

  paintTheme(root.getAttribute("data-bs-theme") || "light");

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      paintTheme(next);
      try { localStorage.setItem("olt-theme", next); } catch (e) { /* ignore */ }
    });
  }

  // ---- navigation rail (small screens) -----------------------------------
  function setSidebar(open) {
    body.classList.toggle("sidebar-open", open);
    var button = document.querySelector("[data-sidebar-open]");
    if (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  document.querySelectorAll("[data-sidebar-open]").forEach(function (node) {
    node.addEventListener("click", function () { setSidebar(true); });
  });
  document.querySelectorAll("[data-sidebar-close]").forEach(function (node) {
    node.addEventListener("click", function () { setSidebar(false); });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { setSidebar(false); }
  });
  window.addEventListener("resize", function () {
    if (window.innerWidth >= 1100) { setSidebar(false); }
  });

  // ---- clock -------------------------------------------------------------
  var clock = document.getElementById("clock");
  function tick() {
    if (clock) {
      clock.textContent = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit"
      });
    }
  }
  tick();
  setInterval(tick, 30000);

  // ---- manual poll of every device ---------------------------------------
  // One button, one request: the scheduler starts a fresh poll for every
  // enabled OLT and router and skips any device that is already walking.
  function pollNote(text, busy) {
    document.querySelectorAll("[data-poll-status]").forEach(function (node) {
      node.textContent = text || "";
      node.classList.toggle("is-busy", !!busy);
    });
  }

  function stopPollWatch() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function watchPoll(seed) {
    if (pollTimer) { return; }
    var sawActive = !!seed;
    pollTimer = window.setInterval(function () {
      fetch("/api/poll-status", { headers: { "Accept": "application/json" } })
        .then(function (response) { return response.json(); })
        .then(function (state) {
          var count = state.count || 0;
          if (count > 0) {
            sawActive = true;
            pollNote("Polling " + count + " device" + (count === 1 ? "" : "s") + "…", true);
            return;
          }
          stopPollWatch();
          // Keep an open editor or filter untouched; the user can reload when done.
          var editing = document.activeElement;
          var typing = editing && (
            editing.tagName === "INPUT" ||
            editing.tagName === "SELECT" ||
            editing.tagName === "TEXTAREA"
          );
          if (sawActive && !typing && !document.querySelector(".modal.show")) {
            window.location.reload();
          } else {
            pollNote("Poll finished", false);
            window.setTimeout(function () { pollNote("", false); }, 4000);
          }
        })
        .catch(function () {
          stopPollWatch();
          pollNote("", false);
        });
    }, 5000);
  }

  function pollAll(button) {
    if (!button || button.disabled) { return; }
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML =
      '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Starting';
    pollNote("Requesting a full poll…", true);
    fetch("/api/poll-all", { method: "POST", headers: { "Accept": "application/json" } })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, body: payload };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          throw new Error(result.body.detail || "could not start the poll");
        }
        var started = ((result.body.result || {}).started || []).length;
        var running = ((result.body.result || {}).running || []).length;
        pollNote(result.body.message || "Poll requested", started > 0 || running > 0);
        if (started || running) {
          watchPoll();
        } else {
          window.setTimeout(function () { pollNote("", false); }, 4000);
        }
      })
      .catch(function (error) {
        pollNote(error && error.message ? error.message : String(error), false);
      })
      .finally(function () {
        button.disabled = false;
        button.innerHTML = original;
      });
  }

  document.querySelectorAll("[data-poll-all]").forEach(function (button) {
    button.addEventListener("click", function () { pollAll(button); });
  });

  window.OLT = window.OLT || {};
  window.OLT.pollAll = pollAll;

  // Show a poll that is already running (scheduled or started from another tab).
  if (document.querySelector("[data-poll-status]")) {
    fetch("/api/poll-status", { headers: { "Accept": "application/json" } })
      .then(function (response) { return response.json(); })
      .then(function (state) {
        var count = state.count || 0;
        if (count > 0) {
          pollNote("Polling " + count + " device" + (count === 1 ? "" : "s") + "…", true);
          watchPoll(true);
        }
      })
      .catch(function () { /* the page still works without the live note */ });
  }

  // ---- auto refresh ------------------------------------------------------
  // Never reload while the user is typing, while a popup is open, or while a
  // manual poll is still running (the poll watcher reloads when it finishes).
  var seconds = parseInt(body.getAttribute("data-refresh") || "30", 10);
  if (!isNaN(seconds) && seconds > 0) {
    setInterval(function () {
      if (pollTimer) { return; }
      var el = document.activeElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA")) {
        return;
      }
      var popupOpen = document.querySelector(".dropdown-menu.show");
      if (popupOpen || document.querySelector(".modal.show")) {
        return;
      }
      window.location.reload();
    }, seconds * 1000);
  }
})();
