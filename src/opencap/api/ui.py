"""Operator web UI at `/ui`: server-rendered HTML (Jinja2) + vendored htmx, no build step.

Pages: dashboard, device page (live values, block plan, bad_config tags, identity, read
once), config editor (channels, devices, tags, publish map, overrides, reload), check
(block plan per site), KEPServer / opencap import with a dry-run diff, audit log.

Every change goes through the same `ConfigStore` as the REST API and is audited the same
way. Login: a token from `poller.yaml` `api` (see `auth.py`); the session cookie is
HttpOnly + SameSite=Strict, unsafe requests need the session's CSRF token, and pages are
served with a strict Content-Security-Policy (no inline script or style; htmx runs with
`allowEval: false`). The UI never offers to write a PLC value: opencap is read-only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote

import yaml
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from opencap import FIRM_VERSION
from opencap.api.auth import SESSION_COOKIE, SESSION_TTL_S, AuthError, Session, cross_site, is_safe
from opencap.api.config_api import import_response, read_json_body
from opencap.api.context import ApiContext
from opencap.config.store import ConfigError, ConfigStore, Invalid, Result

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
HTMX_FILE = "htmx-2.0.11.min.js"
MAX_FORM_BYTES = 2 * 1024 * 1024

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

DEVICE_INT_FIELDS = ("port", "scan_ms", "min_scan_ms", "timeout_ms", "retries", "demote_after", "demote_for_ms")
DEVICE_FIELDS = ("name", "channel", "driver", "host", "enabled", "params", *DEVICE_INT_FIELDS)
TAG_FIELDS = ("name", "address", "dtype", "scan_ms", "enabled", "description", "scale")
MAP_FIELDS = ("sub_device_id", "topic", "env", "stub")


class LoginRequired(Exception):
    pass


class UiForbidden(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- small helpers ------------------------------------------------------------------

def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    s = (datetime.now(UTC) - ts).total_seconds()
    if s < 0:
        s = 0
    if s < 90:
        return f"{s:.0f} s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 172800:
        return f"{s / 3600:.0f} h ago"
    return f"{s / 86400:.0f} d ago"


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, (list, dict)):
        text = json.dumps(v, default=str)
        return text if len(text) <= 200 else text[:197] + "…"
    return str(v)


def _short(v: Any, limit: int = 120) -> str:
    text = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _domid(text: str) -> str:
    return "t" + hashlib.sha1(text.encode()).hexdigest()[:12]


def _yaml_text(data: Any) -> str:
    if not data:
        return ""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)


def _flow(data: Any) -> str:
    if not data:
        return ""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=True, width=1000).strip()


def _parse_yaml_field(text: str | None, field: str, *, want: type = dict) -> Any:
    if text is None or not text.strip():
        return None
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Invalid(f"{field}: not valid YAML/JSON", [{"loc": [field], "msg": f"not valid YAML/JSON: {exc}",
                                                         "type": "yaml_invalid"}]) from None
    if value is not None and not isinstance(value, want):
        raise Invalid(f"{field}: expected a mapping", [{"loc": [field], "msg": "expected a mapping like {key: value}",
                                                        "type": "dict_type"}])
    return value


def split_errors(exc: Invalid, known: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    """Field errors (shown next to the input) and the rest (shown above the form)."""
    fields: dict[str, str] = {}
    general: list[str] = []
    for e in exc.errors:
        loc = list(e.get("loc") or [])
        msg = e.get("msg", "")
        if loc and loc[0] in known:
            rest = ".".join(str(p) for p in loc[1:])
            fields.setdefault(str(loc[0]), f"{rest}: {msg}" if rest else msg)
        else:
            general.append(f"{'.'.join(str(p) for p in loc)}: {msg}" if loc else msg)
    if not exc.errors:
        general.append(exc.message)
    return fields, general


def _safe_next(url: str | None) -> str:
    if url and url.startswith("/ui") and not url.startswith("//") and "\\" not in url:
        return url
    return "/ui"


def _form_device_values(dev: dict[str, Any]) -> dict[str, Any]:
    vals = {k: ("" if dev.get(k) is None else dev.get(k)) for k in DEVICE_FIELDS}
    vals["enabled"] = dev.get("enabled", True) in (True, "true")
    vals["params"] = dev["params"] if isinstance(dev.get("params"), str) else _yaml_text(dev.get("params"))
    return vals


def _form_tag_values(tag: dict[str, Any]) -> dict[str, Any]:
    vals = {k: ("" if tag.get(k) is None else tag.get(k)) for k in TAG_FIELDS}
    vals["enabled"] = tag.get("enabled", True) in (True, "true")
    vals["scale"] = tag["scale"] if isinstance(tag.get("scale"), str) else _flow(tag.get("scale"))
    return vals


# --- install ------------------------------------------------------------------------

def install_ui(app: FastAPI, ctx: ApiContext) -> None:
    auth = ctx.auth
    store: ConfigStore | None = ctx.store
    poller = ctx.poller
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True),
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters.update(ago=_ago, fmt=_fmt_value, short=_short, domid=_domid, yaml=_yaml_text, flow=_flow,
                       q=lambda s: quote(str(s), safe=""), p=lambda s: quote(str(s), safe="/"))
    env.globals.update(version=FIRM_VERSION, htmx=HTMX_FILE,
                       static=lambda name: f"/ui/static/{name}?v={FIRM_VERSION}")

    # --- rendering -----------------------------------------------------------------
    def _base_ctx(request: Request, *, with_status: bool = True) -> dict[str, Any]:
        sess: Session | None = getattr(request.state, "session", None)
        principal = sess.principal() if sess else None
        status = ctx.tracker.status() if principal and with_status else {"available": False, "pending_reload": False}
        return {"request": request, "principal": principal, "csrf": sess.csrf if sess else "",
                "auth_open": auth.open, "path": request.url.path, "status": status}

    def html(request: Request, template: str, status_code: int = 200, headers: dict[str, str] | None = None,
             **kw: Any) -> HTMLResponse:
        # partials ("_x.html") do not show the pending-reload banner, except the banner itself
        with_status = not template.startswith("_") or template == "_pending.html"
        body = env.get_template(template).render(**_base_ctx(request, with_status=with_status), **kw)
        return HTMLResponse(body, status_code=status_code, headers={**SECURITY_HEADERS, **(headers or {})})

    def flash(request: Request, message: str, level: str = "error", details: list[str] | None = None,
              headers: dict[str, str] | None = None) -> HTMLResponse:
        hx = {"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"} if request.headers.get("hx-request") else {}
        return html(request, "_flash.html", message=message, level=level, details=details or [],
                    headers={**hx, **(headers or {})})

    def changed(headers: dict[str, str] | None = None) -> dict[str, str]:
        return {"HX-Trigger": "config-changed", **(headers or {})}

    def need_store() -> ConfigStore:
        if store is None:
            raise UiForbidden("the config editor needs a config directory")
        return store

    async def form_of(request: Request) -> dict[str, str]:
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            raise UiForbidden("form too large")
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))

    async def act(request: Request, fn) -> Result:
        result: Result = await run_in_threadpool(fn)
        await run_in_threadpool(ctx.record, request.state.principal, result)
        return result

    # --- auth dependencies -----------------------------------------------------------
    def ui_session(request: Request) -> Session:
        sess = auth.ui_session(request.cookies.get(SESSION_COOKIE))
        if sess is None:
            raise LoginRequired()
        request.state.session = sess
        request.state.principal = sess.principal()
        if not is_safe(request.method) and not auth.check_csrf(sess, request.headers.get("x-csrf-token")):
            raise UiForbidden("missing or stale CSRF token: reload the page")
        return sess

    def ui_admin(sess: Session = Depends(ui_session)) -> Session:
        if sess.role != "admin":
            raise UiForbidden(f"{sess.name!r} has role viewer: read-only")
        return sess

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            current = request.headers.get("hx-current-url", "")
            nxt = "/ui" + current.split("/ui", 1)[1] if "/ui" in current else "/ui"
            return Response(status_code=200, headers={"HX-Redirect": f"/ui/login?next={quote(_safe_next(nxt), safe='')}"})
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/ui/login?next={quote(_safe_next(nxt), safe='')}", status_code=303)

    @app.exception_handler(UiForbidden)
    async def _forbidden(request: Request, exc: UiForbidden) -> Response:
        if request.headers.get("hx-request"):
            return flash(request, exc.message)
        return html(request, "error.html", status_code=403, title="Not allowed", message=exc.message)

    r = APIRouter(prefix="/ui")
    view = [Depends(ui_session)]
    admin = [Depends(ui_admin)]

    # --- login ---------------------------------------------------------------------
    @r.get("/login")
    def login_page(request: Request, next: str = "/ui") -> Response:
        if auth.open:
            return RedirectResponse(_safe_next(next), status_code=303)
        return html(request, "login.html", next=_safe_next(next), error=None)

    @r.post("/login")
    async def login(request: Request) -> Response:
        if cross_site(request.headers, request.headers.get("host")):
            return html(request, "login.html", status_code=403, next="/ui", error="cross-site login refused")
        form = await form_of(request)
        nxt = _safe_next(form.get("next"))
        sess = auth.login(form.get("token", "").strip())
        if sess is None:
            return html(request, "login.html", status_code=401, next=nxt, error="unknown token")
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, sess.sid, max_age=SESSION_TTL_S, path="/ui", httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
        return resp

    @r.post("/logout", dependencies=view)
    def logout(request: Request) -> Response:
        auth.logout(request.cookies.get(SESSION_COOKIE))
        target = "/ui" if auth.open else "/ui/login"
        resp = Response(status_code=200, headers={"HX-Redirect": target}) if request.headers.get("hx-request") \
            else RedirectResponse(target, status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/ui")
        return resp

    # --- dashboard -----------------------------------------------------------------
    def dashboard_ctx() -> dict[str, Any]:
        return {"health": poller.health(), "channels": poller.channels_summary()}

    @r.get("", dependencies=view)
    def dashboard(request: Request) -> Response:
        return html(request, "dashboard.html", **dashboard_ctx())

    @r.get("/partials/dashboard", dependencies=view)
    def dashboard_partial(request: Request) -> Response:
        return html(request, "_dashboard.html", **dashboard_ctx())

    @r.get("/partials/pending", dependencies=view)
    def pending_partial(request: Request) -> Response:
        return html(request, "_pending.html")

    # --- device page ---------------------------------------------------------------
    def values_ctx(device_id: str) -> dict[str, Any]:
        detail = poller.device_detail(device_id)
        values = poller.device_values(device_id)
        return {"device_id": device_id, "detail": detail, "values": values,
                "counts": {q: sum(1 for v in values.values() if v["quality"] == q)
                           for q in ("good", "bad_comm", "bad_config", "bad_decode", "stale")}}

    @r.get("/devices/{device_id:path}", dependencies=view)
    def device_page(request: Request, device_id: str) -> Response:
        try:
            vctx = values_ctx(device_id)
            plan = poller.device_plan(device_id) if hasattr(poller, "device_plan") else None
        except KeyError:
            in_config = False
            if store is not None:
                try:
                    store.get_device(device_id)
                    in_config = True
                except ConfigError:
                    pass
            return html(request, "device_missing.html", status_code=404, device_id=device_id, in_config=in_config)
        return html(request, "device.html", plan=plan, read_note=None, **vctx)

    @r.get("/partials/values/{device_id:path}", dependencies=view)
    def values_partial(request: Request, device_id: str) -> Response:
        try:
            return html(request, "_values.html", read_note=None, **values_ctx(device_id))
        except KeyError:   # 286 = htmx stops polling
            return html(request, "_flash.html", status_code=286, message=f"{device_id} is no longer polled "
                        "(config reloaded?): reload the page", level="warn", details=[])

    @r.get("/partials/identity/{device_id:path}", dependencies=view)
    async def identity_partial(request: Request, device_id: str) -> Response:
        try:
            ident = await poller.device_identity(device_id)
        except KeyError:
            ident = {"connected": False, "identity": {}, "error": "device is not polled"}
        except Exception as exc:  # noqa: BLE001
            ident = {"connected": True, "identity": {}, "error": f"{type(exc).__name__}: {exc}"}
        return html(request, "_identity.html", device_id=device_id, ident=ident)

    @r.post("/actions/read-once/{device_id:path}", dependencies=admin)
    async def read_once(request: Request, device_id: str) -> Response:
        try:
            await poller.read_once(device_id)
        except KeyError:
            return flash(request, f"unknown device {device_id!r}")
        except Exception as exc:  # noqa: BLE001 - driver unsupported, worker stopped, ...
            return flash(request, f"read once failed: {exc}")
        note = f"read once at {datetime.now(UTC).strftime('%H:%M:%S')} UTC"
        return html(request, "_values.html", read_note=note, **values_ctx(device_id))

    # --- config editor: overview ---------------------------------------------------
    @r.get("/config", dependencies=view)
    def config_page(request: Request) -> Response:
        st = need_store()
        summary = st.summary()
        try:
            channels = st.list_channels()
        except ConfigError as exc:
            channels, summary["error"] = [], summary["error"] or exc.message
        dmap = st.get_map()
        return html(request, "config.html", summary=summary, channels=channels,
                    defaults_text=_yaml_text(dmap["defaults"]), errors={}, general=[], note=None,
                    defaults_errors=[], defaults_note=None, new_channel="")

    @r.get("/config/export", dependencies=view)
    def config_export(request: Request) -> Response:
        data = need_store().export()
        return Response(json.dumps(data, indent=1, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="opencap-config-{data["revision"]}.json"',
                                 "Cache-Control": "no-store"})

    @r.post("/reload", dependencies=admin)
    async def reload(request: Request) -> Response:
        try:
            changed_ = await ctx.reload(request.state.principal)
        except Exception as exc:  # noqa: BLE001
            return flash(request, f"reload failed, old config kept: {exc}", headers=changed())
        parts = [f"{k} {', '.join(v)}" for k, v in changed_.items() if v and k != "unchanged"]
        msg = "Reloaded: " + ("; ".join(parts) if parts else "nothing changed")
        return flash(request, msg, level="ok", headers=changed())

    @r.post("/config/channel-create", dependencies=admin)
    async def channel_create(request: Request) -> Response:
        st = need_store()
        form = await form_of(request)
        name = form.get("name", "")
        body = {"name": name, "enabled": form.get("enabled", "true") == "true"}
        try:
            await act(request, lambda: st.create_channel(body))
        except Invalid as exc:
            fields, general = split_errors(exc, ("name", "enabled"))
            return html(request, "_channel_create.html", errors=fields, general=general, new_channel=name)
        except ConfigError as exc:
            return flash(request, exc.message)
        return Response(status_code=200, headers=changed({"HX-Redirect": f"/ui/config/channel/{quote(name, safe='')}"}))

    @r.post("/config/map-defaults", dependencies=admin)
    async def map_defaults(request: Request) -> Response:
        st = need_store()
        form = await form_of(request)
        text = form.get("defaults", "")
        try:
            body = _parse_yaml_field(text, "defaults") or {}
            result = await act(request, lambda: st.put_map_defaults(body))
        except Invalid as exc:
            fields, general = split_errors(exc, ())
            return html(request, "_map_defaults.html", defaults_text=text,
                        defaults_errors=general + list(fields.values()), defaults_note=None)
        except ConfigError as exc:
            return flash(request, exc.message)
        return html(request, "_map_defaults.html", defaults_text=_yaml_text(body), defaults_errors=[],
                    defaults_note=_saved_note(result), headers=changed())

    # --- config editor: channel ----------------------------------------------------
    def channel_ctx(st: ConfigStore, name: str) -> dict[str, Any]:
        from opencap.drivers import DRIVERS

        return {"channel": st.get_channel(name), "drivers": list(DRIVERS)}

    @r.get("/config/channel/{name:path}", dependencies=view)
    def channel_page(request: Request, name: str) -> Response:
        st = need_store()
        try:
            cctx = channel_ctx(st, name)
        except ConfigError as exc:
            return html(request, "error.html", status_code=exc.status, title="Not found", message=exc.message)
        return html(request, "config_channel.html", errors={}, general=[], note=None,
                    new_device={"driver": "modbus_tcp", "port": 502}, dev_errors={}, dev_general=[], **cctx)

    @r.post("/config/channel-update/{name:path}", dependencies=admin)
    async def channel_update(request: Request, name: str) -> Response:
        st = need_store()
        form = await form_of(request)
        new_name = form.get("name", name)
        patch = {"name": new_name, "enabled": form.get("enabled", "true") == "true"}
        ov = form.get("override_enabled", "")
        try:
            result = await act(request, lambda: st.update_channel(name, patch))
            cur = (st.get_channel(new_name).get("override") or {}).get("enabled")
            if ov in ("true", "false") and cur != (ov == "true"):
                await act(request, lambda: st.put_channel_override(new_name, {"enabled": ov == "true"}))
            elif ov == "" and cur is not None:
                await act(request, lambda: st.delete_channel_override(new_name))
        except Invalid as exc:
            fields, general = split_errors(exc, ("name", "enabled"))
            ch = {**st.get_channel(name), "name": new_name, "enabled": patch["enabled"]}
            return html(request, "_channel_form.html", channel=ch, errors=fields, general=general, note=None)
        except ConfigError as exc:
            return flash(request, exc.message)
        if new_name != name:
            return Response(status_code=200,
                            headers=changed({"HX-Redirect": f"/ui/config/channel/{quote(new_name, safe='')}"}))
        return html(request, "_channel_form.html", channel=st.get_channel(name), errors={}, general=[],
                    note=_saved_note(result), headers=changed())

    @r.post("/config/channel-delete/{name:path}", dependencies=admin)
    async def channel_delete(request: Request, name: str) -> Response:
        st = need_store()
        try:
            await act(request, lambda: st.delete_channel(name))
        except ConfigError as exc:
            return flash(request, exc.message)
        return Response(status_code=200, headers=changed({"HX-Redirect": "/ui/config"}))

    @r.post("/config/device-create/{channel:path}", dependencies=admin)
    async def device_create(request: Request, channel: str) -> Response:
        st = need_store()
        form = await form_of(request)
        try:
            body: dict[str, Any] = {k: form[k].strip() for k in ("name", "driver", "host", "port") if k in form}
            body["enabled"] = form.get("enabled", "true") == "true"
            params = _parse_yaml_field(form.get("params"), "params")
            if params:
                body["params"] = params
            result = await act(request, lambda: st.create_device(channel, body))
        except Invalid as exc:
            fields, general = split_errors(exc, DEVICE_FIELDS)
            from opencap.drivers import DRIVERS

            return html(request, "_device_create.html", channel=st.get_channel(channel), drivers=list(DRIVERS),
                        new_device=form, dev_errors=fields, dev_general=general)
        except ConfigError as exc:
            return flash(request, exc.message)
        return Response(status_code=200,
                        headers=changed({"HX-Redirect": f"/ui/config/device/{quote(result.target, safe='/')}"}))

    # --- config editor: device -----------------------------------------------------
    def device_ctx(st: ConfigStore, device_id: str) -> dict[str, Any]:
        from opencap.drivers import DRIVERS
        from opencap.config.models import DType
        from typing import get_args

        data = st.get_device(device_id)
        dev = data["device"]
        ov = data["override"] or {}
        return {
            "device_id": device_id, "dev": dev, "values": _form_device_values(dev),
            "map_entry": data["map_entry"] or {}, "has_map": data["map_entry"] is not None,
            "override_text": _yaml_text(data["override"]), "tag_overrides": (ov.get("tags") or {}),
            "drivers": list(DRIVERS), "dtypes": list(get_args(DType)),
            "channels": [c["name"] for c in st.list_channels()],
        }

    @r.get("/config/device/{device_id:path}", dependencies=view)
    def device_editor(request: Request, device_id: str) -> Response:
        st = need_store()
        try:
            dctx = device_ctx(st, device_id)
        except ConfigError as exc:
            return html(request, "error.html", status_code=exc.status, title="Not found", message=exc.message)
        return html(request, "config_device.html", errors={}, general=[], note=None, map_errors={},
                    map_general=[], map_note=None, ov_errors=[], ov_note=None,
                    new_tag={"dtype": "float32", "enabled": True}, tag_errors={}, tag_general=[], **dctx)

    @r.post("/config/device-update/{device_id:path}", dependencies=admin)
    async def device_update(request: Request, device_id: str) -> Response:
        st = need_store()
        form = await form_of(request)

        def work() -> Result:
            current = dict(st.get_device(device_id)["device"])
            current.pop("id", None)
            for f in ("name", "channel", "driver", "host"):
                if f in form:
                    current[f] = form[f].strip()
            for f in DEVICE_INT_FIELDS:
                if f in form:
                    if form[f].strip() == "":
                        current.pop(f, None)
                    else:
                        current[f] = form[f].strip()
            if "enabled" in form:
                current["enabled"] = form["enabled"] == "true"
            if "params" in form:
                current["params"] = _parse_yaml_field(form["params"], "params") or {}
            return st.update_device(device_id, current, partial=False)

        try:
            result = await act(request, work)
        except Invalid as exc:
            fields, general = split_errors(exc, DEVICE_FIELDS)
            dctx = device_ctx(st, device_id)
            values = {**dctx["values"], **{k: v for k, v in form.items() if k in DEVICE_FIELDS}}
            values["enabled"] = form.get("enabled", "true") == "true"
            return html(request, "_device_form.html", **{**dctx, "values": values}, errors=fields,
                        general=general, note=None)
        except ConfigError as exc:
            return flash(request, exc.message)
        if result.data["id"] != device_id:
            return Response(status_code=200,
                            headers=changed({"HX-Redirect": f"/ui/config/device/{quote(result.data['id'], safe='/')}"}))
        return html(request, "_device_form.html", **device_ctx(st, device_id), errors={}, general=[],
                    note=_saved_note(result), headers=changed())

    @r.post("/config/device-delete/{device_id:path}", dependencies=admin)
    async def device_delete(request: Request, device_id: str) -> Response:
        st = need_store()
        channel = device_id.split("/", 1)[0]
        try:
            await act(request, lambda: st.delete_device(device_id))
        except ConfigError as exc:
            return flash(request, exc.message)
        return Response(status_code=200,
                        headers=changed({"HX-Redirect": f"/ui/config/channel/{quote(channel, safe='')}"}))

    # tags -------------------------------------------------------------------------
    def tag_from_form(form: dict[str, str]) -> dict[str, Any]:
        tag: dict[str, Any] = {"name": form.get("name", "").strip(), "address": form.get("address", "").strip(),
                               "dtype": form.get("dtype", "")}
        if form.get("scan_ms", "").strip():
            tag["scan_ms"] = form["scan_ms"].strip()
        tag["enabled"] = form.get("enabled", "true") == "true"
        if form.get("description", "").strip():
            tag["description"] = form["description"]
        scale = _parse_yaml_field(form.get("scale"), "scale")
        if scale:
            tag["scale"] = scale
        return tag

    def tag_row(request: Request, device_id: str, tag: dict[str, Any], *, edit: bool = False,
                errors: dict[str, str] | None = None, general: list[str] | None = None,
                values: dict[str, Any] | None = None, note: str | None = None,
                headers: dict[str, str] | None = None, row_id: str | None = None) -> Response:
        from opencap.config.models import DType
        from typing import get_args

        st = need_store()
        ov = ((st.get_device(device_id)["override"] or {}).get("tags") or {})
        return html(request, "_tag_row.html", device_id=device_id, t=tag, edit=edit, errors=errors or {},
                    general=general or [], v=values or _form_tag_values(tag), dtypes=list(get_args(DType)),
                    tag_overrides=ov, note=note, headers=headers, row_id=row_id or _domid(tag["name"]))

    @r.get("/config/tag-row/{device_id:path}", dependencies=view)
    def tag_row_get(request: Request, device_id: str, tag: str, edit: bool = False) -> Response:
        st = need_store()
        try:
            t = st.get_tag(device_id, tag)["tag"]
        except ConfigError as exc:
            return flash(request, exc.message)
        if edit and not request.state.principal.is_admin:
            raise UiForbidden("read-only: your role is viewer")
        return tag_row(request, device_id, t, edit=edit)

    @r.post("/config/tag-update/{device_id:path}", dependencies=admin)
    async def tag_update(request: Request, device_id: str, tag: str) -> Response:
        st = need_store()
        form = await form_of(request)
        try:
            new = tag_from_form(form)
            result = await act(request, lambda: st.update_tag(device_id, tag, new, partial=False))
        except Invalid as exc:
            fields, general = split_errors(exc, TAG_FIELDS)
            values = {**_form_tag_values({"name": tag}), **{k: v for k, v in form.items() if k in TAG_FIELDS}}
            values["enabled"] = form.get("enabled", "true") == "true"
            return tag_row(request, device_id, {"name": tag}, edit=True, errors=fields, general=general,
                           values=values)
        except ConfigError as exc:
            return flash(request, exc.message)
        return tag_row(request, device_id, result.data, note=_saved_note(result), headers=changed(),
                       row_id=_domid(tag))

    @r.post("/config/tag-delete/{device_id:path}", dependencies=admin)
    async def tag_delete(request: Request, device_id: str, tag: str) -> Response:
        st = need_store()
        try:
            await act(request, lambda: st.delete_tag(device_id, tag))
        except ConfigError as exc:
            return flash(request, exc.message)
        return HTMLResponse("", headers={**SECURITY_HEADERS, **changed()})

    @r.post("/config/tag-create/{device_id:path}", dependencies=admin)
    async def tag_create(request: Request, device_id: str) -> Response:
        from opencap.config.models import DType
        from typing import get_args

        st = need_store()
        form = await form_of(request)
        try:
            new = tag_from_form(form)
            result = await act(request, lambda: st.create_tags(device_id, new))
        except Invalid as exc:
            fields, general = split_errors(exc, TAG_FIELDS)
            values = {k: v for k, v in form.items() if k in TAG_FIELDS}
            values["enabled"] = form.get("enabled", "true") == "true"
            return html(request, "_tag_create.html", device_id=device_id, new_tag=values, tag_errors=fields,
                        tag_general=general, dtypes=list(get_args(DType)))
        except ConfigError as exc:
            return flash(request, exc.message)
        row = tag_row(request, device_id, result.data, note=_saved_note(result))
        # the fresh form replaces the old one; the new row rides along as an out-of-band swap
        # (inside <template>: a <tr> cannot stand next to a <form> in HTML parsing)
        return html(request, "_tag_create.html", device_id=device_id,
                    new_tag={"dtype": new["dtype"], "enabled": True}, tag_errors={}, tag_general=[],
                    dtypes=list(get_args(DType)), new_row=Markup(row.body.decode()), headers=changed())

    # publish map / overrides --------------------------------------------------------
    @r.post("/config/map-update/{device_id:path}", dependencies=admin)
    async def map_update(request: Request, device_id: str) -> Response:
        st = need_store()
        form = await form_of(request)
        delete = form.get("action") == "delete"
        body: dict[str, Any] = {k: form[k].strip() for k in ("sub_device_id", "topic", "env")
                                if form.get(k, "").strip()}
        if form.get("stub") == "true":
            body["stub"] = True
        try:
            if delete:
                result = await act(request, lambda: st.delete_map_entry(device_id))
            else:
                result = await act(request, lambda: st.put_map_entry(device_id, body, partial=False))
        except Invalid as exc:
            fields, general = split_errors(exc, MAP_FIELDS)
            return html(request, "_map_form.html", device_id=device_id, map_entry={**body, "stub": form.get("stub") == "true"},
                        has_map=not delete, map_errors=fields, map_general=general, map_note=None)
        except ConfigError as exc:
            return flash(request, exc.message)
        entry = (st.get_device(device_id)["map_entry"])
        return html(request, "_map_form.html", device_id=device_id, map_entry=entry or {}, has_map=entry is not None,
                    map_errors={}, map_general=[], map_note=_saved_note(result), headers=changed())

    @r.post("/config/override-update/{device_id:path}", dependencies=admin)
    async def override_update(request: Request, device_id: str) -> Response:
        st = need_store()
        form = await form_of(request)
        text = form.get("override", "")
        try:
            body = _parse_yaml_field(text, "override")
            if body:
                result = await act(request, lambda: st.put_device_override(device_id, body, partial=False))
            elif st.get_device(device_id)["override"] is not None:
                result = await act(request, lambda: st.delete_device_override(device_id))
            else:
                return html(request, "_override_form.html", device_id=device_id, override_text="", ov_errors=[],
                            ov_note="nothing to save")
        except Invalid as exc:
            fields, general = split_errors(exc, ())
            return html(request, "_override_form.html", device_id=device_id, override_text=text,
                        ov_errors=general + [f"{k}: {v}" for k, v in fields.items()], ov_note=None)
        except ConfigError as exc:
            return flash(request, exc.message)
        return html(request, "_override_form.html", device_id=device_id,
                    override_text=_yaml_text(st.get_device(device_id)["override"]), ov_errors=[],
                    ov_note=_saved_note(result), headers=changed())

    # --- check ---------------------------------------------------------------------
    @r.get("/check", dependencies=view)
    def check_page(request: Request, site: str | None = None) -> Response:
        from opencap.core.plan_report import check_report

        report = check_report(need_store().dir, site=site or None)
        return html(request, "check.html", report=report, site=site or "")

    # --- import --------------------------------------------------------------------
    @r.get("/import", dependencies=view)
    def import_page(request: Request) -> Response:
        need_store()
        return html(request, "import.html")

    async def _import(request: Request, dry_run: bool, filename: str | None) -> Response:
        st = need_store()
        try:
            raw = await read_json_body(request)
            rev = request.headers.get("x-config-revision") if not dry_run else None
            report = await run_in_threadpool(st.import_config, raw, dry_run=dry_run, filename=filename,
                                             if_match=rev)
            body = json.loads((await run_in_threadpool(import_response, ctx, request.state.principal, report)).body)
        except Invalid as exc:
            _, general = split_errors(exc, ())
            return html(request, "_import_result.html", report=None, errors=general, filename=filename or "")
        except ConfigError as exc:
            return html(request, "_import_result.html", report=None, errors=[exc.message], filename=filename or "")
        return html(request, "_import_result.html", report=body, errors=[], filename=filename or "",
                    headers=changed() if not dry_run else None)

    @r.post("/import/preview", dependencies=admin)
    async def import_preview(request: Request, filename: str | None = None) -> Response:
        return await _import(request, True, filename)

    @r.post("/import/apply", dependencies=admin)
    async def import_apply(request: Request, filename: str | None = None) -> Response:
        return await _import(request, False, filename)

    # --- audit ---------------------------------------------------------------------
    @r.get("/audit", dependencies=view)
    def audit_page(request: Request, action: str = "", who: str = "", target: str = "", page: int = 0) -> Response:
        per = 100
        entries = ctx.audit.tail(per + 1, max(page, 0) * per, action=action or None, who=who or None,
                                 target=target or None)
        return html(request, "audit.html", entries=entries[:per], more=len(entries) > per, page=max(page, 0),
                    action=action, who=who, target=target, enabled=ctx.audit.path is not None)

    app.include_router(r)
    app.mount("/ui/static", StaticFiles(directory=str(STATIC)), name="ui-static")

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/ui", status_code=307)


def _saved_note(result: Result) -> str:
    if not result.files:
        return "No change."
    note = f"Saved to {', '.join(result.files)}. Reload to apply."
    device = result.target.split("#", 1)[0]
    mine = [w for w in result.warnings if w.startswith(f"{device}:")]
    return note + (" Warning: " + " ".join(mine) if mine else "")


__all__ = ["install_ui", "split_errors", "AuthError"]
