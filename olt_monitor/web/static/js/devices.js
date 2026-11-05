/* Devices page: add / edit / delete / enable / test - all over the JSON API. */
(function () {
  "use strict";

  var DATA = { olts: [], routers: [], brands: [], telnet_profiles: [] };
  var raw = document.getElementById("registry-data");
  if (raw) {
    try { DATA = JSON.parse(raw.textContent) || DATA; } catch (e) { /* keep empty */ }
  }

  // Every editable field, split by device kind.  One modal holds both editors,
  // so collect() must only send the fields that belong to the selected kind:
  // the registry rejects an OLT-only key sent for a router ("'snmp_version' is
  // not a known router setting") and vice versa.
  var FIELDS = {
    olt: {
      text: [
        "name", "host", "branch", "site", "rack", "firmware", "model",
        "snmp_version", "community", "brand", "pon_type", "telnet_profile",
        "telnet_username", "telnet_password", "telnet_enable_password",
        "cdata_api_username", "cdata_api_password"
      ],
      num: [
        "snmp_port", "timeout_seconds", "retries", "interval_seconds",
        "display_order", "max_onus_per_pon", "telnet_port",
        "telnet_interval_seconds", "telnet_timeout_seconds", "telnet_detail_max",
        "cdata_api_port", "cdata_api_interval_seconds", "cdata_api_timeout_seconds"
      ],
      bool: [
        "enabled", "telnet_enabled", "cdata_api_enabled", "cdata_api_tls",
        "cdata_api_optical", "cdata_api_rates", "cdata_api_mac"
      ],
      secret: [
        "community", "telnet_password", "telnet_enable_password", "cdata_api_password"
      ]
    },
    router: {
      text: ["name", "host", "username", "uplink_interfaces"],
      num: ["api_port", "timeout_seconds", "interval_seconds"],
      bool: [
        "enabled", "use_tls", "verify_tls", "sfp_monitor", "monitor_traffic",
        "pppoe", "dhcp_leases"
      ],
      secret: ["password"]
    }
  };

  // the union, used by clearForm()/fill() which only touch the DOM
  function anyKind(key) {
    var out = [];
    ["olt", "router"].forEach(function (kind) {
      (FIELDS[kind][key] || []).forEach(function (f) {
        if (out.indexOf(f) < 0) { out.push(f); }
      });
    });
    return out;
  }
  var TEXT = anyKind("text");
  var NUMS = anyKind("num");
  var BOOLS = anyKind("bool");
  var SECRETS = anyKind("secret");

  var modal = null;

  function $(id) { return document.getElementById(id); }

  function showAlert(kind, html) {
    var box = $("device-alert");
    if (!box) { return; }
    box.className = "alert alert-" + kind;
    box.innerHTML = html;
    box.classList.remove("d-none");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function setKind(kind) {
    document.querySelectorAll(".olt-only").forEach(function (node) {
      node.classList.toggle("d-none", kind !== "olt");
    });
    document.querySelectorAll(".router-only").forEach(function (node) {
      node.classList.toggle("d-none", kind !== "router");
    });
  }

  function fillSelect(id, items, includeAuto) {
    var select = $(id);
    if (!select) { return; }
    select.innerHTML = "";
    if (includeAuto) {
      var auto = document.createElement("option");
      auto.value = "auto";
      auto.textContent = "auto-detect";
      select.appendChild(auto);
    }
    items.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.key;
      option.textContent = item.label + (item.verified ? "" : " (best effort)");
      if (item.pon_type) { option.dataset.pon = item.pon_type; }
      if (item.pon_type && !item.verified) { option.dataset.pon = item.pon_type; }
      select.appendChild(option);
    });
  }

  function ensureOptions() {
    if (!$("f-brand").options.length) {
      fillSelect("f-brand", DATA.brands || [], true);
    }
    if (!$("f-telnet_profile").options.length) {
      var select = $("f-telnet_profile");
      var auto = document.createElement("option");
      auto.value = "";
      auto.textContent = "auto (from brand)";
      select.appendChild(auto);
      (DATA.telnet_profiles || []).forEach(function (item) {
        var option = document.createElement("option");
        option.value = item.key;
        option.textContent = item.label;
        select.appendChild(option);
      });
    }
  }

  function brandHint() {
    var box = $("brand-hint");
    if (!box) { return; }
    var key = $("f-brand").value;
    var found = (DATA.brands || []).filter(function (b) { return b.key === key; })[0];
    if (!found) {
      box.textContent = "The brand is detected from sysDescr on every poll; auto is safe.";
      return;
    }
    var text = found.label + " - default PON: " + String(found.pon_type || "").toUpperCase();
    if (!found.verified) {
      text += ". Best-effort profile: set the Telnet command profile and check the first poll.";
    }
    box.textContent = text;
  }

  function clearForm() {
    TEXT.concat(NUMS).forEach(function (f) { var n = $("f-" + f); if (n) { n.value = ""; } });
    BOOLS.forEach(function (f) { var n = $("f-" + f); if (n) { n.checked = false; } });
    ["sfp_monitor", "monitor_traffic", "pppoe", "dhcp_leases", "enabled"].forEach(function (f) {
      var n = $("f-" + f); if (n) { n.checked = true; }
    });
    if ($("f-snmp_version")) { $("f-snmp_version").value = "2c"; }
    if ($("f-brand")) { $("f-brand").value = "auto"; }
    if ($("f-pon_type")) { $("f-pon_type").value = ""; }
    if ($("f-telnet_profile")) { $("f-telnet_profile").value = ""; }
    if ($("f-telnet_interval_seconds")) { $("f-telnet_interval_seconds").value = 300; }
    if ($("f-telnet_timeout_seconds")) { $("f-telnet_timeout_seconds").value = 8; }
    if ($("f-telnet_port")) { $("f-telnet_port").value = 23; }
    if ($("f-telnet_detail_max")) { $("f-telnet_detail_max").value = 8; }
    if ($("f-cdata_api_port")) { $("f-cdata_api_port").value = 80; }
    if ($("f-cdata_api_interval_seconds")) { $("f-cdata_api_interval_seconds").value = 300; }
    if ($("f-cdata_api_timeout_seconds")) { $("f-cdata_api_timeout_seconds").value = 12; }
    ["cdata_api_optical", "cdata_api_rates", "cdata_api_mac"].forEach(function (f) {
      var n = $("f-" + f); if (n) { n.checked = true; }
    });
    if ($("community-hint")) { $("community-hint").textContent = ""; }
    if ($("brand-hint")) { $("brand-hint").textContent = ""; }
    brandHint();
    $("device-form-error").classList.add("d-none");
  }

  function fill(device) {
    TEXT.forEach(function (f) {
      var n = $("f-" + f);
      if (!n) { return; }
      if (SECRETS.indexOf(f) >= 0) { n.value = ""; return; }
      if (device[f] !== undefined && device[f] !== null) { n.value = device[f]; }
    });
    NUMS.forEach(function (f) {
      var n = $("f-" + f);
      if (n && device[f] !== undefined && device[f] !== null) { n.value = device[f]; }
    });
    BOOLS.forEach(function (f) {
      var n = $("f-" + f);
      if (n && device[f] !== undefined) { n.checked = !!device[f]; }
    });
    if (device.kind === "olt" && $("community-hint")) {
      $("community-hint").textContent = device.community_set
        ? "a community is stored (encrypted) - leave blank to keep it"
        : "no community stored yet - the default 'public' is used";
    }
    brandHint();
  }

  function findDevice(kind, id) {
    var list = kind === "olt" ? DATA.olts : DATA.routers;
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].id) === String(id)) { return list[i]; }
    }
    return null;
  }

  function open(kind, id) {
    ensureOptions();
    clearForm();
    setKind(kind);
    $("f-kind").value = kind;
    $("f-id").value = id || "";
    $("device-modal-title").textContent = (id ? "Edit " : "Add ") +
      (kind === "olt" ? "OLT" : "router");

    if (id) {
      var device = findDevice(kind, id);
      if (device) { fill(device); }
    }
    if (modal === null) { modal = new bootstrap.Modal($("device-modal")); }
    modal.show();
  }

  function collect() {
    var kind = $("f-kind").value === "router" ? "router" : "olt";
    var spec = FIELDS[kind];
    var payload = { kind: kind };

    // texts AND secrets: a secret that is not also listed as text (the router
    // API password) would otherwise never be sent, and the device would be
    // saved with an empty password and fail every poll.
    spec.text.concat(spec.secret).forEach(function (f) {
      var n = $("f-" + f);
      if (!n) { return; }
      var value = n.value.trim();
      if (spec.secret.indexOf(f) >= 0) {
        if (value !== "") { payload[f] = value; }  // blank = keep what is stored
        return;
      }
      if (value !== "") { payload[f] = value; }
    });

    spec.num.forEach(function (f) {
      var n = $("f-" + f);
      if (n && n.value.trim() !== "") {
        var v = parseFloat(n.value);
        if (!isNaN(v)) { payload[f] = v; }
      }
    });

    spec.bool.forEach(function (f) {
      var n = $("f-" + f);
      if (!n) { return; }
      payload[f] = !!n.checked;
    });

    return payload;
  }

  function submit(event) {
    event.preventDefault();
    var id = $("f-id").value;
    var payload = collect();
    var errBox = $("device-form-error");
    errBox.classList.add("d-none");

    // A brand-new router without its API password can never connect, and a
    // blank field means "keep the stored one" only when editing.
    if (!id && payload.kind === "router" && !payload.password) {
      errBox.textContent =
        "The RouterOS API password is required for a new router " +
        "(the same one you use in WinBox / for the NOC user).";
      errBox.classList.remove("d-none");
      if (errBox.scrollIntoView) { errBox.scrollIntoView({ block: "nearest" }); }
      return false;
    }

    var url = id ? "/api/registry/" + id : "/api/registry/" + payload.kind;
    var method = id ? "PUT" : "POST";

    $("device-save").disabled = true;
    fetch(url, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          errBox.textContent = res.body.detail || "save failed";
          errBox.classList.remove("d-none");
          // the editor scrolls: make sure the message is actually on screen
          if (errBox.scrollIntoView) { errBox.scrollIntoView({ block: "nearest" }); }
          return;
        }
        window.location.href = "/devices";
      })
      .catch(function (err) {
        errBox.textContent = String(err);
        errBox.classList.remove("d-none");
        if (errBox.scrollIntoView) { errBox.scrollIntoView({ block: "nearest" }); }
      })
      .finally(function () { $("device-save").disabled = false; });
    return false;
  }

  function describeTest(res) {
    var extra = res.details || {};
    var bits = [];
    if (extra.detected) {
      bits.push("detected " + extra.detected.label + " / " + String(extra.detected.pon_type || "").toUpperCase());
    }
    if (extra.identity) { bits.push("identity: " + extra.identity); }
    if (extra.version) { bits.push("RouterOS " + extra.version); }
    if (extra.board_name) { bits.push(extra.board_name); }
    if (extra.if_number) { bits.push(extra.if_number + " interfaces"); }
    if (extra.sys_descr) { bits.push(extra.sys_descr); }
    if (extra.model) { bits.push(extra.model + " fw " + (extra.firmware || "?")); }
    if (extra.onus) { bits.push(extra.onus + " ONUs on " + (extra.pons || []).length + " PONs"); }
    return bits.length ? " <span class='text-secondary'>(" + bits.join(" &middot; ") + ")</span>" : "";
  }

  function test(id, button) {
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    fetch("/api/registry/" + id + "/test", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) {
          showAlert("success", "<strong>SNMP OK</strong> &mdash; " + res.message + describeTest(res));
        } else {
          showAlert("danger", "<strong>SNMP failed</strong> &mdash; " + (res.message || "no reply"));
        }
      })
      .catch(function (err) { showAlert("danger", String(err)); })
      .finally(function () { button.disabled = false; button.innerHTML = original; });
  }

  function testTelnet(id, button) {
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    fetch("/api/registry/" + id + "/test-telnet", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) {
          var bits = describeTest(res);
          showAlert("success", "<strong>Telnet OK</strong> &mdash; " + res.message + bits);
        } else {
          showAlert("danger", "<strong>Telnet failed</strong> &mdash; " + (res.message || "no reply"));
        }
      })
      .catch(function (err) { showAlert("danger", String(err)); })
      .finally(function () { button.disabled = false; button.innerHTML = original; });
  }

  function testApi(id, button) {
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    fetch("/api/registry/" + id + "/test-api", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) {
          showAlert("success", "<strong>OLT API OK</strong> &mdash; " + res.message + describeTest(res));
        } else {
          showAlert("danger", "<strong>OLT API failed</strong> &mdash; " + (res.message || "no reply"));
        }
      })
      .catch(function (err) { showAlert("danger", String(err)); })
      .finally(function () { button.disabled = false; button.innerHTML = original; });
  }

  function toggle(id, enabled) {
    fetch("/api/registry/" + id + "/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enabled })
    })
      .then(function (r) { return r.json(); })
      .then(function () { window.location.reload(); })
      .catch(function (err) { showAlert("danger", String(err)); });
  }

  function remove(id, name) {
    if (!window.confirm("Delete device \"" + name + "\" and all data collected for it?")) { return; }
    fetch("/api/registry/" + id, { method: "DELETE" })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) { window.location.reload(); }
        else { showAlert("danger", res.detail || "delete failed"); }
      })
      .catch(function (err) { showAlert("danger", String(err)); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    ensureOptions();
    if ($("f-brand")) { $("f-brand").addEventListener("change", brandHint); }
    var params = new URLSearchParams(window.location.search);
    var edit = params.get("edit");
    if (edit) {
      var device = null;
      (DATA.olts || []).concat(DATA.routers || []).forEach(function (d) {
        if (String(d.id) === String(edit)) { device = d; }
      });
      if (device) {
        $("#device-modal").addEventListener("shown.bs.modal", function () { window.history.replaceState({}, "", "/devices"); });
        open(device.kind, device.id);
      }
    }
  });

  function pollAll(button) {
    var original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span> starting';
    var statusEl = $("poll-all-status");
    fetch("/api/poll-all", { method: "POST" })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          showAlert("danger", res.body.detail || "could not start the poll");
          return;
        }
        var result = res.body.result || {};
        var started = (result.started || []).length;
        var running = (result.running || []).length;
        showAlert("success", res.body.message || "poll requested");
        if (started || running) { watchPollStatus(); }
      })
      .catch(function (err) { showAlert("danger", String(err)); })
      .finally(function () {
        button.disabled = false;
        button.innerHTML = original;
      });
  }

  function watchPollStatus() {
    var statusEl = $("poll-all-status");
    var sawActive = false;
    var timer = window.setInterval(function () {
      fetch("/api/poll-status")
        .then(function (r) { return r.json(); })
        .then(function (state) {
          var count = state.count || 0;
          if (count > 0) {
            sawActive = true;
            if (statusEl) {
              statusEl.innerHTML = '<span class="spinner-border spinner-border-sm"></span> polling ' +
                count + " device(s)";
            }
            return;
          }
          window.clearInterval(timer);
          if (statusEl) { statusEl.textContent = ""; }
          // Do not throw away an open editor: the user can refresh when done.
          if (sawActive && !document.querySelector(".modal.show")) {
            window.location.reload();
          }
        })
        .catch(function () {
          window.clearInterval(timer);
          if (statusEl) { statusEl.textContent = ""; }
        });
    }, 5000);
  }

  window.DeviceUI = {
    open: open, submit: submit, test: test, testTelnet: testTelnet,
    testApi: testApi, toggle: toggle, remove: remove, pollAll: pollAll
  };
})();
