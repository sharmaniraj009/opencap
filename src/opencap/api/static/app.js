// opencap UI helpers. No inline script anywhere (Content-Security-Policy: script-src 'self').
(function () {
  "use strict";
  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : "";
  }

  // Do not poll while the tab is hidden (dashboard and live values poll every few seconds).
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (document.hidden && elt && elt.classList && elt.classList.contains("poll")) {
      evt.preventDefault();
    }
  });

  // Client-side filter of dashboard rows; re-applied after every refresh.
  function applyFilter(input) {
    var root = document.querySelector(input.getAttribute("data-filter-input"));
    if (!root) return;
    var q = input.value.trim().toLowerCase();
    root.querySelectorAll("[data-filter-group]").forEach(function (group) {
      var shown = 0;
      group.querySelectorAll("[data-filter-text]").forEach(function (row) {
        var hit = !q || row.getAttribute("data-filter-text").indexOf(q) !== -1;
        row.hidden = !hit;
        if (hit) shown++;
      });
      group.hidden = shown === 0;
    });
  }
  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches("[data-filter-input]")) applyFilter(evt.target);
  });
  document.addEventListener("htmx:afterSwap", function () {
    document.querySelectorAll("[data-filter-input]").forEach(applyFilter);
  });

  // Flash messages: close button, success messages fade after a while.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-close-flash]");
    if (btn) btn.closest(".flash").remove();
  });
  document.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.detail.target && evt.detail.target.id === "flash") {
      var f = evt.detail.target.querySelector('[data-autohide="yes"]');
      if (f) setTimeout(function () { if (f.isConnected) f.remove(); }, 8000);
    }
  });

  // Import: the file is posted as the raw JSON body (no multipart parser on the server).
  var lastFile = null;
  function upload(mode, revision) {
    var out = document.getElementById("import-result");
    if (!lastFile || !out) return;
    var headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf() };
    if (revision) headers["X-Config-Revision"] = revision;
    out.innerHTML = '<p class="muted">' + (mode === "apply" ? "importing" : "comparing") + "…</p>";
    fetch("/ui/import/" + mode + "?filename=" + encodeURIComponent(lastFile.name), {
      method: "POST", headers: headers, body: lastFile, credentials: "same-origin"
    }).then(function (resp) {
      var redirect = resp.headers.get("HX-Redirect");
      if (redirect) { window.location.href = redirect; return ""; }
      return resp.text();
    }).then(function (html) {
      if (!html) return;
      out.innerHTML = html;               // server-rendered and escaped by Jinja2
      if (window.htmx) window.htmx.process(out);
      if (mode === "apply") document.body.dispatchEvent(new Event("config-changed"));
    }).catch(function (err) {
      out.textContent = "upload failed: " + err;
    });
  }
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form.matches("[data-import-form]")) return;
    evt.preventDefault();
    var input = form.querySelector('input[type="file"]');
    lastFile = input && input.files && input.files[0];
    upload("preview");
  });
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-import-apply]");
    if (!btn) return;
    if (!window.confirm("Write the imported config? The current files are kept as .bak.")) return;
    btn.disabled = true;
    upload("apply", btn.getAttribute("data-revision"));
  });
})();
