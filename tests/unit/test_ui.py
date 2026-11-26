"""Web UI bundle at /ui/ (ADR-0010: a static page that uses only the public /api/v1 API).

These tests load the pages over HTTP and check the key elements and the contract; the
rendering itself is exercised in a real browser by tests/integration/test_ui_browser.py.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencap.api.server import UI_DIR, create_app
from tests.helpers.fake_poller import ConfigPoller
from tests.unit.test_config_api import ENV, api_cfg, make_config

VIEWS = ("login", "dashboard", "device", "quick", "config", "channel", "edit", "check", "import", "audit")
ALPINE = "vendor/alpine-csp-3.17.5.min.js"
I18N = UI_DIR / "i18n"


def _table(lang: str = "en") -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


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
    for table in ("i18n/en.json", "i18n/languages.json"):
        r = c.get("/ui/" + table)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json"), table
    assert c.get("/api/v1/status").status_code == 401                 # the data still needs a token


def test_page_has_every_view_and_the_vendored_framework(client):
    c, _ = client
    page = c.get("/ui/").text
    for view in VIEWS:
        assert f'id="view-{view}"' in page, view
    for link in ("#/dashboard", "#/quick", "#/config", "#/import", "#/audit"):
        assert f'href="{link}"' in page
    for element in ('id="pending"', 'id="device-form"', 'id="tags-body"', 'id="tag-create"', 'id="map-form"',
                    'id="override-form"', 'id="import-form"', 'name="token"', 'type="search"',
                    'id="quick-values"', 'id="quick-mode"', 'name="lang"'):
        assert element in page, element
    texts = set(_table().values())
    for text in ("Read once", "Live values", "Block plan", "bad_config tags", "Identity", "Reload now", "Read now",
                 "Filter tags", "Updates"):
        assert text in texts, text
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
    # the one inside call(), and the one that loads the bundle's own string tables (i18n/, not the API)
    assert js.count("fetch(") == 2
    assert js.count("fetch(BASE + path,") == 1 and js.count("fetch(I18N_DIR + name,") == 1
    assert re.search(r'var I18N_DIR = "([^"]+)"', js).group(1) == "i18n/"
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
    # the one other way out: the live-values WebSocket, opened in openLive() on /api/v1/ws
    assert js.count("new WebSocket(") == 1
    live_path = re.search(r'var LIVE_PATH = "([^"]+)"', js).group(1)
    assert live_path == "/api/v1/ws" and live_path in routes
    assert "XMLHttpRequest" not in js and "EventSource" not in js and "sendBeacon" not in js


def test_quick_client_speaks_the_live_protocol():
    """The Quick Client subscribes with the documented message, sends the token only in the
    subprotocol (base64url), falls back to polling GET .../values, and demand-reads with read-once."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert '["opencap.v1"]' in js and '"opencap.bearer." + b64url(token)' in js
    assert "JSON.stringify({ subscribe: { devices:" in js
    for kind in ('"snapshot"', '"changes"', '"unavailable"', '"error"'):
        assert f"m.type === {kind}" in js, kind
    assert "m.version > this.version" in js                       # stale batches are ignored
    quick = js[js.index('Alpine.data("quickView"'):js.index("// ------------------------------------------------------------ config overview")]
    assert '"/values"' in quick and '"/read-once"' in quick and "fallback" in quick


def test_ui_never_offers_a_plc_write():
    text = "".join((UI_DIR / f).read_text(encoding="utf-8") for f in ("index.html", "app.js", "i18n/en.json"))
    assert not re.search(r"write (a |the )?(value|tag)|set ?point|set value", text.replace("never writes a PLC value", ""), re.I)


def test_bundle_files_are_packaged():
    """pyproject package-data must ship every file of the bundle in the wheel."""
    import tomllib

    data = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["opencap.api"]
    files = [p.relative_to(UI_DIR.parent).as_posix() for p in UI_DIR.rglob("*") if p.is_file()]
    for f in files:
        assert any(Path(f).match(g) for g in globs), f


