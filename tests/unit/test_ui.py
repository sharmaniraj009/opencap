"""Web UI bundle at /ui/ (ADR-0010: a static page that uses only the public /api/v1 API).

These tests load the pages over HTTP and check the key elements and the contract; the
rendering itself is exercised in a real browser by tests/integration/test_ui_browser.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencap.api.server import UI_DIR, create_app
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, api_cfg, make_config

VIEWS = ("login", "dashboard", "device", "config", "channel", "edit", "check", "import", "audit")
ALPINE = "vendor/alpine-csp-3.17.5.min.js"


@pytest.fixture
def client(tmp_path):
    app = create_app(ConfigPoller(make_config(tmp_path)), api_cfg(tmp_path), environ=ENV)
    return TestClient(app), app


def test_ui_is_served_without_a_token(client):
    c, _ = client
    assert c.get("/", follow_redirects=False).headers["location"] == "/ui/"
    assert c.get("/ui", follow_redirects=False).headers["location"] == "/ui/"
    r = c.get("/ui/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert c.get("/ui/app.js").status_code == 200 and c.get("/ui/app.css").status_code == 200
    assert c.get("/api/v1/status").status_code == 401                 # the data still needs a token


def test_page_has_every_view_and_the_vendored_framework(client):
    c, _ = client
    page = c.get("/ui/").text
    for view in VIEWS:
        assert f'id="view-{view}"' in page, view
    for link in ("#/dashboard", "#/config", "#/import", "#/audit"):
        assert f'href="{link}"' in page
    for element in ('id="pending"', 'id="device-form"', 'id="tags-body"', 'id="tag-create"', 'id="map-form"',
                    'id="override-form"', 'id="import-form"', 'name="token"', 'type="search"', "Read once",
                    "Live values", "Block plan", "bad_config tags", "Identity", "Reload now"):
        assert element in page, element
    assert page.index('src="app.js"') < page.index(f'src="{ALPINE}"')   # components registered first


def test_security_headers_and_csp_clean_markup(client):
    c, _ = client
    for path in ("/ui/", "/ui/app.js", f"/ui/{ALPINE}"):
        r = c.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
    page = c.get("/ui/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "inline script"
    assert " style=" not in page and not re.search(r"\son[a-z]+=", page)
    assert "`" not in page                                            # no template literals for the CSP build
    assert c.get("/ui/").headers["cache-control"] == "no-cache"


def test_vendored_alpine_and_its_licence(client):
    c, _ = client
    js = c.get(f"/ui/{ALPINE}")
    assert js.status_code == 200 and len(js.content) > 50_000
    lic = c.get("/ui/vendor/alpine-LICENSE.md")
    assert lic.status_code == 200 and "MIT License" in lic.text and "Caleb Porzio" in lic.text


def _paths(routes):
    """Route paths, also inside included routers (newer FastAPI keeps them nested)."""
    for r in routes:
        if getattr(r, "path", None):
            yield r.path
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from _paths(inner.routes)


def test_ui_uses_only_the_public_api(client):
    """ADR-0010: the bundle reaches the server only through call(), and every call() targets an
    existing /api/v1 route (no engine-internal calls)."""
    _, app = client
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1                                    # the one inside call()
    routes = list(_paths(app.routes))
    sites = re.findall(r'call\("(GET|POST|PUT|PATCH|DELETE)",\s*([^,)]+)', js)
    assert len(sites) > 30
    for _method, arg in sites:
        arg = arg.strip()
        if arg.startswith('"'):
            prefix = re.match(r'"([^"]*)"', arg).group(1)          # the literal before any `+ enc(...)`
        else:                                   # a variable such as `base` or `url`, built just before
            name = re.match(r"[A-Za-z_]+", arg).group(0)
            prefix = re.search(rf'\b{name} = "([^"]*)"', js).group(1)
        prefix = prefix.split("?", 1)[0]
        assert prefix.startswith("/api/v1/"), arg
        assert any(r == prefix.rstrip("/") or r.startswith(prefix) for r in routes), prefix


def test_ui_never_offers_a_plc_write():
    text = (UI_DIR / "index.html").read_text(encoding="utf-8") + (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"write (a |the )?(value|tag)|set ?point|set value", text.replace("never writes a PLC value", ""), re.I)


def test_bundle_files_are_packaged():
    """pyproject package-data must ship every file of the bundle in the wheel."""
    import tomllib

    data = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["opencap.api"]
    files = [p.relative_to(UI_DIR.parent).as_posix() for p in UI_DIR.rglob("*") if p.is_file()]
    for f in files:
        assert any(Path(f).match(g) for g in globs), f
