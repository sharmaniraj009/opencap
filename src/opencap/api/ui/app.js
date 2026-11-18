/* opencap web UI: a static page that talks only to the public API under /api/v1 (ADR-0010).
 *
 * No build step: plain JS plus the vendored Alpine.js CSP build, so the page runs under a strict
 * Content-Security-Policy (no inline script, no eval). Expressions in index.html therefore stay
 * simple: property access, comparisons and calls of the component methods defined here.
 * The API token is kept in this tab's sessionStorage and sent as `Authorization: Bearer`.
 * opencap is read-only towards PLCs: nothing here can write a PLC value.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "opencap.token";
  var BASE = (window.OPENCAP_API_BASE || "").replace(/\/$/, "");   // set by the desktop shell for remote nodes
  var DTYPES = ["bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"];
  var DEVICE_INTS = ["port", "scan_ms", "min_scan_ms", "timeout_ms", "retries", "demote_after", "demote_for_ms"];

  // ---------------------------------------------------------------- API client
  function ApiError(status, data) {
    this.status = status;
    this.data = data;
    var d = data && data.detail;
    this.message = typeof d === "string" ? d : (data && data.message) || ("HTTP " + status);
  }

  function readToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function writeToken(t) {
    try { if (t) { sessionStorage.setItem(TOKEN_KEY, t); } else { sessionStorage.removeItem(TOKEN_KEY); } } catch (e) { /* private mode */ }
  }

  function call(method, path, opts) {
    opts = opts || {};
    var headers = { "X-Opencap-Client": "ui" };
    var token = opts.token !== undefined ? opts.token : readToken();
    if (token) headers.Authorization = "Bearer " + token;
    var body;
    if (opts.raw !== undefined) { headers["Content-Type"] = "application/json"; body = opts.raw; }
    else if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.body); }
    if (opts.rev) headers["If-Match"] = '"' + opts.rev + '"';
    return fetch(BASE + path, { method: method, headers: headers, body: body, cache: "no-store" }).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = { detail: text }; } }
        if (resp.status === 401 && opts.token === undefined) {
          writeToken("");
          go("login");
        }
        if (!resp.ok) throw new ApiError(resp.status, data);
        return data;
      });
    });
  }

  function enc(s) { return encodeURIComponent(String(s)); }
  function encId(id) { return String(id).split("/").map(encodeURIComponent).join("/"); }   // "<channel>/<device>"

  // Errors of a failed write: {field: message} next to the inputs, the rest above the form.
  function splitErrors(err, known) {
    var fields = {}, general = [];
    if (!(err instanceof ApiError)) { general.push(String(err && err.message || err)); return { fields: fields, general: general }; }
    if (err.status === 412) {
      general.push("Someone changed the configuration since you loaded it. Load the page again, then redo your change.");
    } else if (err.data && Array.isArray(err.data.detail)) {
      err.data.detail.forEach(function (e) {
        var loc = (e.loc || []).slice();
        var msg = e.msg || "";
        if (loc.length && known.indexOf(String(loc[0])) !== -1) {
          var f = String(loc.shift());
          if (!fields[f]) fields[f] = loc.length ? loc.join(".") + ": " + msg : msg;
        } else {
          general.push(loc.length ? loc.join(".") + ": " + msg : msg);
        }
      });
    } else {
      general.push(err.message);
    }
    return { fields: fields, general: general };
  }

  function parseJsonField(text, field, errors) {
    if (!text || !String(text).trim()) return null;
    try {
      var v = JSON.parse(text);
      if (v === null || typeof v !== "object" || Array.isArray(v)) { errors[field] = "expected a JSON object like {\"key\": 1}"; return undefined; }
      return v;
    } catch (e) {
      errors[field] = "not valid JSON: " + e.message;
      return undefined;
    }
  }

  function pretty(v) { return v && Object.keys(v).length ? JSON.stringify(v, null, 2) : ""; }
  function compact(v) { return v && Object.keys(v).length ? JSON.stringify(v) : ""; }

  // ---------------------------------------------------------------- routing
  function parseHash() {
    var h = (location.hash || "").replace(/^#\/?/, "");
    if (!h) return { name: "dashboard", arg: "" };
    var i = h.indexOf("/");
    var name = i === -1 ? h : h.slice(0, i);
    var arg = i === -1 ? "" : h.slice(i + 1).split("/").map(decodeURIComponent).join("/");
    return { name: name, arg: arg };
  }
  function href(name, arg) { return "#/" + name + (arg ? "/" + encId(arg) : ""); }
  function go(name, arg) { location.hash = href(name, arg); }
  function changed() { window.dispatchEvent(new Event("opencap:changed")); }

  // ---------------------------------------------------------------- shared helpers (mixed into views)
  var helpers = {
    href: href,
    deviceHref: function (id) { return href("device", id); },
    editHref: function (id) { return href("edit", id); },
    channelHref: function (name) { return "#/channel/" + enc(name); },
    isAdmin: function () { return Alpine.store("app").role === "admin"; },
    canEdit: function () { var s = Alpine.store("app"); return s.role === "admin" && s.writable; },
    editBlockedText: function () {
      var s = Alpine.store("app");
      if (s.role !== "admin") return s.open ? "No API token is configured: the configuration is read-only." : "Your role is viewer: changes need an admin token.";
      return s.writable ? "" : "The config directory is read-only: " + s.writableReason;
    },
    fmt: function (v) {
      if (v === null || v === undefined) return "—";
      if (typeof v === "boolean") return v ? "true" : "false";
      if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toPrecision(6)));
      if (typeof v === "object") { var s = JSON.stringify(v); return s.length > 200 ? s.slice(0, 197) + "…" : s; }
      return String(v);
    },
    short: function (v, n) {
      var s = typeof v === "string" ? v : JSON.stringify(v);
      if (s === undefined) s = "";
      n = n || 120;
      return s.length > n ? s.slice(0, n - 1) + "…" : s;
    },
    ago: function (iso) {
      if (!iso) return "never";
      var s = (Date.now() - Date.parse(iso)) / 1000;
      if (isNaN(s)) return iso;
      if (s < 0) s = 0;
      if (s < 90) return Math.round(s) + " s ago";
      if (s < 5400) return Math.round(s / 60) + " min ago";
      if (s < 172800) return Math.round(s / 3600) + " h ago";
      return Math.round(s / 86400) + " d ago";
    },
    ms: function (v) { return v === null || v === undefined ? "—" : Math.round(v) + " ms"; },
    pct: function (v) { return v === null || v === undefined ? "—" : Math.round(v * 100) + "%"; },
    stateClass: function (s) { return "badge state-" + (s || "offline"); },
    qualityClass: function (q) { return "badge q-" + q; },
    rateText: function (r) { return r % 1000 === 0 ? (r / 1000) + "s" : r + "ms"; },
    join: function (list, sep) { return (list || []).join(sep || ", "); },
    count: function (list) { return (list || []).length; },
    has: function (list) { return !!(list && list.length); },
    err: function (name) { return (this.errors && this.errors[name]) || ""; },
  };

  // poll fn() every `ms` while the tab is visible; returns a stop function
  function poll(fn, ms) {
    var id = setInterval(function () { if (!document.hidden) fn(); }, ms);
    return function () { clearInterval(id); };
  }

  document.addEventListener("alpine:init", function () {
    Alpine.store("app", { name: "", role: "", open: false, writable: false, writableReason: "", ready: false, route: parseHash(), pending: null, flash: null });

    // ------------------------------------------------------------ shell
    Alpine.data("shell", function () {
      return Object.assign({}, helpers, {
        init: function () {
          var self = this;
          window.addEventListener("hashchange", function () { self.route(); });
          window.addEventListener("opencap:changed", function () { self.loadPending(); });
          this.whoami().then(function () { self.route(); });
          this.stopPending = poll(function () { self.loadPending(); }, 30000);
        },
        app: function () { return Alpine.store("app"); },
        view: function (name) { return Alpine.store("app").ready && Alpine.store("app").route.name === name; },
        active: function (names) { return names.split(" ").indexOf(Alpine.store("app").route.name) !== -1; },
        whoami: function () {
          var s = Alpine.store("app");
          return call("GET", "/api/v1/auth-info", { token: "" }).then(function (info) {
            if (!info.open && !readToken()) throw new Error("login needed");
            return call("GET", "/api/v1/whoami", { token: readToken() });
          }).then(function (me) {
            s.name = me.name; s.role = me.role; s.open = me.open; s.ready = true;
            s.writable = !!me.config_writable; s.writableReason = me.config_writable_reason || "";
          }).catch(function () {
            s.name = ""; s.role = ""; s.ready = true;
          });
        },
        route: function () {
          var s = Alpine.store("app");
          var r = parseHash();
          if (!s.name && r.name !== "login") { go("login"); return; }
          s.route = r;
          window.dispatchEvent(new Event("opencap:route"));
          if (s.name) this.loadPending();
        },
        loadPending: function () {
          var s = Alpine.store("app");
          if (!s.name) return;
          call("GET", "/api/v1/project/pending").then(function (p) { s.pending = p; }).catch(function () { /* keep */ });
        },
        pendingShown: function () { var p = Alpine.store("app").pending; return !!(p && p.available && p.pending_reload); },
        pendingText: function () {
          var p = Alpine.store("app").pending;
          if (!p) return "";
          if (!p.disk_valid) return "The config on disk does not load: " + p.error + ". A reload would fail and keep the running config.";
          var parts = [];
          var c = p.channels || {};
          if (c.changed && c.changed.length) parts.push(c.changed.length + " channel(s) change (" + c.changed.slice(0, 3).join(", ") + (c.changed.length > 3 ? ", …" : "") + ")");
          if (c.added && c.added.length) parts.push(c.added.length + " added");
          if (c.removed && c.removed.length) parts.push(c.removed.length + " removed");
          if (p.poller_changed && p.poller_changed.length) parts.push("poller.yaml: " + p.poller_changed.join(", "));
          var text = "Changes not applied yet: " + parts.join("; ") + ".";
          if (p.restart_needed && p.restart_needed.length) text += " " + p.restart_needed.join(", ") + " need a restart.";
          return text;
        },
        canReload: function () { var p = Alpine.store("app").pending; return this.isAdmin() && !!(p && p.disk_valid); },
        reload: function () {
          var self = this;
          call("POST", "/api/v1/reload").then(function (r) {
            var parts = [];
            ["started", "restarted", "stopped"].forEach(function (k) { if (r[k] && r[k].length) parts.push(k + " " + r[k].join(", ")); });
            self.flash("Reloaded: " + (parts.length ? parts.join("; ") : "nothing changed"), "ok");
            changed();
          }).catch(function (e) { self.flash(e.message, "error"); });
        },
        flash: function (message, level) {
          var s = Alpine.store("app");
          s.flash = { message: message, level: level || "ok" };
          if (level === "ok") setTimeout(function () { if (s.flash && s.flash.message === message) s.flash = null; }, 8000);
        },
        flashClass: function () { var f = Alpine.store("app").flash; return "flash flash-" + (f ? f.level : "ok"); },
        flashText: function () { var f = Alpine.store("app").flash; return f ? f.message : ""; },
        closeFlash: function () { Alpine.store("app").flash = null; },
        logout: function () {
          writeToken("");
          var s = Alpine.store("app");
          s.name = ""; s.role = ""; s.pending = null;
          go("login");
        },
        userText: function () { var s = Alpine.store("app"); return s.name + " · " + s.role; },
      });
    });
    function shellFlash(message, level) {
      var s = Alpine.store("app");
      s.flash = { message: message, level: level || "ok" };
      if (level === "ok") setTimeout(function () { if (s.flash && s.flash.message === message) s.flash = null; }, 8000);
    }

    // a view reloads when the route changes while it stays on screen (e.g. device A -> device B)
    function routed(self, load) {
      self._onRoute = function () { load.call(self); };
      window.addEventListener("opencap:route", self._onRoute);
    }
    function unrouted(self) {
      if (self._onRoute) window.removeEventListener("opencap:route", self._onRoute);
      if (self._stop) self._stop();
    }

    // ------------------------------------------------------------ login
    Alpine.data("loginView", function () {
      return Object.assign({}, helpers, {
        token: "", error: "", busy: false,
        submit: function () {
          var self = this;
          this.busy = true; this.error = "";
          call("GET", "/api/v1/whoami", { token: this.token.trim() }).then(function (me) {
            writeToken(self.token.trim());
            var s = Alpine.store("app");
            s.name = me.name; s.role = me.role; s.open = me.open;
            s.writable = !!me.config_writable; s.writableReason = me.config_writable_reason || "";
            self.token = "";
            go("dashboard");
          }).catch(function (e) {
            self.error = e.status === 401 ? "Unknown token." : e.message;
          }).then(function () { self.busy = false; });
        },
      });
    });

    // ------------------------------------------------------------ dashboard
    Alpine.data("dashboardView", function () {
      return Object.assign({}, helpers, {
        data: null, q: "", error: "",
        init: function () {
          var self = this;
          this.load();
          this._stop = poll(function () { self.load(); }, 5000);
        },
        destroy: function () { unrouted(this); },
        load: function () {
          var self = this;
          return call("GET", "/api/v1/status").then(function (d) {
            self.data = d; self.error = "";
            if (d.config) Alpine.store("app").pending = d.config;
          }).catch(function (e) { self.error = e.message; });
        },
        health: function () { return (this.data && this.data.health) || { devices: {} }; },
        mqttText: function () { return this.health().mqtt_connected ? "connected" : "down"; },
        mqttClass: function () { return "value " + (this.health().mqtt_connected ? "ok" : "bad"); },
        outboxClass: function () { return "value" + (this.health().saf_queue > 100 ? " warn" : ""); },
        stateCount: function (s) { var d = this.health().devices || {}; return d[s] || 0; },
        sites: function () {
          var q = this.q.trim().toLowerCase();
          var out = [];
          ((this.data && this.data.channels) || []).forEach(function (ch) {
            var devices = ch.devices.filter(function (d) {
              return !q || (d.id + " " + d.driver + " " + (d.state || "offline")).toLowerCase().indexOf(q) !== -1;
            });
            if (devices.length) out.push({ name: ch.name, devices: devices });
          });
          return out;
        },
        noChannels: function () { return !!this.data && !(this.data.channels || []).length; },
        deviceName: function (d, ch) { return d.id.indexOf(ch.name + "/") === 0 ? d.id.slice(ch.name.length + 1) : d.id; },
        errorsText: function (d) { return d.errors === null || d.errors === undefined ? "—" : String(d.errors); },
      });
    });

    // ------------------------------------------------------------ device page
    Alpine.data("deviceView", function () {
      return Object.assign({}, helpers, {
        id: "", detail: null, values: [], plan: null, ident: null, readNote: "", missing: false, error: "", busy: false,
        init: function () {
          var self = this;
          routed(this, this.load);
          this.load();
          this._stop = poll(function () { self.refresh(); }, 2000);
        },
        destroy: function () { unrouted(this); },
        load: function () {
          var r = Alpine.store("app").route;
          if (r.name !== "device") return;
          this.id = r.arg; this.detail = null; this.values = []; this.plan = null; this.ident = null;
          this.readNote = ""; this.missing = false; this.error = "";
          var self = this;
          this.refresh();
          call("GET", "/api/v1/devices/" + encId(this.id) + "/plan").then(function (p) { self.plan = p; }).catch(function () {});
          this.loadIdentity();
        },
        refresh: function () {
          var self = this, base = "/api/v1/devices/" + encId(this.id);
          if (!this.id || this.missing) return;
          call("GET", base + "/status").then(function (d) {
            self.detail = d;
            return call("GET", base + "/values");
          }).then(function (v) {
            self.values = Object.keys(v).sort().map(function (k) { return { name: k, value: v[k].value, quality: v[k].quality, ts: v[k].source_ts }; });
          }).catch(function (e) {
            if (e.status === 404) self.missing = true; else self.error = e.message;
          });
        },
        loadIdentity: function (refresh) {
          var self = this;
          this.ident = null;
          call("GET", "/api/v1/devices/" + encId(this.id) + "/identity" + (refresh ? "?refresh=true" : "")).then(function (i) {
            self.ident = { connected: i.connected, error: i.error || "", rows: Object.keys(i.identity || {}).sort().map(function (k) { return { key: k, value: i.identity[k] }; }) };
          }).catch(function (e) { self.ident = { connected: false, error: e.message, rows: [] }; });
        },
        readOnce: function () {
          var self = this;
          this.busy = true;
          call("POST", "/api/v1/devices/" + encId(this.id) + "/read-once").then(function () {
            self.readNote = "Read once at " + new Date().toLocaleTimeString();
            self.refresh();
          }).catch(function (e) { shellFlash("Read once failed: " + e.message, "error"); }).then(function () { self.busy = false; });
        },
        qualityCount: function (q) { return this.values.filter(function (v) { return v.quality === q; }).length; },
        countsText: function () {
          var self = this, parts = [this.qualityCount("good") + " good"];
          ["bad_comm", "bad_config", "bad_decode", "stale"].forEach(function (q) { var n = self.qualityCount(q); if (n) parts.push(n + " " + q); });
          return parts.join(" · ");
        },
        subtitle: function () {
          var d = this.detail;
          if (!d) return "";
          return d.driver + " · " + d.host + ":" + d.port + " · topic " + d.topic + " · SubDeviceId " + (d.sub_device_id || "none") + (d.enabled ? "" : " · disabled");
        },
        planText: function () {
          var p = this.plan;
          if (!p || p.plan_error) return "";
          return p.enabled_tags + " enabled tags in " + p.blocks + " block(s), " + p.wire_requests + " request(s) per full pass, about " +
            Math.round(p.est_cycle_ms) + " ms per pass, channel load " + Math.round(p.load * 100) + "% (estimates).";
        },
        invalidRows: function () { var inv = (this.plan && this.plan.invalid) || {}; return Object.keys(inv).sort().map(function (k) { return { name: k, reason: inv[k] }; }); },
        blockRows: function () { return (this.plan && this.plan.block_plan) || []; },
      });
    });

    // ------------------------------------------------------------ config overview
    Alpine.data("configView", function () {
      return Object.assign({}, helpers, {
        summary: null, channels: [], rev: "", defaultsText: "", defaultsErrors: [], defaultsNote: "",
        newChannel: "", errors: {}, general: [], loadError: "",
        init: function () { this.load(); },
        load: function () {
          var self = this;
          return Promise.all([call("GET", "/api/v1/project/status"), call("GET", "/api/v1/channels").catch(function () { return { channels: [] }; }),
                              call("GET", "/api/v1/map")]).then(function (r) {
            self.summary = r[0]; self.rev = r[0].revision; self.channels = r[1].channels;
            self.defaultsText = pretty(r[2].defaults);
          }).catch(function (e) { self.loadError = e.message; });
        },
        counts: function () { return (this.summary && this.summary.counts) || {}; },
        warnings: function () { return (this.summary && this.summary.warnings) || []; },
        diskErrors: function () { return ((this.summary && this.summary.errors) || []).slice(0, 20).map(function (e) { return (e.loc || []).join(".") + ": " + e.msg; }); },
        overrideText: function (c) { return c.override ? "enabled: " + c.override.enabled : "—"; },
        addChannel: function () {
          var self = this;
          this.errors = {}; this.general = [];
          call("POST", "/api/v1/channels", { body: { name: this.newChannel }, rev: this.rev }).then(function () {
            changed();
            location.hash = "#/channel/" + enc(self.newChannel);
          }).catch(function (e) { var s = splitErrors(e, ["name"]); self.errors = s.fields; self.general = s.general; });
        },
        exportConfig: function () {
          call("GET", "/api/v1/project").then(function (data) {
            var blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
            var a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "opencap-config-" + data.revision + ".json";
            document.body.appendChild(a);
            a.click();
            setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
          }).catch(function (e) { shellFlash("Export failed: " + e.message, "error"); });
        },
        saveDefaults: function () {
          var self = this, errs = {};
          this.defaultsErrors = []; this.defaultsNote = "";
          var body = parseJsonField(this.defaultsText, "defaults", errs);
          if (body === undefined) { this.defaultsErrors = [errs.defaults]; return; }
          call("PUT", "/api/v1/map/defaults", { body: body || {}, rev: this.rev }).then(function (r) {
            self.rev = r.revision; self.defaultsNote = savedNote(r); changed();
          }).catch(function (e) { var s = splitErrors(e, []); self.defaultsErrors = s.general; });
        },
      });
    });

    function savedNote(r) {
      if (!r.files || !r.files.length) return "No change.";
      var note = "Saved to " + r.files.join(", ") + ". Reload to apply.";
      var target = String(r.target || "").split("#")[0];
      var mine = (r.warnings || []).filter(function (w) { return w.indexOf(target + ":") === 0; });
      if (r.comments_lost) note += " Comments could not be kept in " + r.comments_lost.join(", ") + " (old text in .bak).";
      return mine.length ? note + " Warning: " + mine.join(" ") : note;
    }

    // ------------------------------------------------------------ channel page
    Alpine.data("channelView", function () {
      return Object.assign({}, helpers, {
        name: "", channel: null, rev: "", form: { name: "", enabled: true, override: "" }, errors: {}, general: [], note: "",
        drivers: [], dev: { name: "", driver: "modbus_tcp", host: "", port: "502", params: "" }, devErrors: {}, devGeneral: [], loadError: "",
        init: function () { routed(this, this.load); this.load(); },
        destroy: function () { unrouted(this); },
        load: function () {
          var r = Alpine.store("app").route, self = this;
          if (r.name !== "channel") return;
          this.name = r.arg; this.loadError = ""; this.note = ""; this.errors = {}; this.general = [];
          return Promise.all([call("GET", "/api/v1/channels/" + enc(this.name)), call("GET", "/api/v1/project/status")]).then(function (res) {
            var c = res[0];
            self.channel = c; self.rev = c.revision; self.drivers = res[1].drivers;
            var ov = c.override && typeof c.override.enabled === "boolean" ? String(c.override.enabled) : "";
            self.form = { name: c.name, enabled: c.enabled, override: ov };
          }).catch(function (e) { self.loadError = e.message; });
        },
        save: function () {
          var self = this, cur = this.channel;
          this.errors = {}; this.general = []; this.note = "";
          var newName = this.form.name;
          call("PATCH", "/api/v1/channels/" + enc(this.name), { body: { name: newName, enabled: this.form.enabled }, rev: this.rev }).then(function (r) {
            self.rev = r.revision;
            var curOv = cur.override && typeof cur.override.enabled === "boolean" ? String(cur.override.enabled) : "";
            if (self.form.override === curOv) return r;
            if (self.form.override === "") return call("DELETE", "/api/v1/overrides/channels/" + enc(newName), { rev: self.rev });
            return call("PUT", "/api/v1/overrides/channels/" + enc(newName), { body: { enabled: self.form.override === "true" }, rev: self.rev });
          }).then(function (r) {
            changed();
            if (newName !== self.name) { location.hash = "#/channel/" + enc(newName); return; }
            self.note = savedNote(r);
            self.load().then(function () { self.note = savedNote(r); });
          }).catch(function (e) { var s = splitErrors(e, ["name", "enabled"]); self.errors = s.fields; self.general = s.general; });
        },
        remove: function () {
          var self = this, n = (this.channel && this.channel.devices.length) || 0;
          if (!window.confirm("Delete channel " + this.name + " with its " + n + " device(s), their publish map entries and overrides?")) return;
          call("DELETE", "/api/v1/channels/" + enc(this.name), { rev: this.rev }).then(function () { changed(); go("config"); })
            .catch(function (e) { shellFlash(splitErrors(e, []).general.join(" "), "error"); });
        },
        addDevice: function () {
          var self = this, errs = {};
          this.devErrors = {}; this.devGeneral = [];
          var params = parseJsonField(this.dev.params, "params", errs);
          if (params === undefined) { this.devErrors = errs; return; }
          var body = { name: this.dev.name, driver: this.dev.driver, host: this.dev.host, port: this.dev.port };
          if (params) body.params = params;
          call("POST", "/api/v1/channels/" + enc(this.name) + "/devices", { body: body, rev: this.rev }).then(function (r) {
            changed(); go("edit", r.target);
          }).catch(function (e) {
            var s = splitErrors(e, ["name", "driver", "host", "port", "params"]); self.devErrors = s.fields; self.devGeneral = s.general;
          });
        },
        devices: function () { return (this.channel && this.channel.devices) || []; },
      });
    });

    // ------------------------------------------------------------ device editor
    var DEVICE_FIELDS = ["name", "channel", "driver", "host", "enabled", "params"].concat(DEVICE_INTS);
    var TAG_FIELDS = ["name", "address", "dtype", "scan_ms", "enabled", "description", "scale"];

    function tagForm(t) {
      return { name: t.name || "", address: t.address || "", dtype: t.dtype || "float32",
               scan_ms: t.scan_ms === undefined || t.scan_ms === null ? "" : String(t.scan_ms),
               enabled: t.enabled !== false, description: t.description || "", scale: compact(t.scale) };
    }
    function tagBody(f, errs) {
      var body = { name: f.name.trim(), address: f.address.trim(), dtype: f.dtype, enabled: !!f.enabled };
      if (String(f.scan_ms).trim()) body.scan_ms = String(f.scan_ms).trim();
      if (f.description.trim()) body.description = f.description;
      var scale = parseJsonField(f.scale, "scale", errs);
      if (scale === undefined) return undefined;
      if (scale) body.scale = scale;
      return body;
    }

    Alpine.data("editorView", function () {
      return Object.assign({}, helpers, {
        id: "", dev: null, rev: "", form: {}, errors: {}, general: [], note: "", loadError: "",
        drivers: [], channels: [], dtypes: DTYPES,
        tags: [], tagOverrides: {}, editing: "", tagEdit: {}, tagErrors: {}, tagGeneral: [], savedTag: "",
        newTag: tagForm({}), newTagErrors: {}, newTagGeneral: [],
        map: { sub_device_id: "", topic: "", env: "", stub: false }, hasMap: false, mapErrors: {}, mapGeneral: [], mapNote: "",
        overrideText: "", hasOverride: false, ovErrors: [], ovNote: "",
        init: function () { routed(this, this.load); this.load(); },
        destroy: function () { unrouted(this); },
        load: function () {
          var r = Alpine.store("app").route, self = this;
          if (r.name !== "edit") return Promise.resolve();
          this.id = r.arg; this.loadError = "";
          return Promise.all([call("GET", "/api/v1/devices/" + encId(this.id)), call("GET", "/api/v1/project/status"),
                              call("GET", "/api/v1/channels")]).then(function (res) {
            self.apply(res[0]);
            self.drivers = res[1].drivers;
            self.channels = res[2].channels.map(function (c) { return c.name; });
          }).catch(function (e) { self.loadError = e.message; });
        },
        apply: function (data) {
          var d = data.device;
          this.dev = d; this.rev = data.revision;
          var f = {};
          ["name", "channel", "driver", "host"].forEach(function (k) { f[k] = d[k]; });
          DEVICE_INTS.forEach(function (k) { f[k] = d[k] === undefined ? "" : String(d[k]); });
          f.enabled = d.enabled !== false;
          f.params = pretty(d.params);
          this.form = f;
          this.tags = d.tags;
          this.tagOverrides = (data.override && data.override.tags) || {};
          var m = data.map_entry;
          this.hasMap = !!m;
          this.map = { sub_device_id: (m && m.sub_device_id) || "", topic: (m && m.topic) || "", env: (m && m.env) || "", stub: !!(m && m.stub) };
          this.overrideText = pretty(data.override);
          this.hasOverride = !!data.override;
        },
        reloadDevice: function () {
          var self = this;
          return call("GET", "/api/v1/devices/" + encId(this.id)).then(function (d) { self.apply(d); });
        },
        sourceText: function () { return this.dev && this.dev.source && Object.keys(this.dev.source).length ? "Imported from KEPServerEX: " + JSON.stringify(this.dev.source) : ""; },
        saveDevice: function () {
          var self = this, errs = {};
          this.errors = {}; this.general = []; this.note = "";
          var params = parseJsonField(this.form.params, "params", errs);
          if (params === undefined) { this.errors = errs; return; }
          var body = Object.assign({}, this.dev);
          delete body.id;
          ["name", "channel", "driver", "host"].forEach(function (k) { body[k] = String(self.form[k]).trim(); });
          DEVICE_INTS.forEach(function (k) { var v = String(self.form[k]).trim(); if (v === "") delete body[k]; else body[k] = v; });
          body.enabled = !!this.form.enabled;
          body.params = params || {};
          call("PUT", "/api/v1/devices/" + encId(this.id), { body: body, rev: this.rev }).then(function (r) {
            changed();
            if (r.data.id !== self.id) { go("edit", r.data.id); return; }
            return self.reloadDevice().then(function () { self.note = savedNote(r); });
          }).catch(function (e) { var s = splitErrors(e, DEVICE_FIELDS); self.errors = s.fields; self.general = s.general; });
        },
        removeDevice: function () {
          var self = this, channel = this.dev.channel;
          if (!window.confirm("Delete device " + this.id + " with its " + this.tags.length + " tag(s), its publish map entry and its override?")) return;
          call("DELETE", "/api/v1/devices/" + encId(this.id), { rev: this.rev }).then(function () {
            changed(); location.hash = "#/channel/" + enc(channel);
          }).catch(function (e) { self.general = splitErrors(e, []).general; });
        },
        // tags
        isEditing: function (t) { return this.editing === t.name; },
        hasTagOverride: function (t) { return Object.prototype.hasOwnProperty.call(this.tagOverrides, t.name); },
        tagOverrideText: function (t) { return "override: " + JSON.stringify(this.tagOverrides[t.name]); },
        scanText: function (t) { return t.scan_ms === undefined || t.scan_ms === null ? "" : String(t.scan_ms); },
        enabledText: function (t) { return t.enabled === false ? "no" : "yes"; },
        scaleText: function (t) { return t.scale ? this.short(JSON.stringify(t.scale), 60) : ""; },
        rowClass: function (t) { return t.enabled === false ? "disabled" : ""; },
        startEdit: function (t) { this.editing = t.name; this.tagEdit = tagForm(t); this.tagErrors = {}; this.tagGeneral = []; },
        cancelEdit: function () { this.editing = ""; },
        tagErr: function (f) { return this.tagErrors[f] || ""; },
        saveTag: function () {
          var self = this, errs = {}, name = this.editing;
          this.tagErrors = {}; this.tagGeneral = [];
          var body = tagBody(this.tagEdit, errs);
          if (body === undefined) { this.tagErrors = errs; return; }
          call("PUT", "/api/v1/devices/" + encId(this.id) + "/tags/" + enc(name), { body: body, rev: this.rev }).then(function (r) {
            self.editing = ""; self.savedTag = r.data.name; changed();
            return self.reloadDevice();
          }).catch(function (e) { var s = splitErrors(e, TAG_FIELDS); self.tagErrors = s.fields; self.tagGeneral = s.general; });
        },
        deleteTag: function (t) {
          var self = this;
          if (!window.confirm("Delete tag " + t.name + "?")) return;
          call("DELETE", "/api/v1/devices/" + encId(this.id) + "/tags/" + enc(t.name), { rev: this.rev }).then(function () {
            changed(); return self.reloadDevice();
          }).catch(function (e) { self.tagGeneral = splitErrors(e, []).general; });
        },
        newTagErr: function (f) { return this.newTagErrors[f] || ""; },
        addTag: function () {
          var self = this, errs = {};
          this.newTagErrors = {}; this.newTagGeneral = [];
          var body = tagBody(this.newTag, errs);
          if (body === undefined) { this.newTagErrors = errs; return; }
          call("POST", "/api/v1/devices/" + encId(this.id) + "/tags", { body: body, rev: this.rev }).then(function (r) {
            self.savedTag = r.data.name;
            self.newTag = tagForm({ dtype: self.newTag.dtype });
            changed();
            return self.reloadDevice();
          }).catch(function (e) { var s = splitErrors(e, TAG_FIELDS); self.newTagErrors = s.fields; self.newTagGeneral = s.general; });
        },
        // publish map
        mapErr: function (f) { return this.mapErrors[f] || ""; },
        saveMap: function () {
          var self = this, body = {};
          this.mapErrors = {}; this.mapGeneral = []; this.mapNote = "";
          ["sub_device_id", "topic", "env"].forEach(function (k) { if (self.map[k].trim()) body[k] = self.map[k].trim(); });
          if (this.map.stub) body.stub = true;
          call("PUT", "/api/v1/map/devices/" + encId(this.id), { body: body, rev: this.rev }).then(function (r) {
            changed();
            return self.reloadDevice().then(function () { self.mapNote = savedNote(r); });
          }).catch(function (e) { var s = splitErrors(e, ["sub_device_id", "topic", "env", "stub"]); self.mapErrors = s.fields; self.mapGeneral = s.general; });
        },
        removeMap: function () {
          var self = this;
          if (!window.confirm("Remove the publish map entry of " + this.id + "?")) return;
          call("DELETE", "/api/v1/map/devices/" + encId(this.id), { rev: this.rev }).then(function (r) {
            changed();
            return self.reloadDevice().then(function () { self.mapNote = savedNote(r); });
          }).catch(function (e) { self.mapGeneral = splitErrors(e, []).general; });
        },
        // override
        saveOverride: function () {
          var self = this, errs = {};
          this.ovErrors = []; this.ovNote = "";
          var body = parseJsonField(this.overrideText, "override", errs);
          if (body === undefined) { this.ovErrors = [errs.override]; return; }
          var req;
          if (body) req = call("PUT", "/api/v1/overrides/devices/" + encId(this.id), { body: body, rev: this.rev });
          else if (this.hasOverride) req = call("DELETE", "/api/v1/overrides/devices/" + encId(this.id), { rev: this.rev });
          else { this.ovNote = "Nothing to save."; return; }
          req.then(function (r) {
            changed();
            return self.reloadDevice().then(function () { self.ovNote = savedNote(r); });
          }).catch(function (e) { var s = splitErrors(e, []); self.ovErrors = s.general.concat(Object.keys(s.fields).map(function (k) { return k + ": " + s.fields[k]; })); });
        },
      });
    });

    // ------------------------------------------------------------ check
    Alpine.data("checkView", function () {
      return Object.assign({}, helpers, {
        site: "", report: null, error: "",
        init: function () { this.load(); },
        load: function () {
          var self = this;
          this.error = "";
          call("GET", "/api/v1/project/validate" + (this.site.trim() ? "?site=" + enc(this.site.trim()) : "")).then(function (r) { self.report = r; })
            .catch(function (e) { self.error = e.message; });
        },
        ok: function () { return !!this.report && this.report.error === undefined; },
        failed: function () { return !!this.report && this.report.error !== undefined; },
        totals: function () { return (this.report && this.report.totals) || {}; },
        channelTotal: function (ch) { return ch.devices.length > 1 ? " · total " + ch.wire_requests + " req, " + Math.round(ch.est_cycle_ms) + " ms, " + Math.round(ch.load * 100) + "%" : ""; },
        rates: function (d) { var self = this; return (d.rates_ms || []).map(function (r) { return self.rateText(r); }).join(","); },
      });
    });

    // ------------------------------------------------------------ import
    Alpine.data("importView", function () {
      return Object.assign({}, helpers, {
        file: null, fileName: "", report: null, errors: [], busy: false, applied: false,
        pick: function (ev) { this.file = ev.target.files[0] || null; this.fileName = this.file ? this.file.name : ""; this.report = null; this.errors = []; this.applied = false; },
        send: function (dryRun) {
          var self = this;
          if (!this.file) { this.errors = ["Choose a file first."]; return; }
          this.busy = true; this.errors = [];
          var url = "/api/v1/project/import?filename=" + enc(this.fileName) + (dryRun ? "" : "&dry_run=false");
          call("POST", url, { raw: this.file, rev: dryRun ? undefined : this.report.revision }).then(function (r) {
            self.report = r; self.applied = !!r.applied;
            if (r.applied) changed();
          }).catch(function (e) { self.errors = splitErrors(e, []).general; }).then(function () { self.busy = false; });
        },
        preview: function () { this.send(true); },
        apply: function () { if (window.confirm("Write the imported config? The current files are kept as .bak.")) this.send(false); },
        dj: function () { return (this.report && this.report.devices_json) || null; },
        sum: function (part, key) { var d = this.dj(); return d ? d.summary[part][key] : 0; },
        changeCounts: function (part) { return "+" + this.sum(part, "added") + " −" + this.sum(part, "removed") + " ~" + this.sum(part, "changed"); },
        filesText: function () { var f = (this.report && this.report.files) || []; return f.length ? f.join(", ") : "no change"; },
        canApply: function () { return !!this.report && !this.applied && !!(this.report.files || []).length; },
        nothing: function () { return !!this.report && !this.applied && !(this.report.files || []).length; },
        fieldChanges: function (d) { var self = this; return d.fields.map(function (c) { return c.path + ": " + self.short(c.old, 60) + " → " + self.short(c.new, 60); }); },
        tagChanges: function (d) {
          var self = this;
          return d.tags_changed.slice(0, 20).map(function (t) {
            return t.name + ": " + t.changes.map(function (c) { return c.path + " " + self.short(c.old, 30) + " → " + self.short(c.new, 30); }).join("; ");
          });
        },
        firstNames: function (list) { return list.slice(0, 20).join(", ") + (list.length > 20 ? " … (+" + (list.length - 20) + ")" : ""); },
        stubs: function () { return (this.report && this.report.devices_map && this.report.devices_map.stubs_added) || []; },
        textDiffs: function () { var t = (this.report && this.report.text) || {}; return Object.keys(t).map(function (k) { return { name: k, d: t[k] }; }); },
        textState: function (x) { return x.d.unchanged ? "unchanged" : (x.d.created ? "new file" : "changed"); },
        problems: function () { return (this.report && this.report.problems) || []; },
        warnings: function () { return (this.report && this.report.warnings) || []; },
      });
    });

    // ------------------------------------------------------------ audit
    Alpine.data("auditView", function () {
      return Object.assign({}, helpers, {
        action: "", who: "", target: "", entries: [], offset: 0, more: false, error: "", per: 100,
        init: function () { this.load(); },
        load: function () {
          var self = this, q = "?limit=" + (this.per + 1) + "&offset=" + this.offset;
          ["action", "who", "target"].forEach(function (k) { if (self[k].trim()) q += "&" + k + "=" + enc(self[k].trim()); });
          call("GET", "/api/v1/audit" + q).then(function (r) {
            self.more = r.entries.length > self.per; self.entries = r.entries.slice(0, self.per); self.error = "";
          }).catch(function (e) { self.error = e.message; });
        },
        filter: function () { this.offset = 0; this.load(); },
        newer: function () { this.offset = Math.max(0, this.offset - this.per); this.load(); },
        older: function () { this.offset += this.per; this.load(); },
        when: function (e) { return String(e.ts || "").slice(0, 19).replace("T", " "); },
        changeRows: function (e) { var self = this; return (e.changes || []).map(function (c) { return (c.path || "(whole object)") + ": " + self.short(c.old) + " → " + self.short(c.new); }); },
        detailText: function (e) { return e.detail ? this.short(JSON.stringify(e.detail), 300) : ""; },
        filesText: function (e) { return (e.files || []).join(", "); },
      });
    });
  });
})();
