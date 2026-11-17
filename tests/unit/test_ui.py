"""Web UI at /ui: login and sessions, CSRF, roles, and smoke tests that every page renders."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from opencap.api.auth import Auth, SESSION_TTL_S
from opencap.api.server import create_app
from opencap.api.ui import HTMX_FILE
from opencap.config.models import ApiCfg
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, SAMPLE, api_cfg, audit_lines, make_config

DEV = "SITE MODBUS/Device1"
HX = {"HX-Request": "true"}


@pytest.fixture
def ui(tmp_path):
    config_dir = make_config(tmp_path)
    poller = ConfigPoller(config_dir)
    poller.identities[DEV] = {"vendor": "Example Vendor", "revision": "1.2"}
    poller.states["SITE M800/8 PUMP"] = "demoted"
    app = create_app(poller, api_cfg(tmp_path), environ=ENV)
    return TestClient(app), poller, config_dir, tmp_path


def login(c: TestClient, token: str = "adm1n-secret") -> str:
    r = c.post("/ui/login", data={"token": token, "next": "/ui"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = c.get("/ui").text
    return re.search(r'<meta name="csrf-token" content="([^"]*)">', page).group(1)


def post(c: TestClient, path: str, csrf: str, data: dict | None = None):
    return c.post(path, data=data or {}, headers={**HX, "X-CSRF-Token": csrf})


# --- login / session -------------------------------------------------------------------

def test_pages_need_login(ui):
    c, _, _, _ = ui
    r = c.get("/ui/config", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login?next=%2Fui%2Fconfig"
    r = c.get("/ui/partials/dashboard", headers={**HX, "HX-Current-URL": "http://testserver/ui/devices/X"})
    assert r.headers["hx-redirect"] == "/ui/login?next=%2Fui%2Fdevices%2FX"
    assert c.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_login_page_and_bad_token(ui):
    c, _, _, _ = ui
    page = c.get("/ui/login")
    assert page.status_code == 200 and 'name="token"' in page.text and 'type="password"' in page.text
    r = c.post("/ui/login", data={"token": "wrong", "next": "/ui"})
    assert r.status_code == 401 and "unknown token" in r.text
    assert "opencap_session" not in r.headers.get("set-cookie", "")


def test_login_sets_a_strict_httponly_cookie(ui):
    c, _, _, _ = ui
    r = c.post("/ui/login", data={"token": "adm1n-secret", "next": "/ui/audit"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/audit"
    cookie = r.headers["set-cookie"].lower()
    assert "opencap_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/ui" in cookie
    assert "adm1n-secret" not in cookie                              # a session id, not the token


def test_login_next_cannot_leave_the_ui(ui):
    c, _, _, _ = ui
    for evil in ("//evil.example/ui", "https://evil.example", "/elsewhere"):
        r = c.post("/ui/login", data={"token": "adm1n-secret", "next": evil}, follow_redirects=False)
        assert r.headers["location"] == "/ui"


def test_cross_site_login_refused(ui):
    c, _, _, _ = ui
    r = c.post("/ui/login", data={"token": "adm1n-secret"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_session_cookie_does_not_authorize_the_rest_api(ui):
    c, _, _, _ = ui
    login(c)
    c.cookies.set("opencap_session", c.cookies.get("opencap_session"), path="/")
    assert c.get("/config").status_code == 401


def test_logout_ends_the_session(ui):
    c, _, _, _ = ui
    csrf = login(c)
    r = post(c, "/ui/logout", csrf)
    assert r.headers["hx-redirect"] == "/ui/login"
    c.cookies.clear()
    assert c.get("/ui", follow_redirects=False).status_code == 303


def test_sessions_expire():
    now = [1000.0]
    auth = Auth(ApiCfg(), environ={"OPENCAP_API_TOKEN": "t"}, clock=lambda: now[0])
    sess = auth.login("t")
    assert auth.session(sess.sid).name == "default" and auth.login("bad") is None
    now[0] += SESSION_TTL_S + 1
    assert auth.session(sess.sid) is None


# --- security headers, static ----------------------------------------------------------

def test_csp_and_security_headers(ui):
    c, _, _, _ = ui
    login(c)
    r = c.get("/ui")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe" not in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text and " style=" not in r.text and " onclick" not in r.text   # CSP-clean markup


def test_vendored_htmx_and_licence_are_served(ui):
    c, _, _, _ = ui
    js = c.get(f"/ui/static/{HTMX_FILE}")
    assert js.status_code == 200 and 'version:"2.0.11"' in js.text
    lic = c.get("/ui/static/htmx-LICENSE.txt")
    assert lic.status_code == 200 and "Zero-Clause BSD" in lic.text
    assert c.get("/ui/static/app.css").status_code == 200 and c.get("/ui/static/app.js").status_code == 200
    page = c.get("/ui/login").text
    assert f"/ui/static/{HTMX_FILE}" in page and '"allowEval":false' in page


# --- pages render ----------------------------------------------------------------------

def test_dashboard_renders_sites_devices_states_and_link(ui):
    c, poller, _, _ = ui
    login(c)
    page = c.get("/ui").text
    for text in ("MQTT link", "connected", "Outbox", ">7<", "SITE MODBUS", "SITE M800", "state-demoted",
                 'href="/ui/devices/SITE%20MODBUS/Device1"', "Last good", "42 ms", 'data-filter-input'):
        assert text in page, text
    poller.mqtt_connected = False
    assert ">down<" in c.get("/ui/partials/dashboard").text


def test_device_page_renders_values_plan_identity_and_read_once(ui):
    c, poller, _, _ = ui
    csrf = login(c)
    page = c.get(f"/ui/devices/{DEV}").text
    for text in ("Live values", "FLOW_1", "q-good", "Read once", "Block plan", "bad_config tags", "Identity",
                 'hx-get="/ui/partials/values/SITE%20MODBUS/Device1"', 'hx-trigger="every 2s"', "Edit config"):
        assert text in page, text
    ident = c.get(f"/ui/partials/identity/{DEV}").text
    assert "Example Vendor" in ident
    r = post(c, f"/ui/actions/read-once/{DEV}", csrf)
    assert r.status_code == 200 and "read once at" in r.text and poller.read_onces == [DEV]
    assert "is no longer polled" in c.get("/ui/partials/values/NOPE/X").text
    assert c.get("/ui/devices/NOPE/X").status_code == 404


def test_config_pages_render(ui):
    c, _, _, _ = ui
    login(c)
    page = c.get("/ui/config").text
    for text in ("Configuration", "SITE MODBUS", "Reload", "Export", "Add channel", "Publish defaults", "Check plan"):
        assert text in page, text
    page = c.get("/ui/config/channel/SITE MODBUS").text
    assert "Device1" in page and "Add device" in page and "Delete channel" in page
    page = c.get(f"/ui/config/device/{DEV}").text
    for text in ('id="device-form"', 'name="timeout_ms"', 'id="tags-body"', "FLOW_1", 'id="tag-create"',
                 'id="map-form"', 'id="override-form"', "Delete device", "modbus_tcp"):
        assert text in page, text
    assert c.get("/ui/config/device/NOPE/X").status_code == 404
    page = c.get("/ui/check").text
    assert "Requests / full pass" in page and "SITE MODBUS" in page
    assert c.get("/ui/import").text.count('type="file"') == 1
    assert "Audit log" in c.get("/ui/audit").text
    export = c.get("/ui/config/export")
    assert export.json()["format"] == "opencap-config" and "attachment" in export.headers["content-disposition"]


def test_ui_never_offers_a_plc_write(ui):
    c, _, _, _ = ui
    login(c)
    for path in ("/ui", f"/ui/devices/{DEV}", f"/ui/config/device/{DEV}"):
        assert not re.search(r"write (a |the )?(value|tag)|set value", c.get(path).text, re.I), path


# --- csrf and roles --------------------------------------------------------------------

def test_unsafe_requests_need_the_csrf_token(ui):
    c, _, cfg, _ = ui
    csrf = login(c)
    before = (cfg / "devices.json").read_bytes()
    r = c.post(f"/ui/config/device-update/{DEV}", data={"retries": "0"}, headers=HX)
    assert r.status_code == 200 and "CSRF" in r.text and r.headers["hx-retarget"] == "#flash"
    r = c.post(f"/ui/config/device-update/{DEV}", data={"retries": "0"}, headers={**HX, "X-CSRF-Token": "x" + csrf})
    assert "CSRF" in r.text
    assert c.post("/ui/reload").status_code == 403                    # non-htmx: plain 403 page
    assert (cfg / "devices.json").read_bytes() == before


def test_viewer_is_read_only(ui):
    c, poller, cfg, _ = ui
    csrf = login(c, "v1ewer-secret")
    page = c.get(f"/ui/devices/{DEV}").text
    assert "Read once" not in page and "grafana · viewer" in page
    page = c.get(f"/ui/config/device/{DEV}").text
    assert '<fieldset disabled>' in page and 'id="tag-create"' not in page and "Delete device" not in page
    before = (cfg / "devices.json").read_bytes()
    for path in (f"/ui/config/device-update/{DEV}", f"/ui/actions/read-once/{DEV}", "/ui/reload",
                 "/ui/config/channel-create", f"/ui/config/tag-delete/{DEV}?tag=FLOW_1"):
        r = post(c, path, csrf, {"name": "X"})
        assert "role viewer" in r.text, path
    assert c.get(f"/ui/config/tag-row/{DEV}?tag=FLOW_1&edit=true", headers=HX).text.count("read-only") == 1
    assert (cfg / "devices.json").read_bytes() == before and poller.read_onces == []


# --- editing through the UI ------------------------------------------------------------

def test_device_form_shows_validation_errors_inline(ui):
    c, _, cfg, tmp = ui
    csrf = login(c)
    r = post(c, f"/ui/config/device-update/{DEV}", csrf, {"timeout_ms": "soon", "params": "unit_id: 999"})
    assert r.status_code == 200
    assert 'class="field-error"' in r.text and "valid integer" in r.text
    assert 'value="soon"' in r.text                                   # the operator's input is kept
    r = post(c, f"/ui/config/device-update/{DEV}", csrf, {"params": "unit_id: 999"})
    assert "0..255" in r.text
    r = post(c, f"/ui/config/device-update/{DEV}", csrf, {"params": "unit_id: [1"})
    assert "not valid YAML" in r.text
    r = post(c, f"/ui/config/device-update/{DEV}", csrf, {"timeout_ms": "1500", "enabled": "true"})
    assert "Saved to devices.json" in r.text and r.headers["hx-trigger"] == "config-changed"
    entry = audit_lines(tmp)[-1]
    assert entry["who"] == "alice" and entry["via"] == "ui" and entry["changes"][0]["new"] == 1500
    assert "Changes not applied yet" in c.get("/ui/partials/pending").text


def test_device_rename_redirects(ui):
    c, _, _, _ = ui
    csrf = login(c)
    r = post(c, f"/ui/config/device-update/{DEV}", csrf, {"name": "Device9"})
    assert r.headers["hx-redirect"] == "/ui/config/device/SITE%20MODBUS/Device9"


def test_tag_edit_create_delete(ui):
    c, _, cfg, _ = ui
    csrf = login(c)
    row = c.get(f"/ui/config/tag-row/{DEV}?tag=FLOW_1&edit=true", headers=HX).text
    assert 'name="address"' in row and 'hx-include="closest tr"' in row
    r = post(c, f"/ui/config/tag-update/{DEV}?tag=FLOW_1", csrf,
             {"name": "FLOW_1", "address": "999999", "dtype": "float32", "enabled": "true"})
    assert 'class="editing"' in r.text and "unknown area prefix" in r.text
    r = post(c, f"/ui/config/tag-update/{DEV}?tag=FLOW_1", csrf,
             {"name": "FLOW_1", "address": "400201", "dtype": "float32", "enabled": "true", "scale": "{raw_hi: 10}"})
    assert ">400201<" in r.text and "saved" in r.text
    r = post(c, f"/ui/config/tag-create/{DEV}", csrf, {"name": "", "address": "400301", "dtype": "float32"})
    assert 'id="tag-create"' in r.text and "field-error" in r.text and "hx-swap-oob" not in r.text
    r = post(c, f"/ui/config/tag-create/{DEV}", csrf, {"name": "NEW_TAG", "address": "400301", "dtype": "float32"})
    assert 'hx-swap-oob="beforeend:#tags-body"' in r.text and "NEW_TAG" in r.text and 'value=""' in r.text
    r = post(c, f"/ui/config/tag-delete/{DEV}?tag=NEW_TAG", csrf)
    assert r.status_code == 200 and r.text == ""
    names = [t["name"] for t in json.loads((cfg / "devices.json").read_text())["channels"][0]["devices"][0]["tags"]]
    assert "NEW_TAG" not in names and "FLOW_1" in names


def test_channel_device_map_override_through_the_ui(ui):
    c, _, cfg, _ = ui
    csrf = login(c)
    r = post(c, "/ui/config/channel-create", csrf, {"name": "a/b"})
    assert "field-error" in r.text
    r = post(c, "/ui/config/channel-create", csrf, {"name": "NEW SITE"})
    assert r.headers["hx-redirect"] == "/ui/config/channel/NEW%20SITE"
    r = post(c, "/ui/config/device-create/NEW SITE", csrf, {"name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.4",
                                                           "port": "nope"})
    assert "field-error" in r.text
    r = post(c, "/ui/config/device-create/NEW SITE", csrf, {"name": "PLC", "driver": "modbus_tcp", "host": "192.0.2.4",
                                                           "port": "502", "params": "{unit_id: 3}"})
    assert r.headers["hx-redirect"] == "/ui/config/device/NEW%20SITE/PLC"
    r = post(c, "/ui/config/map-update/NEW SITE/PLC", csrf, {"sub_device_id": "SUB_NEW", "stub": "false"})
    assert "Saved to devices_map.yaml" in r.text
    r = post(c, "/ui/config/override-update/NEW SITE/PLC", csrf, {"override": "timeout_ms: soon"})
    assert "Not saved" in r.text
    r = post(c, "/ui/config/override-update/NEW SITE/PLC", csrf, {"override": "timeout_ms: 2500"})
    assert "Saved to overrides.yaml" in r.text
    r = post(c, "/ui/config/channel-update/NEW SITE", csrf, {"name": "NEW SITE", "enabled": "false",
                                                             "override_enabled": "true"})
    assert "Saved" in r.text
    r = post(c, "/ui/config/map-defaults", csrf, {"defaults": "env: staging"})
    assert "Saved to devices_map.yaml" in r.text
    r = post(c, "/ui/config/map-update/NEW SITE/PLC", csrf, {"action": "delete"})
    assert "No entry" in r.text
    r = post(c, "/ui/config/device-delete/NEW SITE/PLC", csrf)
    assert r.headers["hx-redirect"] == "/ui/config/channel/NEW%20SITE"
    r = post(c, "/ui/config/channel-delete/NEW SITE", csrf)
    assert r.headers["hx-redirect"] == "/ui/config"


def test_reload_button(ui):
    c, poller, _, tmp = ui
    csrf = login(c)
    post(c, f"/ui/config/device-update/{DEV}", csrf, {"retries": "0"})
    r = post(c, "/ui/reload", csrf)
    assert "Reloaded: restarted SITE MODBUS" in r.text and poller.reloads == 1
    assert audit_lines(tmp)[-1]["action"] == "reload" and audit_lines(tmp)[-1]["via"] == "ui"


def test_import_preview_and_apply(ui):
    c, _, cfg, _ = ui
    csrf = login(c)
    post(c, "/ui/config/channel-delete/SITE PCCC", csrf)
    hdr = {**HX, "X-CSRF-Token": csrf, "Content-Type": "application/json"}
    r = c.post("/ui/import/preview?filename=project.json", content=SAMPLE.read_bytes(), headers=hdr)
    assert "Dry run: project.json" in r.text and "SITE PCCC/7 PUMP" in r.text and "data-import-apply" in r.text
    rev = re.search(r'data-revision="([0-9a-f]+)"', r.text).group(1)
    r = c.post("/ui/import/apply?filename=project.json", content=SAMPLE.read_bytes(),
               headers={**hdr, "X-Config-Revision": "0000000000000000"})
    assert "Cannot import" in r.text and "changed since" in r.text
    r = c.post("/ui/import/apply?filename=project.json", content=SAMPLE.read_bytes(),
               headers={**hdr, "X-Config-Revision": rev})
    assert "Imported project.json" in r.text
    assert "SITE PCCC/7 PUMP" in (cfg / "devices.json").read_text()
    r = c.post("/ui/import/preview", content=b"{}", headers=hdr)
    assert "Cannot import" in r.text


def test_audit_page_lists_changes(ui):
    c, _, _, _ = ui
    csrf = login(c)
    post(c, f"/ui/config/device-update/{DEV}", csrf, {"retries": "0"})
    page = c.get("/ui/audit").text
    assert "device.update" in page and "alice" in page and "retries" in page
    assert "device.update" not in c.get("/ui/audit?action=map").text


# --- open (no token) mode --------------------------------------------------------------

def test_open_mode_needs_no_login_but_still_csrf(tmp_path):
    poller = ConfigPoller(make_config(tmp_path))
    c = TestClient(create_app(poller, ApiCfg(audit_log=str(tmp_path / "a.jsonl")), environ={}))
    page = c.get("/ui")
    assert page.status_code == 200 and "No API token is configured" in page.text and "anonymous" in page.text
    assert c.get("/ui/login", follow_redirects=False).status_code == 303
    csrf = re.search(r'<meta name="csrf-token" content="([^"]*)">', page.text).group(1)
    assert "CSRF" in c.post("/ui/reload", headers=HX).text
    assert "Reloaded" in c.post("/ui/reload", headers={**HX, "X-CSRF-Token": csrf}).text
