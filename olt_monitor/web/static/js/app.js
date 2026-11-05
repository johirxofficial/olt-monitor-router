/* Shared front-end helpers: clock, theme toggle, auto refresh. */
(function () {
  "use strict";

  // ---- theme -------------------------------------------------------------
  var root = document.documentElement;
  var toggle = document.getElementById("theme-toggle");
  var icon = document.getElementById("theme-icon");

  function paintTheme(theme) {
    root.setAttribute("data-bs-theme", theme);
    if (icon) {
      icon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
    }
    if (toggle) {
      toggle.setAttribute("title", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    }
  }

  paintTheme(root.getAttribute("data-bs-theme") || "light");

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      paintTheme(next);
      try { localStorage.setItem("olt-theme", next); } catch (e) { /* ignore */ }
    });
  }

  // ---- clock -------------------------------------------------------------
  var clock = document.getElementById("clock");
  function tick() {
    if (clock) {
      clock.textContent = new Date().toLocaleTimeString();
    }
  }
  tick();
  setInterval(tick, 1000);

  // ---- auto refresh ------------------------------------------------------
  // Never reload while the user is typing in a filter box and never while a
  // modal or dropdown is open.
  var body = document.body;
  var seconds = parseInt(body.getAttribute("data-refresh") || "30", 10);
  if (!isNaN(seconds) && seconds > 0) {
    setInterval(function () {
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
