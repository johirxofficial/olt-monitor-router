/* Shared front-end helpers: theme, navigation rail, clock, auto refresh. */
(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;

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

  // ---- auto refresh ------------------------------------------------------
  // Never reload while the user is typing in a filter box and never while a
  // modal or dropdown is open.
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
