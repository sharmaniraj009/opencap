// opencap desktop launcher: connection profiles and engine health. Talks only to the app's
// own Rust commands (window.__TAURI__.core.invoke); nothing here reaches the network itself.
(function () {
  "use strict";
  var invoke = window.__TAURI__ && window.__TAURI__.core ? window.__TAURI__.core.invoke : null;
  var list = document.getElementById("profiles");
  var tpl = document.getElementById("profile-tpl");
  var dialog = document.getElementById("editor");
  var form = document.getElementById("profile-form");
  var editing = null;

  var BUNDLED = "bundled";                       // sidecar::PROFILE_ID

  function showError(el, text) { el.textContent = text || ""; el.hidden = !text; }

  // --- the engine bundled with the installers (#58) ---------------------------------------------
  function showLocal(st) {
    var box = document.getElementById("local");
    box.hidden = !st.available;
    if (!st.available) return;
    var state = document.getElementById("local-state");
    if (st.running) {
      state.className = "small status ok";
      state.textContent = "running on " + st.base_url + " (pid " + st.pid + ") · open it from \"Engine on this computer\" below";
    } else {
      state.className = "small " + (st.last_exit ? "status bad" : "muted");
      state.textContent = st.last_exit || "stopped";
    }
    document.getElementById("local-paths").textContent = "config: " + st.config_dir + " · log: " + st.log_file;
    document.getElementById("local-start").disabled = st.running;
    document.getElementById("local-stop").disabled = !st.running;
  }

  function localAction(cmd, busyText) {
    var state = document.getElementById("local-state");
    state.className = "small muted";
    state.textContent = busyText;
    document.getElementById("local-start").disabled = true;
    document.getElementById("local-stop").disabled = true;
    return invoke(cmd).then(function (st) {
      showLocal(st);
      return load().then(function () { showError(document.getElementById("notice"), st.notice); });
    }).catch(function (e) {
      showError(document.getElementById("error"), String(e));
      return invoke("local_engine_status").then(showLocal);
    });
  }

  function statusText(st) {
    var parts = [];
    parts.push((st.version || "engine") + (st.instance ? " · " + st.instance : ""));
    parts.push("devices " + st.devices.ok + " ok / " + st.devices.offline + " offline / " + st.devices.demoted + " demoted");
    if (st.mqtt_connected !== null && st.mqtt_connected !== undefined) parts.push("MQTT " + (st.mqtt_connected ? "connected" : "down"));
    var a = st.auth;
    if (a.state === "ok") parts.push("token ok (" + a.name + ", " + a.role + ")");
    else if (a.state === "open") parts.push("no token configured on the engine: read-only");
    else if (a.state === "token_required") parts.push("needs a token: edit this engine to add one");
    else if (a.state === "bad_token") parts.push("the stored token was refused");
    else parts.push("token: " + (a.detail || "unknown"));
    if (!st.has_ui) parts.push("this engine does not serve the web UI");
    return parts.join(" · ");
  }

  function statusClass(st) {
    if (st.auth.state === "bad_token" || st.auth.state === "token_required") return "status warn";
    return "status ok";
  }

  function check(p, li) {
    var el = li.querySelector(".status");
    el.className = "status muted small";
    el.textContent = "checking…";
    invoke("check_engine", { id: p.id }).then(function (st) {
      el.className = statusClass(st) + " small";
      el.textContent = statusText(st);
      li.querySelector(".open").disabled = !st.has_ui;
    }).catch(function (e) {
      el.className = "status bad small";
      el.textContent = String(e);
    });
  }

  function render(profiles) {
    list.textContent = "";
    profiles.forEach(function (p) {
      var li = tpl.content.firstElementChild.cloneNode(true);
      li.querySelector(".name").textContent = p.name;
      li.querySelector(".url").textContent = p.base_url + (p.keychain_error ? " · keychain unavailable: " + p.keychain_error
        : (p.has_token ? " · token in keychain" : " · no token stored"));
      if (p.cleartext && p.has_token) {
        var warn = li.querySelector(".warn");
        warn.textContent = "The token is sent unencrypted (http:// to another computer). Put TLS (https) in front of the engine.";
        warn.hidden = false;
      }
      li.querySelector(".open").addEventListener("click", function () {
        invoke("open_engine", { id: p.id }).then(function () { return invoke("get_settings"); }).then(showSettings)
          .catch(function (e) { showError(document.getElementById("error"), String(e)); });
      });
      li.querySelector(".check").addEventListener("click", function () { check(p, li); });
      if (p.id === BUNDLED) {                    // follows the bundled engine by itself
        li.querySelector(".edit").hidden = true;
        li.querySelector(".delete").hidden = true;
      }
      li.querySelector(".edit").addEventListener("click", function () { openEditor(p); });
      li.querySelector(".delete").addEventListener("click", function () {
        if (!window.confirm("Delete " + p.name + " and its stored token?")) return;
        invoke("delete_profile", { id: p.id }).then(load).catch(function (e) { showError(document.getElementById("error"), String(e)); });
      });
      list.appendChild(li);
      check(p, li);
    });
  }

  // --- preferences: tray, start at login, what happens when the app starts ------------------------
  function showSettings(s) {
    var last = document.getElementById("open-last");
    last.hidden = !s.last_profile_name;
    last.textContent = s.last_profile_name ? "Open " + s.last_profile_name : "";
    last.onclick = function () {
      invoke("open_engine", { id: s.last_profile }).catch(function (e) { showError(document.getElementById("error"), String(e)); });
    };
    var auto = document.getElementById("pref-autostart");
    auto.checked = !!s.autostart;
    auto.disabled = s.autostart === null || s.autostart === undefined;
    document.getElementById("pref-autostart-note").textContent = auto.disabled ? "(not available here" + (s.autostart_error ? ": " + s.autostart_error : "") + ")" : "";
    var tray = document.getElementById("pref-tray");
    tray.checked = !!s.keep_in_tray;
    tray.disabled = !s.tray;
    document.getElementById("pref-tray-note").textContent = s.tray ? "" : "(this desktop has no status area for a tray icon)";
    document.getElementById("pref-engine").checked = !!s.start_engine_on_launch;
    document.getElementById("pref-last").checked = !!s.open_last_on_launch;
  }

  function prefsFailed(e) {
    showError(document.getElementById("prefs-error"), String(e));
    return invoke("get_settings").then(showSettings);
  }

  function bindPref(id, run) {
    document.getElementById(id).addEventListener("change", function (ev) {
      showError(document.getElementById("prefs-error"), "");
      run(ev.target.checked).then(showSettings).catch(prefsFailed);
    });
  }

  function load() {
    showError(document.getElementById("error"), "");
    showError(document.getElementById("notice"), "");
    return invoke("list_profiles").then(render).catch(function (e) { showError(document.getElementById("error"), String(e)); });
  }

  function openEditor(p) {
    editing = p || null;
    document.getElementById("editor-title").textContent = p ? "Edit " + p.name : "Add engine";
    form.elements.name.value = p ? p.name : "";
    form.elements.base_url.value = p ? p.base_url : "http://127.0.0.1:8080";
    form.elements.token.value = "";
    form.elements.clear_token.checked = false;
    form.elements.clear_token.parentElement.hidden = !(p && p.has_token);
    showError(document.getElementById("form-error"), "");
    dialog.showModal();
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var input = {
      id: editing ? editing.id : null,
      name: form.elements.name.value,
      base_url: form.elements.base_url.value,
      token: form.elements.token.value || null,
      clear_token: form.elements.clear_token.checked,
    };
    invoke("save_profile", { input: input }).then(function (saved) {
      form.elements.token.value = "";
      dialog.close();
      load().then(function () { showError(document.getElementById("notice"), saved && saved.notice); });
    }).catch(function (e) { showError(document.getElementById("form-error"), String(e)); });
  });
  document.getElementById("cancel").addEventListener("click", function () { dialog.close(); });
  document.getElementById("add").addEventListener("click", function () { openEditor(null); });

  if (!invoke) {
    showError(document.getElementById("error"), "This page runs inside the opencap desktop app.");
    return;
  }
  document.getElementById("local-start").addEventListener("click", function () { localAction("start_local_engine", "starting…"); });
  document.getElementById("local-stop").addEventListener("click", function () { localAction("stop_local_engine", "stopping…"); });
  bindPref("pref-autostart", function (on) { return invoke("set_autostart", { enabled: on }); });
  bindPref("pref-tray", function (on) { return invoke("set_settings", { patch: { keep_in_tray: on } }); });
  bindPref("pref-engine", function (on) { return invoke("set_settings", { patch: { start_engine_on_launch: on } }); });
  bindPref("pref-last", function (on) { return invoke("set_settings", { patch: { open_last_on_launch: on } }); });
  invoke("get_settings").then(showSettings);
  invoke("local_engine_status").then(function (st) {
    showLocal(st);
    document.getElementById("pref-engine-row").hidden = !st.available;
  });
  invoke("app_info").then(function (info) {
    document.getElementById("foot").textContent = "opencap desktop " + info.version + " · profiles: " + info.profiles_path +
      " (tokens: system keychain) · read-only towards PLCs";
  });
  load();
})();