# ---------------------------------------------------------------- string tables (#126)
class _Text(HTMLParser):
    """Text runs of the page outside the elements whose content is data or not shown by the UI."""

    SKIP = {"code", "script", "style", "title", "noscript"}
    VOID = {"input", "meta", "link", "br", "img", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.found: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        skip = tag in self.SKIP or "brand" in (a.get("class") or "") or bool(self.stack and self.stack[-1][1])
        for name in ("placeholder", "title", "aria-label", "data-label", "alt"):
            if a.get(name) and re.search(r"[A-Za-z]{2}", a[name]):
                self.attrs.append(f"{tag} {name}={a[name]!r}")
        if tag not in self.VOID:
            self.stack.append((tag, skip))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        if re.search(r"[A-Za-z]{2}", data) and not (self.stack and self.stack[-1][1]):
            self.found.append(data.strip())


def test_the_page_has_no_text_outside_the_table():
    """Static half of the pseudo-locale browser check: no text and no translatable attribute is
    written into index.html (only identifiers in <code>, the brand and the <noscript> note)."""
    parser = _Text()
    parser.feed((UI_DIR / "index.html").read_text(encoding="utf-8"))
    assert parser.found == []
    assert parser.attrs == []


def _first_arguments(code: str, names: str):
    """The first argument of every call of t(), tr(), rich() or hasText() in code."""
    for m in re.finditer(r"\b(?:%s)\(" % names, code):
        depth, i = 0, m.end()
        while i < len(code):
            ch = code[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}" and depth:
                depth -= 1
            elif ch in ",)" and not depth:
                break
            i += 1
        yield code[m.end():i]


def _keys_in_code() -> tuple[set[str], set[str], set[str]]:
    """Keys named in the translation calls, the prefixes of keys built at run time
    (tr("state." + s)), and the keys used with rich()."""
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    keys, prefixes = set(), set()
    for arg in list(_first_arguments(page, "t|rich")) + list(_first_arguments(js, "tr|hasText")):
        for m in re.finditer(r"""(["'])([a-z]+\.[a-z0-9_]*)\1(\s*\+)?""", arg):
            (prefixes if m.group(3) else keys).add(m.group(2))
    rich = {arg.strip("'") for arg in _first_arguments(page, "rich")}
    return keys, prefixes, rich


def test_every_key_the_code_uses_is_in_the_english_table_and_none_is_left_over():
    table = _table()
    keys, prefixes, _ = _keys_in_code()
    assert len(keys) > 250 and prefixes, "the call scan is out of date"
    assert sorted(k for k in keys if k not in table) == []
    assert [k for k in table if k not in keys and not any(k.startswith(p) for p in prefixes)] == []
    for prefix in prefixes:
        assert any(k.startswith(prefix) for k in table), prefix


def test_no_loop_variable_hides_the_translation_helpers():
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    for loop in re.findall(r'x-for="\(?([^")]*?)\)? in ', page):
        names = {n.strip() for n in loop.split(",")}
        assert not names & {"t", "rich"}, loop


def test_rich_strings_use_only_the_markup_the_renderer_builds():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "x-html" not in (UI_DIR / "index.html").read_text(encoding="utf-8")        # refused by the CSP build
    table = _table()
    _, _, rich = _keys_in_code()
    assert rich, "no rich() call found"
    for key in rich:
        for tag in re.findall(r"<[^>]*>", table[key]):
            assert re.fullmatch(r'</?(code|strong|em)>|<a href="(#/[^"]*|[A-Za-z][A-Za-z0-9_./-]*)">|</a>', tag), (key, tag)
    for key, text in table.items():
        if key not in rich:
            assert not re.search(r"</?(code|strong|em|a)\b", text), f"{key} has markup but is not used with rich()"
    assert "RICH_TAG = /<(\\/?)(code|strong|em|a)" in js


def test_languages_list_tables_that_exist_and_match_english():
    """i18n/languages.json lists English and every other table; a translation has only English
    keys and keeps each {placeholder} of the English string."""
    languages = json.loads((I18N / "languages.json").read_text(encoding="utf-8"))
    assert languages["en"] == "English"
    english = _table()
    assert all(isinstance(v, str) and v.strip() for v in english.values())
    for code in languages:
        assert re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", code), code
        table = _table(code)
        assert set(table) <= set(english), code
        for key, text in table.items():
            assert set(re.findall(r"\{(\w+)\}", text)) == set(re.findall(r"\{(\w+)\}", english[key])), (code, key)
    assert sorted(p.stem for p in I18N.glob("*.json") if p.stem != "languages") == sorted(languages)
