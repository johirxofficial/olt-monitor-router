/* OLT overview: "Poll now" and delete actions over the JSON API. */
(function () {
  "use strict";

  function toast(kind, text) {
    var box = document.getElementById("olt-alert");
    if (!box) { alert(text); return; }
    var el = document.createElement("div");
    el.className = "alert alert-" + kind + " shadow-sm py-2 px-3 mb-2";
    el.innerHTML = text;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }

  async function call(url, options) {
    var response = await fetch(url, options || {});
    var data = {};
    try { data = await response.json(); } catch (e) { /* empty body */ }
    if (!response.ok) {
      throw new Error((data && data.detail) || ("HTTP " + response.status));
    }
    return data;
  }

  document.querySelectorAll("[data-poll]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var name = button.getAttribute("data-poll");
      var original = button.innerHTML;
      button.disabled = true;
      button.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
      try {
        var data = await call("/api/olts/" + encodeURIComponent(name) + "/poll", { method: "POST" });
        toast("info", data.message || (name + ": polling"));
        // a full SNMP walk takes ~90 s - give the UI a moment before reloading
        setTimeout(function () { location.reload(); }, 6000);
      } catch (error) {
        toast("danger", "Poll failed: " + error.message);
        button.disabled = false;
        button.innerHTML = original;
      }
    });
  });

  document.querySelectorAll("[data-delete]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var id = button.getAttribute("data-delete");
      var name = button.getAttribute("data-name");
      if (!confirm("Delete OLT \"" + name + "\" and all data collected for it?\n\nThis cannot be undone.")) {
        return;
      }
      try {
        await call("/api/registry/" + encodeURIComponent(id), { method: "DELETE" });
        toast("success", "Deleted " + name);
        setTimeout(function () { location.reload(); }, 700);
      } catch (error) {
        toast("danger", "Delete failed: " + error.message);
      }
    });
  });
})();
