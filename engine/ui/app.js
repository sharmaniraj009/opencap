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
    this.message = typeof d === "string" ? d : (data && data.message) || tr("error.http", { status: status });
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

  // Live values: the WebSocket /api/v1/ws (protocol opencap.v1). A browser cannot set headers on a
  // WebSocket, so the token goes in the subprotocol "opencap.bearer.<base64url(token)>"; the server
  // answers with "opencap.v1" and never echoes it.
  var LIVE_PATH = "/api/v1/ws";
  function b64url(text) {
    var bytes = new TextEncoder().encode(text), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function openLive() {
    var origin = BASE || (location.protocol + "//" + location.host);
    var protocols = ["opencap.v1"], token = readToken();
    if (token) protocols.push("opencap.bearer." + b64url(token));
    return new WebSocket(origin.replace(/^http/, "ws") + LIVE_PATH, protocols);
  }

  var STAGED_KEY = "opencap.import";      // a staged import's id, to reopen the review (#33)
  function readStaged() { try { return sessionStorage.getItem(STAGED_KEY) || ""; } catch (e) { return ""; } }
  function writeStaged(id) {
    try { if (id) { sessionStorage.setItem(STAGED_KEY, id); } else { sessionStorage.removeItem(STAGED_KEY); } } catch (e) { /* private mode */ }
  }

  function enc(s) { return encodeURIComponent(String(s)); }
  function encId(id) { return String(id).split("/").map(encodeURIComponent).join("/"); }   // "<channel>/<device>"

  // Errors of a failed write: {field: message} next to the inputs, the rest above the form.
  function splitErrors(err, known) {
    var fields = {}, general = [];
    if (!(err instanceof ApiError)) { general.push(String(err && err.message || err)); return { fields: fields, general: general }; }
    if (err.status === 412) {
      general.push(tr("error.conflict"));
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
      if (v === null || typeof v !== "object" || Array.isArray(v)) { errors[field] = tr("error.json_object"); return undefined; }
      return v;
    } catch (e) {
      errors[field] = tr("error.json_invalid", { error: e.message });
      return undefined;
    }
  }

  function pretty(v) { return v && Object.keys(v).length ? JSON.stringify(v, null, 2) : ""; }
  function compact(v) { return v && Object.keys(v).length ? JSON.stringify(v) : ""; }

  // ---------------------------------------------------------------- strings (i18n/)
  // Every user-visible string comes from a table per language: i18n/en.json, plus i18n/<code>.json
  // for each other language listed in i18n/languages.json (code -> name in that language). The
  // language follows the browser's preferred languages, falls back to English, and a choice made in
  // the header is kept for this tab (sessionStorage). A key missing from a table falls back to
  // English; a key missing from English shows itself. Templates use t('key', {name: value}) for
  // text ("{name}" is replaced) and x-rich="rich('key', {...})" for text with a little markup.
  var LANG_KEY = "opencap.lang";
  var I18N_DIR = "i18n/";
  var I18N = { lang: "en", table: {}, languages: { en: "English" } };    // copied into Alpine.store("i18n")

  function strings() { return (window.Alpine && Alpine.store("i18n")) || I18N; }
  function hasText(key) { return Object.prototype.hasOwnProperty.call(strings().table, key); }
  function fill(text, vars) {
    if (!vars) return text;
    return text.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, function (m, name) {
      if (!Object.prototype.hasOwnProperty.call(vars, name)) return m;
      var v = vars[name];
      return v === null || v === undefined ? "" : String(v);
    });
  }
  function tr(key, vars) { return fill(hasText(key) ? strings().table[key] : key, vars); }

  function readLang() {
    try { return sessionStorage.getItem(LANG_KEY) || ""; } catch (e) { return ""; }
  }
  function writeLang(code) {
    try { sessionStorage.setItem(LANG_KEY, code); } catch (e) { /* private mode */ }
  }
  // the tab's choice, else the first preferred language with a table (exact, then without region)
  function pickLanguage(codes, chosen, preferred) {
    var byLower = {};
    codes.forEach(function (c) { byLower[c.toLowerCase()] = c; });
    if (chosen && byLower[chosen.toLowerCase()]) return byLower[chosen.toLowerCase()];
    for (var i = 0; i < preferred.length; i++) {
      var p = String(preferred[i] || "").toLowerCase();
      if (byLower[p]) return byLower[p];
      if (byLower[p.split("-")[0]]) return byLower[p.split("-")[0]];
    }
    return "en";
  }
  // the tables are files of this bundle (not the API), loaded relative to the page
  function bundleJson(name) {
    return fetch(I18N_DIR + name, { cache: "no-cache" }).then(function (resp) {
      if (!resp.ok) throw new Error(name + ": HTTP " + resp.status);
      return resp.json();
    });
  }
  var english = bundleJson("en.json");
  function tableFor(code) {
    if (code === "en") return english;
    return Promise.all([english, bundleJson(code + ".json")]).then(function (r) { return Object.assign({}, r[0], r[1]); });
  }
  function useStrings(code, table, languages) {
    var patch = { lang: code, table: table };
    if (languages) patch.languages = languages;
    Object.assign(I18N, patch);
    if (window.Alpine && Alpine.store("i18n")) Object.assign(Alpine.store("i18n"), patch);
    document.documentElement.lang = code;
  }
  var STRINGS_READY = bundleJson("languages.json").catch(function () { return { en: "English" }; }).then(function (languages) {
    var preferred = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
    var code = pickLanguage(Object.keys(languages), readLang(), preferred);
    return tableFor(code).catch(function () { code = "en"; return english; }).then(function (table) { useStrings(code, table, languages); });
  }).catch(function () { /* no table at all: the keys show */ });
  function chooseLanguage(code) {
    writeLang(code);
    return tableFor(code).then(function (table) { useStrings(code, table); }, function () { /* keep the current table */ });
  }

  // x-rich: a table string with a little markup, built as DOM nodes (the CSP build has no x-html).
  // Only <code>, <strong>, <em> and <a href="..."> to a page of this UI or a relative file are made;
  // anything else stays text, and the {name} values are filled into text nodes only.
  var RICH_TAG = /<(\/?)(code|strong|em|a)(?:\s+href="(#\/[^"]*|[A-Za-z][A-Za-z0-9_.\/-]*)")?>/g;
  function renderRich(el, text, vars) {
    var root = document.createDocumentFragment(), stack = [root], last = 0, m;
    function addText(s) { if (s) stack[stack.length - 1].appendChild(document.createTextNode(fill(s, vars))); }
    RICH_TAG.lastIndex = 0;
    while ((m = RICH_TAG.exec(text)) !== null) {
      addText(text.slice(last, m.index));
      last = RICH_TAG.lastIndex;
      if (m[1]) {
        if (stack.length > 1 && stack[stack.length - 1].localName === m[2]) stack.pop();
        continue;
      }
      var node = document.createElement(m[2]);
      if (m[2] === "a" && m[3]) node.setAttribute("href", m[3]);
      stack[stack.length - 1].appendChild(node);
      stack.push(node);
    }
    addText(text.slice(last));
    el.replaceChildren(root);
  }

  // ---------------------------------------------------------------- routing
  function parseHash() {
    var h = (location.hash || "").replace(/^#\/?/, "");
    if (!h) return { name: "tree", arg: "" };
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
    t: function (key, vars) { return tr(key, vars); },
    rich: function (key, vars) { return { text: tr(key), vars: vars || null }; },
    href: href,
    deviceHref: function (id) { return href("device", id); },
    editHref: function (id) { return href("edit", id); },
    quickHref: function (id) { return href("quick", id); },
    channelHref: function (name) { return "#/channel/" + enc(name); },
    isAdmin: function () { return Alpine.store("app").role === "admin"; },
    canEdit: function () { var s = Alpine.store("app"); return s.role === "admin" && s.writable; },
    editBlockedText: function () {
      var s = Alpine.store("app");
      if (s.role !== "admin") return tr(s.open ? "blocked.open" : "blocked.viewer");
      return s.writable ? "" : tr("blocked.readonly", { reason: s.writableReason });
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
      if (!iso) return tr("time.never");
      var s = (Date.now() - Date.parse(iso)) / 1000;
      if (isNaN(s)) return iso;
      if (s < 0) s = 0;
      if (s < 90) return tr("time.s_ago", { n: Math.round(s) });
      if (s < 5400) return tr("time.min_ago", { n: Math.round(s / 60) });
      if (s < 172800) return tr("time.h_ago", { n: Math.round(s / 3600) });
      return tr("time.d_ago", { n: Math.round(s / 86400) });
    },
    ms: function (v) { return v === null || v === undefined ? "—" : tr("unit.ms", { n: Math.round(v) }); },
    pct: function (v) { return v === null || v === undefined ? "—" : tr("unit.pct", { n: Math.round(v * 100) }); },
    stateClass: function (s) { return "badge state-" + (s || "offline"); },
    stateText: function (s) { s = s || "offline"; return hasText("state." + s) ? tr("state." + s) : s; },
    yesNo: function (v) { return tr(v ? "common.yes" : "common.no"); },
    qualityClass: function (q) { return "badge q-" + q; },
    rateText: function (r) { return r % 1000 === 0 ? tr("unit.rate_s", { n: r / 1000 }) : tr("unit.rate_ms", { n: r }); },
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
    Alpine.store("i18n", Object.assign({}, I18N));
    Alpine.directive("rich", function (el, dir, utils) {
      var get = utils.evaluateLater(dir.expression);
      utils.effect(function () { get(function (r) { renderRich(el, (r && r.text) || "", r && r.vars); }); });
    });
    Alpine.store("app", { name: "", role: "", open: false, writable: false, writableReason: "", ready: false, route: parseHash(), pending: null, flash: null });

    // ------------------------------------------------------------ shell
    Alpine.data("shell", function () {
      return Object.assign({}, helpers, {
        init: function () {
          var self = this;
          window.addEventListener("hashchange", function () { self.route(); });
          window.addEventListener("opencap:changed", function () { self.loadPending(); });
          // the page shows once the strings and the caller are known (no flash of empty labels)
          Promise.all([this.whoami(), STRINGS_READY]).then(function (r) { self.signIn(r[0]); self.language = strings().lang; self.route(); });
          this.stopPending = poll(function () { self.loadPending(); }, 30000);
        },
        app: function () { return Alpine.store("app"); },
        view: function (name) { return Alpine.store("app").ready && Alpine.store("app").route.name === name; },
        active: function (names) { return names.split(" ").indexOf(Alpine.store("app").route.name) !== -1; },
        whoami: function () {        // the caller, or null when a login is needed
          return call("GET", "/api/v1/auth-info", { token: "" }).then(function (info) {
            if (!info.open && !readToken()) return null;
            return call("GET", "/api/v1/whoami", { token: readToken() });
          }).catch(function () { return null; });
        },
        signIn: function (me) {
          var s = Alpine.store("app");
          if (me) {
            s.name = me.name; s.role = me.role; s.open = me.open;
            s.writable = !!me.config_writable; s.writableReason = me.config_writable_reason || "";
          } else {
            s.name = ""; s.role = "";
          }
          s.ready = true;
        },
        // language picker (shown when i18n/languages.json lists more than English)
        language: "",
        languageList: function () {
          var l = strings().languages;
          return Object.keys(l).sort().map(function (code) { return { code: code, name: l[code] }; });
        },
        languageChoice: function () { return Object.keys(strings().languages).length > 1; },
        pickLanguage: function () { if (this.language && this.language !== strings().lang) chooseLanguage(this.language); },
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
          if (!p.disk_valid) return tr("pending.disk_invalid", { error: p.error });
          var parts = [];
          var c = p.channels || {};
          if (c.changed && c.changed.length) parts.push(tr("pending.changed", { n: c.changed.length, names: c.changed.slice(0, 3).join(", ") + (c.changed.length > 3 ? ", …" : "") }));
          if (c.added && c.added.length) parts.push(tr("pending.added", { n: c.added.length }));
          if (c.removed && c.removed.length) parts.push(tr("pending.removed", { n: c.removed.length }));
          if (p.poller_changed && p.poller_changed.length) parts.push(tr("pending.poller", { fields: p.poller_changed.join(", ") }));
          var text = tr("pending.text", { parts: parts.join("; ") });
          if (p.restart_needed && p.restart_needed.length) text += " " + tr("pending.restart", { names: p.restart_needed.join(", ") });
          return text;
        },
        canReload: function () { var p = Alpine.store("app").pending; return this.isAdmin() && !!(p && p.disk_valid); },
        reload: function () {
          var self = this;
          call("POST", "/api/v1/reload").then(function (r) {
            var parts = [];
            ["started", "restarted", "stopped"].forEach(function (k) { if (r[k] && r[k].length) parts.push(tr("reload." + k, { names: r[k].join(", ") })); });
            self.flash(tr("reload.done", { what: parts.length ? parts.join("; ") : tr("reload.nothing") }), "ok");
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
            go("tree");
          }).catch(function (e) {
            self.error = e.status === 401 ? tr("login.unknown_token") : e.message;
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
        mqttText: function () { return tr(this.health().mqtt_connected ? "dashboard.mqtt_up" : "dashboard.mqtt_down"); },
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
            self.readNote = tr("device.read_once_at", { time: new Date().toLocaleTimeString(strings().lang) });
            self.refresh();
          }).catch(function (e) { shellFlash(tr("device.read_once_failed", { error: e.message }), "error"); }).then(function () { self.busy = false; });
        },
        qualityCount: function (q) { return this.values.filter(function (v) { return v.quality === q; }).length; },
        countsText: function () {
          var self = this, parts = [tr("common.n_quality", { n: this.qualityCount("good"), quality: "good" })];
          ["bad_comm", "bad_config", "bad_decode", "stale"].forEach(function (q) { var n = self.qualityCount(q); if (n) parts.push(tr("common.n_quality", { n: n, quality: q })); });
          return parts.join(" · ");
        },
        subtitle: function () {
          var d = this.detail;
          if (!d) return "";
          var text = tr("device.subtitle", { driver: d.driver, host: d.host, port: d.port, topic: d.topic, sub: d.sub_device_id || tr("common.none") });
          return d.enabled ? text : text + " · " + tr("device.disabled");
        },
        planText: function () {
          var p = this.plan;
          if (!p || p.plan_error) return "";
          return tr("device.plan_text", { tags: p.enabled_tags, blocks: p.blocks, requests: p.wire_requests,
                                          ms: Math.round(p.est_cycle_ms), pct: Math.round(p.load * 100) });
        },
        invalidRows: function () { var inv = (this.plan && this.plan.invalid) || {}; return Object.keys(inv).sort().map(function (k) { return { name: k, reason: inv[k] }; }); },
        blockRows: function () { return (this.plan && this.plan.block_plan) || []; },
      });
    });

    // ------------------------------------------------------------ Quick Client (live values)
    var QUICK_ROWS = 1000;          // rows rendered at once; the filter narrows the rest
    Alpine.data("quickView", function () {
      // the socket and the row index live outside Alpine's reactive state (a proxied WebSocket breaks)
      var sock = null, index = {}, retry = null, failures = 0, everOpen = false, closing = false, stopPoll = null;
      return Object.assign({}, helpers, {
        channels: [], q: "", tq: "", id: "", rows: [], loaded: false, version: 0, unavailable: "", error: "",
        devError: "", busy: false, readNote: "", mode: "connecting", now: Date.now(), config: {},
        init: function () {
          var self = this;
          routed(this, this.select);
          this.loadDevices();
          this._stop = poll(function () { self.loadDevices(); }, 10000);
          this._clock = setInterval(function () { self.now = Date.now(); }, 1000);
          this.select();
          this.connect();
        },
        destroy: function () {
          closing = true;
          unrouted(this);
          clearInterval(this._clock);
          if (retry) clearTimeout(retry);
          if (stopPoll) stopPoll();
          if (sock) sock.close();
          sock = null;
        },
        // --- devices
        loadDevices: function () {
          var self = this;
          call("GET", "/api/v1/status").then(function (d) { self.channels = d.channels || []; self.devError = ""; })
            .catch(function (e) { self.devError = e.message; });
        },
        tree: function () {
          var q = this.q.trim().toLowerCase(), out = [];
          this.channels.forEach(function (ch) {
            var devices = ch.devices.filter(function (d) { return !q || d.id.toLowerCase().indexOf(q) !== -1; });
            if (devices.length) out.push({ name: ch.name, devices: devices });
          });
          return out;
        },
        deviceName: function (d, ch) { return d.id.indexOf(ch.name + "/") === 0 ? d.id.slice(ch.name.length + 1) : d.id; },
        stateDot: function (s) { return "dot state-" + (s || "offline"); },
        select: function () {
          var r = Alpine.store("app").route;
          if (r.name !== "quick") return;
          this.id = r.arg; this.rows = []; index = {}; this.loaded = false; this.version = 0;
          this.unavailable = ""; this.error = ""; this.readNote = ""; this.config = {};
          if (!this.id) { this.subscribe(); return; }
          var self = this, id = this.id;
          // address and type from the configuration (best effort: the feed works without it)
          call("GET", "/api/v1/devices/" + encId(id) + "/tags").then(function (t) {
            if (self.id !== id) return;
            var cfg = {};
            (t.tags || []).forEach(function (tag) { cfg[tag.name] = tag; });
            self.config = cfg;
            self.rows.forEach(function (row) { self.describe(row); });
          }).catch(function () { /* no config directory, or not in the config */ });
          this.subscribe();
          if (this.mode === "polling") this.pollOnce();
        },
        describe: function (row) {
          var c = this.config[row.tag];
          row.address = c ? c.address : ""; row.dtype = c ? c.dtype : "";
        },
        // --- the socket
        connect: function () {
          var self = this;
          if (closing) return;
          try { sock = openLive(); } catch (e) { this.fallback(); return; }
          this.mode = "connecting";
          sock.onopen = function () { everOpen = true; failures = 0; self.mode = "live"; self.subscribe(); };
          sock.onmessage = function (ev) {
            var m;
            try { m = JSON.parse(ev.data); } catch (e) { return; }
            self.receive(m);
          };
          sock.onclose = function () {
            sock = null;
            if (closing) return;
            failures += 1;
            // never opened three times in a row: this engine has no live feed; poll the REST API instead
            if (!everOpen && failures >= 3) { self.fallback(); return; }
            self.mode = "connecting";
            retry = setTimeout(function () { retry = null; self.connect(); }, Math.min(15000, 1000 * Math.pow(2, failures - 1)));
          };
        },
        subscribe: function () {
          if (!sock || sock.readyState !== 1) return;
          sock.send(JSON.stringify({ subscribe: { devices: this.id ? [this.id] : [] } }));
        },
        receive: function (m) {
          if (m.type === "error") { this.error = m.detail; return; }
          if (m.device !== this.id) return;
          if (m.type === "unavailable") { this.unavailable = m.detail; this.rows = []; index = {}; this.loaded = true; return; }
          if (m.type === "snapshot") { this.unavailable = ""; this.version = m.version; this.replace(m.changes); return; }
          if (m.type === "changes" && m.version > this.version) {
            this.version = m.version;
            var self = this;
            m.changes.forEach(function (c) { self.apply(c.tag, c); });
          }
        },
        replace: function (list) {
          var self = this, now = Date.now();
          var rows = list.map(function (c) {
            var old = index[c.tag];
            var row = { tag: c.tag, value: c.value, quality: c.quality, ts: c.source_ts, updates: old ? old.updates : 0,
                        changedAt: 0, address: "", dtype: "" };
            if (old && (old.value !== c.value || old.quality !== c.quality || old.ts !== c.source_ts)) { row.updates += 1; row.changedAt = now; }
            self.describe(row);
            return row;
          });
          rows.sort(function (a, b) { return a.tag < b.tag ? -1 : (a.tag > b.tag ? 1 : 0); });
          this.rows = rows;
          index = {};
          this.rows.forEach(function (r) { index[r.tag] = r; });    // the reactive rows
          this.loaded = true;
        },
        apply: function (tag, tv) {
          var row = index[tag];
          if (!row) return;
          if (row.value === tv.value && row.quality === tv.quality && row.ts === tv.source_ts) return;
          row.value = tv.value; row.quality = tv.quality; row.ts = tv.source_ts;
          row.updates += 1; row.changedAt = Date.now();
        },
        // --- REST fallback (an engine without /api/v1/ws)
        fallback: function () {
          var self = this;
          this.mode = "polling";
          if (stopPoll) return;
          this.pollOnce();
          stopPoll = poll(function () { self.pollOnce(); }, 2000);
        },
        pollOnce: function () {
          var self = this, id = this.id;
          if (!id) return;
          call("GET", "/api/v1/devices/" + encId(id) + "/values").then(function (v) {
            if (self.id !== id) return;
            self.unavailable = "";
            var list = Object.keys(v).map(function (k) { return { tag: k, value: v[k].value, quality: v[k].quality, source_ts: v[k].source_ts }; });
            if (!self.loaded || list.length !== self.rows.length) self.replace(list);
            else list.forEach(function (c) { self.apply(c.tag, c); });
          }).catch(function (e) {
            if (self.id !== id) return;
            if (e.status === 404) { self.unavailable = e.message; self.rows = []; index = {}; self.loaded = true; } else self.error = e.message;
          });
        },
        modeText: function () { return tr("quick.mode_" + this.mode); },
        modeClass: function () { return "badge mode-" + this.mode; },
        modeTitle: function () {
          return tr("quick.title_" + this.mode);
        },
        // --- demand read
        readDisabled: function () { return this.busy || !!this.unavailable; },
        readNow: function () {
          var self = this, id = this.id;
          this.busy = true; this.readNote = "";
          call("POST", "/api/v1/devices/" + encId(id) + "/read-once").then(function (v) {
            if (self.id !== id) return;
            Object.keys(v).forEach(function (k) { self.apply(k, v[k]); });
            self.readNote = tr("quick.read_now_at", { time: new Date().toLocaleTimeString(strings().lang) });
          }).catch(function (e) { shellFlash(tr("quick.read_now_failed", { error: e.message }), "error"); }).then(function () { self.busy = false; });
        },
        // --- table
        shown: function () {
          var q = this.tq.trim().toLowerCase();
          var list = q ? this.rows.filter(function (r) { return r.tag.toLowerCase().indexOf(q) !== -1 || (r.address || "").toLowerCase().indexOf(q) !== -1; }) : this.rows;
          return list.length > QUICK_ROWS ? list.slice(0, QUICK_ROWS) : list;
        },
        shownNote: function () {
          var q = this.tq.trim().toLowerCase(), n = this.rows.length;
          if (q) n = this.rows.filter(function (r) { return r.tag.toLowerCase().indexOf(q) !== -1 || (r.address || "").toLowerCase().indexOf(q) !== -1; }).length;
          return n > QUICK_ROWS ? tr("quick.shown_note", { shown: QUICK_ROWS, n: n }) : "";
        },
        countsText: function () {
          var counts = {}, parts = [];
          this.rows.forEach(function (r) { counts[r.quality] = (counts[r.quality] || 0) + 1; });
          parts.push(tr("quick.tags_n", { n: this.rows.length }));
          ["good", "bad_comm", "bad_config", "bad_decode", "stale"].forEach(function (q) { if (counts[q]) parts.push(tr("common.n_quality", { n: counts[q], quality: q })); });
          return parts.join(" · ");
        },
        rowClass: function (r) { return this.now - r.changedAt < 1500 ? "changed" : ""; },
        valueClass: function (r) { return "num" + (r.quality === "good" ? "" : " old-value"); },
        tsText: function (r) { return r.ts ? String(r.ts).replace("T", " ").replace(/(\.\d{3})\d*/, "$1") : tr("time.never"); },
        ageSeconds: function (r) { var t = r.ts ? Date.parse(r.ts) : NaN; return isNaN(t) ? null : Math.max(0, (this.now - t) / 1000); },
        age: function (r) {
          var s = this.ageSeconds(r);
          if (s === null) return "—";
          if (s < 60) return tr("unit.s", { n: Math.floor(s) });
          if (s < 3600) return tr("unit.min", { n: Math.floor(s / 60) });
          if (s < 172800) return tr("unit.h", { n: Math.floor(s / 3600) });
          return tr("unit.d", { n: Math.floor(s / 86400) });
        },
        ageClass: function (r) { var s = this.ageSeconds(r); return "small nowrap" + (s !== null && s > 60 ? " age-old" : ""); },
      });
    });

    // ------------------------------------------------------------ project tree (KEPServerEX-style, read-only)
    // Channels > Devices > Tags (plus the _System tags) on the left, the properties of the selected node on the
    // right. It uses only the routes every engine serves: /api/v1/status, /devices/<id>/status and /values, and
    // /system/tags. The route is #/tree/<kind>/<path>: ch/<channel>, dev/<channel>/<device>,
    // tag/<channel>/<device>/<tag>, sys, sys/<path>.
    var TREE_TAG_ROWS = 500;           // tag rows drawn per device; the filter narrows the rest
    var TREE_REFRESH_DEVICES = 25;     // expanded devices refreshed at once
    var TREE_LOAD_ALL = 60;            // devices loaded to search their tags
    Alpine.data("treeView", function () {
      return Object.assign({}, helpers, {
        data: null, error: "", q: "", initialOpen: false, opened: {}, devs: {}, sys: [], sysLoaded: false, diag: { scope: "", items: {} }, sel: { kind: "root", id: "", tag: "", path: "" },
        init: function () {
          var self = this;
          routed(this, this.select);
          this.select();
          this.loadStatus();
          this.loadSystem();
          this._stopStatus = poll(function () { self.loadStatus(); }, 5000);
          this._stopDevs = poll(function () { self.refreshDevices(); }, 2000);
        },
        destroy: function () { if (this._stopStatus) this._stopStatus(); if (this._stopDevs) this._stopDevs(); unrouted(this); },

        // ---- route -> selection
        select: function () {
          var r = Alpine.store("app").route;
          if (r.name !== "tree") return;
          var parts = r.arg ? r.arg.split("/") : [];
          var kind = parts[0] || "root", s = { kind: "root", id: "", tag: "", path: "" };
          if (kind === "ch" && parts.length >= 2) { s.kind = "channel"; s.id = parts.slice(1).join("/"); }
          else if (kind === "dev" && parts.length >= 3) { s.kind = "device"; s.id = parts[1] + "/" + parts[2]; }
          else if (kind === "tag" && parts.length >= 4) { s.kind = "tag"; s.id = parts[1] + "/" + parts[2]; s.tag = parts.slice(3).join("/"); }
          else if (kind === "sys") { s.kind = parts.length > 1 ? "systag" : "sys"; s.path = parts.slice(1).join("/"); }
          this.sel = s;
          var o = Object.assign({}, this.opened);
          o.root = true;
          if (s.kind === "channel" || s.kind === "device" || s.kind === "tag") o["ch:" + this.channelOf(s.id)] = true;
          if (s.kind === "device" || s.kind === "tag") o["dev:" + s.id] = true;
          if (s.kind === "sys" || s.kind === "systag") o.sys = true;
          this.opened = o;
          if (s.id && (s.kind === "device" || s.kind === "tag")) this.loadDevice(s.id);
          this.loadDiagnostics();
        },
        channelOf: function (id) { var i = String(id).indexOf("/"); return i === -1 ? String(id) : String(id).slice(0, i); },

        // ---- data
        loadStatus: function () {
          var self = this;
          return call("GET", "/api/v1/status").then(function (d) {
            self.data = d; self.error = "";
            if (!self.initialOpen) {
              self.initialOpen = true;
              var o = Object.assign({}, self.opened);
              (d.channels || []).forEach(function (c) { if (o["ch:" + c.name] === undefined) o["ch:" + c.name] = true; });
              self.opened = o;
            }
          }).catch(function (e) { self.error = e.message; });
        },
        loadSystem: function () {
          var self = this;
          return call("GET", "/api/v1/system/tags").then(function (r) {
            self.sys = (r.items || []).slice().sort(function (a, b) { return a.path < b.path ? -1 : a.path > b.path ? 1 : 0; });
            self.sysLoaded = true;
          }).catch(function () { self.sys = []; self.sysLoaded = false; });
        },
        loadDevice: function (id) {
          var self = this, base = "/api/v1/devices/" + encId(id);
          return Promise.all([call("GET", base + "/status"), call("GET", base + "/values")]).then(function (r) {
            var values = r[1], names = Object.keys(values).sort();
            var m = Object.assign({}, self.devs);
            m[id] = { detail: r[0], names: names, values: values, error: "", loaded: true };
            self.devs = m;
          }).catch(function (e) {
            var m = Object.assign({}, self.devs), old = m[id] || { names: [], values: {}, detail: null };
            m[id] = { detail: old.detail, names: old.names, values: old.values, error: e.status === 404 ? tr("tree.not_polled") : e.message, loaded: true };
            self.devs = m;
          });
        },
        refreshDevices: function () {
          var self = this, ids = [];
          if (this.sel.id && (this.sel.kind === "device" || this.sel.kind === "tag")) ids.push(this.sel.id);
          Object.keys(this.opened).forEach(function (k) {
            if (self.opened[k] && k.indexOf("dev:") === 0 && ids.indexOf(k.slice(4)) === -1) ids.push(k.slice(4));
          });
          ids.slice(0, TREE_REFRESH_DEVICES).forEach(function (id) { self.loadDevice(id); });
          if (this.sel.kind === "sys" || this.sel.kind === "systag" || this.opened.sys) this.loadSystem();
          this.loadDiagnostics();
        },
        // ---- diagnostics: the statistics and health system tags of the selected node (DIAG-10), read from
        // GET /api/v1/system/tags with the node's prefix. Keys are the paths without that prefix.
        diagScope: function () {
          var s = this.sel;
          if (s.kind === "root") return { scope: "root", prefix: "_System" };
          if (s.kind === "channel") return { scope: "ch:" + s.id, prefix: s.id + "._" };
          if (s.kind === "device" || s.kind === "tag") return { scope: "dev:" + s.id, prefix: s.id.replace("/", ".") + "._" };
          return null;
        },
        loadDiagnostics: function () {
          var self = this, sc = this.diagScope();
          if (!sc) { if (this.diag.scope) this.diag = { scope: "", items: {} }; return; }
          call("GET", "/api/v1/system/tags?prefix=" + enc(sc.prefix)).then(function (r) {
            var cur = self.diagScope();
            if (!cur || cur.scope !== sc.scope) return;
            var items = {}, cut = sc.scope === "root" ? 0 : sc.prefix.length - 1;
            (r.items || []).forEach(function (t) { items[sc.scope === "root" ? t.path : t.path.slice(cut)] = t; });
            self.diag = { scope: sc.scope, items: items };
          }).catch(function () { self.diag = { scope: sc.scope, items: {} }; });
        },
        dv: function (key) { var t = this.diag.items[key]; return t ? t.value : undefined; },
        dn: function (key) { var v = this.dv(key); return typeof v === "number" ? v : null; },
        duration: function (secs) {
          if (secs === null || secs === undefined) return "—";
          secs = Math.floor(secs);
          var d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60;
          if (d) return tr("tree.dur_d", { d: d, h: h });
          if (h) return tr("tree.dur_h", { h: h, m: m });
          if (m) return tr("tree.dur_m", { m: m, s: s });
          return tr("tree.dur_s", { s: s });
        },
        bytes: function (n) {
          if (n === null || n === undefined) return "—";
          var u = ["B", "KB", "MB", "GB"], i = 0;
          while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
          return (i ? n.toFixed(1) : String(n)) + " " + u[i];
        },
        // cards (the figures worth a glance) and rows (the rest) for the selected node
        diagCards: function () {
          var self = this, sc = this.diagScope(), out = [];
          if (!sc || this.diag.scope !== sc.scope || !Object.keys(this.diag.items).length) return out;
          function card(label, value, cls) { out.push({ label: label, value: value === null || value === undefined ? "—" : String(value), cls: cls || "" }); }
          if (sc.scope === "root") {
            card(tr("tree.uptime"), this.duration(this.dn("_System._UptimeS")));
            card(tr("tree.total_tags"), this.dv("_System._TotalTagCount"));
            card(tr("tree.active_tags"), this.dv("_System._ActiveTagCount"));
            card(tr("tree.clients"), this.dv("_System._ClientCount"));
            card(tr("tree.devices_ok"), this.dv("_System._DevicesOk"), "ok");
            card(tr("tree.devices_offline"), this.dv("_System._DevicesOffline"), this.dn("_System._DevicesOffline") ? "bad" : "");
            card(tr("tree.devices_demoted"), this.dv("_System._DevicesDemoted"), this.dn("_System._DevicesDemoted") ? "warn" : "");
            card(tr("tree.outbox_messages"), this.dv("_System._OutboxMessages"), this.dn("_System._OutboxMessages") > 100 ? "warn" : "");
          } else {
            var failed = this.dn("_Statistics._FailedReads"), t = this.dn("_Statistics._Timeouts"), c = this.dn("_Statistics._ConnErrors");
            card(tr("tree.reads_ok"), this.dv("_Statistics._SuccessfulReads"), "ok");
            card(tr("tree.reads_failed"), this.dv("_Statistics._FailedReads"), failed ? "bad" : "");
            card(tr("tree.timeouts"), this.dv("_Statistics._Timeouts"), t ? "warn" : "");
            card(tr("tree.conn_errors"), this.dv("_Statistics._ConnErrors"), c ? "warn" : "");
            card(tr("tree.overruns"), this.dv(sc.scope.indexOf("ch:") === 0 ? "_Statistics._Overruns" : "_System._Overruns"));
            if (sc.scope.indexOf("dev:") === 0) card(tr("tree.seconds_in_error"), this.dv("_System._SecondsInError"), this.dn("_System._SecondsInError") ? "bad" : "");
            else card(tr("tree.pending_reads"), this.dv("_Statistics._PendingReads"));
          }
          return out;
        },
        diagRows: function () {
          var self = this, sc = this.diagScope(), rows = [];
          if (!sc || this.diag.scope !== sc.scope || !Object.keys(this.diag.items).length) return rows;
          function add(label, value, cls) { if (value !== undefined) rows.push({ label: label, value: value === null || value === "" ? "—" : String(value), cls: cls || "" }); }
          if (sc.scope === "root") {
            add(tr("tree.mqtt"), this.dv("_System._MqttConnected") === undefined ? undefined : tr(this.dv("_System._MqttConnected") ? "dashboard.mqtt_up" : "dashboard.mqtt_down"), this.dv("_System._MqttConnected") ? "ok" : "bad");
            add(tr("tree.outbox_age"), this.duration(this.dn("_System._OutboxAgeS")));
            add(tr("tree.outbox_dropped"), this.dv("_System._OutboxDropped"), this.dn("_System._OutboxDropped") ? "warn" : "");
            add(tr("tree.devices_disabled"), this.dv("_System._DevicesDisabled"));
            add(tr("tree.revision"), this.dv("_System._ProjectRevision"));
            add(tr("tree.engine"), this.dv("_System._Engine") === undefined ? undefined : this.dv("_System._Engine") + " " + (this.dv("_System._ProductVersion") || ""));
            var mq = "_System._North.mqtt.";
            if (this.diag.items[mq + "_Published"]) {
              add(tr("tree.mqtt_published"), this.dv(mq + "_Published"));
              add(tr("tree.forward_mode"), this.dv(mq + "_ForwardMode"));
              add(tr("tree.outbox_size"), this.bytes(this.dn(mq + "_OutboxBytes")) + " / " + this.bytes(this.dn(mq + "_OutboxCapacityBytes")));
              if (this.dv(mq + "_LastError")) add(tr("tree.last_error"), this.dv(mq + "_LastError"), "bad");
            }
            add(tr("tree.redundancy"), this.dv("_System._Redundancy._Mode") === undefined ? undefined : this.dv("_System._Redundancy._Mode") + " / " + this.dv("_System._Redundancy._Role"));
          } else {
            var ok = this.dn("_Statistics._RequestsOk"), bad = (this.dn("_Statistics._Timeouts") || 0) + (this.dn("_Statistics._ConnErrors") || 0) +
              (this.dn("_Statistics._BadConfig") || 0) + (this.dn("_Statistics._OtherErrors") || 0);
            if (ok !== null) add(tr("tree.success_rate"), ok + bad === 0 ? "—" : (100 * ok / (ok + bad)).toFixed(ok + bad < 1000 ? 1 : 2) + " %");
            add(tr("tree.requests_ok"), this.dv("_Statistics._RequestsOk"));
            add(tr("tree.bad_config"), this.dv("_Statistics._BadConfig"), this.dn("_Statistics._BadConfig") ? "warn" : "");
            add(tr("tree.other_errors"), this.dv("_Statistics._OtherErrors"), this.dn("_Statistics._OtherErrors") ? "warn" : "");
            if (sc.scope.indexOf("ch:") === 0) add(tr("tree.max_pending"), this.dv("_Statistics._MaxPendingReads"));
            if (sc.scope.indexOf("dev:") === 0) {
              add(tr("tree.error_count"), this.dv("_System._ErrorCount"));
              add(tr("tree.auto_demoted"), this.dv("_System._AutoDemoted") === undefined ? undefined : this.yesNo(this.dv("_System._AutoDemoted")), this.dv("_System._AutoDemoted") ? "warn" : "");
            }
          }
          return rows;
        },
        showDiag: function () { return this.diagCards().length > 0; },
        cardClass: function (c) { return "value " + c.cls; },
        searchChanged: function () {
          var self = this;
          if (!this.q.trim()) return;
          var n = 0;
          this.deviceList().forEach(function (d) {
            if (n < TREE_LOAD_ALL && !self.devs[d.id]) { n += 1; self.loadDevice(d.id); }
          });
        },

        // ---- the tree
        channelList: function () { return (this.data && this.data.channels) || []; },
        deviceList: function () {
          var out = [];
          this.channelList().forEach(function (c) { c.devices.forEach(function (d) { out.push(d); }); });
          return out;
        },
        deviceName: function (d, chName) { return d.id.indexOf(chName + "/") === 0 ? d.id.slice(chName.length + 1) : d.id; },
        isOpen: function (key) { return !!this.opened[key]; },
        toggle: function (key) { var o = Object.assign({}, this.opened); o[key] = !o[key]; this.opened = o; if (key.indexOf("dev:") === 0) this.loadDevice(key.slice(4)); },
        toggleRow: function (r) { if (r.expandable) this.toggle(r.key); },
        matches: function (text) { var q = this.q.trim().toLowerCase(); return !q || String(text).toLowerCase().indexOf(q) !== -1; },
        tagHref: function (id, tag) { return "#/tree/tag/" + encId(id) + "/" + tag.split("/").map(encodeURIComponent).join("/"); },
        rows: function () {
          var self = this, out = [], searching = !!this.q.trim();
          var s = this.sel;
          function row(key, kind, depth, label, extra) {
            var r = { key: key, kind: kind, depth: depth, cls: "tree-row d" + depth, label: label, expandable: false, open: false, state: "", quality: "", hint: "", href: "", selected: false, level: depth + 1 };
            return Object.assign(r, extra || {});
          }
          var rootOpen = searching || this.isOpen("root");
          out.push(row("root", "root", 0, tr("tree.root"), { expandable: true, open: rootOpen, href: "#/tree", selected: s.kind === "root" }));
          if (!rootOpen) return this.finishRows(out);
          this.channelList().forEach(function (ch) {
            var devs = [], chHit = self.matches(ch.name);
            ch.devices.forEach(function (d) {
              var name = self.deviceName(d, ch.name), entry = self.devs[d.id], tags = [];
              var devHit = chHit || self.matches(name);
              if (entry && entry.names) {
                entry.names.forEach(function (t) { if (!searching || devHit || self.matches(t)) tags.push(t); });
              }
              if (!searching || devHit || tags.length) devs.push({ d: d, name: name, tags: tags, entry: entry, devHit: devHit });
            });
            if (searching && !chHit && !devs.length) return;
            var cKey = "ch:" + ch.name, cOpen = searching || self.isOpen(cKey);
            out.push(row(cKey, "channel", 1, ch.name, { expandable: true, open: cOpen, href: "#/tree/ch/" + encId(ch.name), hint: String(ch.devices.length),
                                                        selected: s.kind === "channel" && s.id === ch.name }));
            if (!cOpen) return;
            devs.forEach(function (e) {
              var dKey = "dev:" + e.d.id, dOpen = (searching && e.tags.length > 0) || self.isOpen(dKey);
              out.push(row(dKey, "device", 2, e.name, { expandable: true, open: dOpen, state: e.d.state || "offline", href: "#/tree/dev/" + encId(e.d.id),
                                                        hint: e.entry && e.entry.names ? String(e.entry.names.length) : "",
                                                        selected: s.kind === "device" && s.id === e.d.id }));
              if (!dOpen) return;
              if (e.entry && e.entry.error) out.push(row(dKey + "!err", "note", 3, e.entry.error, {}));
              e.tags.slice(0, TREE_TAG_ROWS).forEach(function (t) {
                var v = e.entry.values[t] || {};
                out.push(row("tag:" + e.d.id + "/" + t, "tag", 3, t, { quality: v.quality || "", hint: self.fmt(v.value), href: self.tagHref(e.d.id, t),
                                                                       selected: s.kind === "tag" && s.id === e.d.id && s.tag === t }));
              });
              if (e.tags.length > TREE_TAG_ROWS) out.push(row(dKey + "!more", "note", 3, tr("tree.more", { n: e.tags.length - TREE_TAG_ROWS }), {}));
            });
          });
          if (this.sysLoaded) {
            var sysHits = this.sys.filter(function (t) { return !searching || self.matches(t.path); });
            if (!searching || sysHits.length) {
              var sOpen = searching || this.isOpen("sys");
              out.push(row("sys", "sys", 1, "_System", { expandable: true, open: sOpen, href: "#/tree/sys", hint: String(this.sys.length), selected: s.kind === "sys" }));
              if (sOpen) sysHits.forEach(function (t) {
                out.push(row("systag:" + t.path, "systag", 2, t.path.replace(/^_System\./, ""), { quality: t.quality || "", hint: self.fmt(t.value),
                                                                                                   href: "#/tree/sys/" + encId(t.path), selected: s.kind === "systag" && s.path === t.path }));
              });
            }
          }
          return this.finishRows(out);
        },
        finishRows: function (out) {
          if (out.length === 1 && !this.channelList().length && this.data) out.push({ key: "empty", kind: "note", depth: 1, cls: "tree-row d1", label: tr("tree.empty"), expandable: false, open: false, state: "", quality: "", hint: "", href: "", selected: false, level: 2 });
          return out;
        },
        rowClass: function (r) { return r.selected ? r.cls + " selected" : r.cls; },
        rowAria: function (r) { return r.selected ? "true" : null; },
        isLink: function (r) { return !!r.href; },
        isNote: function (r) { return r.kind === "note"; },
        dotClass: function (r) { return r.state ? "dot state-" + r.state : "dot hidden"; },
        showQuality: function (r) { return r.kind === "tag" || r.kind === "systag"; },
        toggleLabel: function (r) { return tr(r.open ? "tree.collapse" : "tree.expand", { name: r.label }); },
        onKey: function (e) {
          var li = e.target && e.target.closest ? e.target.closest("li") : null;
          if (!li) return;
          var key = li.getAttribute("data-key"), list = document.getElementById("tree-list");
          var links = Array.prototype.slice.call(list.querySelectorAll("li .tree-link"));
          var cur = links.indexOf(li.querySelector(".tree-link")), next = -1;
          var r = this.rows().filter(function (x) { return x.key === key; })[0];
          if (e.key === "ArrowDown") next = cur + 1;
          else if (e.key === "ArrowUp") next = cur - 1;
          else if (e.key === "Home") next = 0;
          else if (e.key === "End") next = links.length - 1;
          else if (e.key === "ArrowRight" && r && r.expandable && !r.open) { this.toggle(key); e.preventDefault(); return; }
          else if (e.key === "ArrowLeft" && r && r.expandable && r.open) { this.toggle(key); e.preventDefault(); return; }
          else return;
          e.preventDefault();
          if (next >= 0 && next < links.length) links[next].focus();
        },

        // ---- properties panel
        current: function () {
          var s = this.sel, e = s.id ? this.devs[s.id] : null;
          return e || null;
        },
        title: function () {
          var s = this.sel;
          if (s.kind === "channel" || s.kind === "device") return s.id;
          if (s.kind === "tag") return s.id + " · " + s.tag;
          if (s.kind === "sys") return "_System";
          if (s.kind === "systag") return s.path;
          return tr("tree.root");
        },
        kindText: function () { return tr("tree.kind_" + this.sel.kind); },
        props: function () {
          var s = this.sel, self = this, rows = [];
          function add(label, value, cls) { rows.push({ label: label, value: value === null || value === undefined || value === "" ? "—" : String(value), cls: cls || "" }); }
          if (s.kind === "root") {
            var h = (this.data && this.data.health) || { devices: {} }, dv = h.devices || {};
            add(tr("tree.instance"), h.instance); add(tr("tree.version"), h.version);
            add(tr("common.channels"), this.channelList().length); add(tr("common.devices"), this.deviceList().length);
            add(tr("tree.states"), tr("tree.states_text", { ok: dv.ok || 0, offline: dv.offline || 0, demoted: dv.demoted || 0 }));
            add(tr("dashboard.mqtt_link"), tr(h.mqtt_connected ? "dashboard.mqtt_up" : "dashboard.mqtt_down"), h.mqtt_connected ? "ok" : "bad");
            add(tr("dashboard.outbox"), h.saf_queue);
            if (h.initial_scan_ms !== undefined && h.initial_scan_ms !== null) add(tr("tree.initial_scan"), tr("unit.ms", { n: Math.round(h.initial_scan_ms) }));
          } else if (s.kind === "channel") {
            var ch = this.channelList().filter(function (c) { return c.name === s.id; })[0];
            if (ch) {
              var counts = {}, drivers = {}, errs = 0, last = "";
              ch.devices.forEach(function (d) {
                counts[d.state || "offline"] = (counts[d.state || "offline"] || 0) + 1; drivers[d.driver] = true; errs += d.errors || 0;
                if (d.last_good && d.last_good > last) last = d.last_good;
              });
              add(tr("common.name"), ch.name); add(tr("common.devices"), ch.devices.length);
              add(tr("tree.drivers"), Object.keys(drivers).sort().join(", "));
              add(tr("tree.states"), tr("tree.states_text", { ok: counts.ok || 0, offline: counts.offline || 0, demoted: counts.demoted || 0 }));
              add(tr("common.errors"), errs); add(tr("common.last_good"), last ? this.ago(last) : tr("time.never"));
            }
          } else if (s.kind === "device") {
            var e = this.current(), d = e && e.detail;
            if (d) {
              add(tr("common.channel"), d.channel); add(tr("common.driver"), d.driver); add(tr("common.host"), d.host); add(tr("common.port"), d.port);
              add(tr("common.enabled"), this.yesNo(d.enabled)); add(tr("common.state"), this.stateText(d.state), "state-" + (d.state || "offline"));
              add(tr("tree.connection"), tr(d.connected ? "device.connected" : "device.not_connected"));
              add(tr("common.tags"), d.tags); add(tr("tree.blocks"), d.blocks);
              add(tr("common.last_good"), this.ago(d.last_good)); add(tr("common.errors"), d.errors); add(tr("common.cycle"), this.ms(d.cycle_ms));
              add(tr("tree.topic"), d.topic); add(tr("common.subdeviceid"), d.sub_device_id || tr("common.none"));
              if (d.last_error) add(tr("tree.last_error"), d.last_error, "bad");
              if (d.last_reason) add(tr("tree.last_reason"), d.last_reason);
              if (d.plan_error) add(tr("tree.plan_error"), d.plan_error, "bad");
              if (d.bad_config && d.bad_config.length) add(tr("common.bad_config_tags"), d.bad_config.join(", "), "warn");
            }
          } else if (s.kind === "tag") {
            var te = this.current(), v = te && te.values ? te.values[s.tag] : null;
            if (v) {
              add(tr("common.tag"), s.tag); add(tr("common.device"), s.id); add(tr("common.value"), this.fmt(v.value));
              add(tr("common.quality"), v.quality, "q-" + v.quality); add(tr("common.source_time"), v.source_ts); add(tr("tree.age"), this.ago(v.source_ts));
            }
          } else if (s.kind === "sys") {
            add(tr("common.tags"), this.sys.length);
          } else if (s.kind === "systag") {
            var t = this.sys.filter(function (x) { return x.path === s.path; })[0];
            if (t) { add(tr("tree.path"), t.path); add(tr("tree.dtype"), t.dtype); add(tr("common.value"), this.fmt(t.value)); add(tr("common.quality"), t.quality, "q-" + t.quality); add(tr("common.source_time"), t.source_ts); }
          }
          return rows;
        },
        valueClass: function (p) {
          if (p.cls.indexOf("q-") === 0 || p.cls.indexOf("state-") === 0) return "badge " + p.cls;
          return p.cls === "ok" || p.cls === "bad" || p.cls === "warn" ? p.cls : "";
        },
        expandedAttr: function (r) { return r.expandable ? (r.open ? "true" : "false") : null; },
        devTreeHref: function (d) { return "#/tree/dev/" + encId(d.id); },
        missing: function () {
          var s = this.sel;
          if (!this.data) return false;
          if (s.kind === "channel") return !this.channelList().some(function (c) { return c.name === s.id; });
          if (s.kind === "device") return !this.deviceList().some(function (d) { return d.id === s.id; });
          if (s.kind === "tag") { var e = this.current(); return !!e && e.loaded && !e.values[s.tag] && !e.error; }
          if (s.kind === "systag") return this.sysLoaded && !this.sys.some(function (x) { return x.path === s.path; });
          return false;
        },
        hasDevice: function () { return this.sel.kind === "device" || this.sel.kind === "tag"; },
        showMeta: function () { return this.sel.kind === "tag"; },
        showEdit: function () { return this.hasDevice() && this.canEdit(); },
        deviceError: function () { var e = this.current(); return e ? e.error : ""; },
        channelDevices: function () {
          var s = this.sel;
          if (s.kind !== "channel") return [];
          var ch = this.channelList().filter(function (c) { return c.name === s.id; })[0];
          return ch ? ch.devices : [];
        },
        deviceLabel: function (d) { return this.deviceName(d, this.sel.id); },
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
        overrideText: function (c) { return c.override ? tr("config.override_enabled", { value: c.override.enabled }) : "—"; },
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
          }).catch(function (e) { shellFlash(tr("config.export_failed", { error: e.message }), "error"); });
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
      if (!r.files || !r.files.length) return tr("saved.no_change");
      var note = tr("saved.files", { files: r.files.join(", ") });
      var target = String(r.target || "").split("#")[0];
      var mine = (r.warnings || []).filter(function (w) { return w.indexOf(target + ":") === 0; });
      if (r.comments_lost) note += " " + tr("saved.comments_lost", { files: r.comments_lost.join(", ") });
      return mine.length ? note + " " + tr("saved.warning", { warnings: mine.join(" ") }) : note;
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
          if (!window.confirm(tr("channel.confirm_delete", { name: this.name, n: n }))) return;
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
        sourceText: function () { return this.dev && this.dev.source && Object.keys(this.dev.source).length ? tr("edit.source", { source: JSON.stringify(this.dev.source) }) : ""; },
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
          if (!window.confirm(tr("edit.confirm_delete_device", { id: this.id, n: this.tags.length }))) return;
          call("DELETE", "/api/v1/devices/" + encId(this.id), { rev: this.rev }).then(function () {
            changed(); location.hash = "#/channel/" + enc(channel);
          }).catch(function (e) { self.general = splitErrors(e, []).general; });
        },
        // tags
        isEditing: function (t) { return this.editing === t.name; },
        hasTagOverride: function (t) { return Object.prototype.hasOwnProperty.call(this.tagOverrides, t.name); },
        tagOverrideText: function (t) { return tr("edit.tag_override", { override: JSON.stringify(this.tagOverrides[t.name]) }); },
        scanText: function (t) { return t.scan_ms === undefined || t.scan_ms === null ? "" : String(t.scan_ms); },
        enabledText: function (t) { return this.yesNo(t.enabled !== false); },
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
          if (!window.confirm(tr("edit.confirm_delete_tag", { name: t.name }))) return;
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
          if (!window.confirm(tr("edit.confirm_remove_map", { id: this.id }))) return;
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
          else { this.ovNote = tr("edit.nothing_to_save"); return; }
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
        channelNote: function (ch) {    // " (disabled) · total 3 req, 120 ms, 40%" after the channel name
          var parts = [];
          if (!ch.enabled) parts.push(tr("check.channel_disabled"));
          if (ch.devices.length > 1) parts.push(tr("check.channel_total", { req: ch.wire_requests, ms: Math.round(ch.est_cycle_ms), pct: Math.round(ch.load * 100) }));
          return parts.length ? " " + parts.join(" ") : "";
        },
        rates: function (d) { var self = this; return (d.rates_ms || []).map(function (r) { return self.rateText(r); }).join(","); },
      });
    });

    // ------------------------------------------------------------ import wizard (upload -> review -> apply, #33)
    Alpine.data("importView", function () {
      return Object.assign({}, helpers, {
        file: null, fileName: "", report: null, result: null, errors: [], busy: false, step: 1,
        stubsOk: false, reloadAfter: true, discarded: false,
        // a staged import survives leaving the page: its id is kept in this tab and reopened here
        init: function () {
          var self = this, id = readStaged();
          if (!id) return;
          call("GET", "/api/v1/project/import/" + enc(id)).then(function (r) {
            self.report = r; self.fileName = r.import_summary ? r.import_summary.source.file : "";
            self.step = 2; self.stubsOk = !self.stubs().length;
          }).catch(function () { writeStaged(""); });
        },
        pick: function (ev) {
          this.file = ev.target.files[0] || null; this.fileName = this.file ? this.file.name : "";
          this.errors = []; this.discarded = false;
        },
        upload: function () {
          var self = this;
          if (!this.file) { this.errors = [tr("import.choose_file")]; return; }
          this.busy = true; this.errors = []; this.discarded = false;
          call("POST", "/api/v1/project/import?stage=true&filename=" + enc(this.fileName), { raw: this.file }).then(function (r) {
            self.report = r; self.result = null; self.step = 2; self.stubsOk = !self.stubs().length;
            writeStaged(r.import_id);
          }).catch(function (e) { self.errors = splitErrors(e, []).general; }).then(function () { self.busy = false; });
        },
        apply: function () {
          var self = this;
          if (!this.canApply()) return;
          this.busy = true; this.errors = [];
          var url = "/api/v1/project/import/" + enc(this.report.import_id) + "/apply" + (this.reloadAfter ? "?apply=true" : "");
          call("POST", url, { rev: this.report.revision }).then(function (r) {
            self.result = r; self.step = 3; writeStaged(""); changed();
          }).catch(function (e) {
            if (e.status === 412) self.errors = [tr("import.err_changed")];
            else if (e.status === 404) self.errors = [tr("import.err_expired")];
            else self.errors = splitErrors(e, []).general;
          }).then(function () { self.busy = false; });
        },
        discard: function () {
          var self = this, id = this.report && this.report.import_id;
          this.busy = true;
          call("DELETE", "/api/v1/project/import/" + enc(id)).catch(function () { /* expired already */ }).then(function () {
            self.reset(); self.discarded = true; self.busy = false;
          });
        },
        again: function () { this.reset(); },
        reset: function () {
          writeStaged("");
          this.report = null; this.result = null; this.errors = []; this.step = 1; this.stubsOk = false;
          this.file = null; this.fileName = "";
          var input = document.querySelector("#import-form input[type=file]");
          if (input) input.value = "";
        },
        stepClass: function (n) { return this.step > n ? "done" : ""; },
        kindText: function () { return tr(this.report && this.report.kind === "kepserver" ? "import.kind_kepserver" : "import.kind_export"); },
        expiresText: function () { var t = this.report && this.report.expires_at; return t ? new Date(t).toLocaleTimeString() : ""; },
        // importer summary (KEPServerEX)
        summary: function () { return (this.report && this.report.import_summary) || null; },
        countText: function (part) { var s = this.summary()[part]; return tr("import.devices_tags", { devices: s.devices, tags: s.tags }); },
        saText: function () { var s = this.summary().same_address; return s.alias_groups + " / " + s.suspect_groups; },
        driverRows: function () {
          var by = this.summary().included.by_driver || {};
          return Object.keys(by).sort().map(function (k) { return { name: k, devices: by[k].devices, tags: by[k].tags }; });
        },
        excludedRows: function () { return (this.report && this.report.excluded_devices) || []; },
        sameAddress: function () { return (this.report && this.report.same_address) || []; },
        kindOf: function (r) { return r.classification + (r.included ? "" : tr("import.not_imported_suffix")); },
        // changes to the configuration
        dj: function () { return (this.report && this.report.devices_json) || null; },
        sum: function (part, key) { var d = this.dj(); return d ? d.summary[part][key] : 0; },
        changeCounts: function (part) { return "+" + this.sum(part, "added") + " −" + this.sum(part, "removed") + " ~" + this.sum(part, "changed"); },
        files: function () { var r = this.result || this.report; return (r && r.files) || []; },
        filesText: function () { var f = this.files(); return f.length ? f.join(", ") : tr("import.no_change"); },
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
        textState: function (x) { return tr(x.d.unchanged ? "import.file_unchanged" : (x.d.created ? "import.file_new" : "import.file_changed")); },
        problems: function () { return (this.report && this.report.problems) || []; },
        // credentials are never copied: the importer names the environment variables to set (D-010)
        envNotes: function () { return this.problems().filter(function (p) { return /set [A-Z0-9_]+( and [A-Z0-9_]+)? in the environment/.test(p); }); },
        warnings: function () { var r = this.result || this.report; return (r && r.warnings) || []; },
        // apply
        canApply: function () { return !!this.report && this.step === 2 && !this.busy && this.canEdit() && !!this.files().length && this.stubsOk; },
        applyHint: function () {
          if (!this.files().length) return tr("import.hint_nothing");
          if (!this.stubsOk) return tr("import.hint_stubs");
          return tr("import.writes", { files: this.filesText() });
        },
        resultText: function () {
          var r = this.result, vars = { file: this.fileName, files: this.filesText(), error: r && r.reload_error };
          if (!r) return "";
          return tr(r.reload ? "import.imported_reloaded" : (r.reload_error ? "import.imported_reload_error" : "import.imported"), vars);
        },
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
        changeRows: function (e) { var self = this; return (e.changes || []).map(function (c) { return (c.path || tr("audit.whole_object")) + ": " + self.short(c.old) + " → " + self.short(c.new); }); },
        detailText: function (e) { return e.detail ? this.short(JSON.stringify(e.detail), 300) : ""; },
        filesText: function (e) { return (e.files || []).join(", "); },
      });
    });
  });
})();
