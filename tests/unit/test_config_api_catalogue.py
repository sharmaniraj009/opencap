"""Driver catalogue, driver-schema validation of device writes, and filtering, sorting and paging
of the list routes (API-16, API-17, API-18; #129). Fixtures and helpers come from test_config_api."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.unit.test_config_api import (  # noqa: F401  (env is a fixture)
    ADMIN,
    DEV,
    DEV_PATH,
    V1,
    VIEWER,
    admin_w,
    env,
    rev,
)

# --- driver catalogue (API-16) ---------------------------------------------------------------


def test_driver_catalogue_lists_every_registered_driver(env):
    from opencap.drivers import DRIVERS

    c, *_ = env
    r = c.get(f"{V1}/drivers", headers=VIEWER)
    assert r.status_code == 200
    rows = {d["name"]: d for d in r.json()["drivers"]}
    assert set(rows) == set(DRIVERS)
    assert all({"name", "transport", "supports_identity", "supports_browse"} <= set(d) for d in rows.values())
    assert rows["modbus_tcp"]["supports_identity"] is True and rows["modbus_tcp"]["supports_browse"] is False
    assert c.get(f"{V1}/drivers").status_code == 401


def test_driver_catalogue_entry_has_schema_dtypes_and_examples(env):
    c, *_ = env
    r = c.get(f"{V1}/drivers/s7", headers=VIEWER)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "s7" and body["params_schema"]["properties"]["rack"]["maximum"] == 7
    assert "float32" in body["dtypes"] and "DB1,REAL8" in body["address_examples"]
    assert c.get(f"{V1}/drivers/nope", headers=VIEWER).status_code == 404


def test_device_params_breaking_the_driver_schema_are_422_under_params(env):
    c, _, cfg, _ = env
    before = (cfg / "devices.json").read_bytes()
    r = c.patch(DEV_PATH, json={"params": {"unit_id": 300, "zero_based": "yes", "nonnormal_float": "maybe"}},
                headers=admin_w(c))
    assert r.status_code == 422
    errors = {tuple(e["loc"]) for e in r.json()["detail"]}
    assert errors == {("params", "unit_id"), ("params", "zero_based"), ("params", "nonnormal_float")}
    assert "yes" not in json.dumps(r.json())                       # values are not echoed
    channel = DEV.split("/")[0]
    r = c.post(f"{V1}/channels/{channel}/devices", headers=admin_w(c),
               json={"name": "New", "driver": "s7", "host": "192.0.2.77", "port": 102, "params": {"rack": 9}})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["params", "rack"]
    r = c.post(f"{V1}/channels", headers=admin_w(c),
               json={"name": "Fresh", "devices": [{"name": "P", "driver": "s7", "host": "192.0.2.78", "port": 102,
                                                   "params": {"slot": 99}}]})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["devices", 0, "params", "slot"]
    assert (cfg / "devices.json").read_bytes() == before


def test_valid_params_and_unknown_keys_are_still_accepted(env):
    c, *_ = env
    r = c.patch(DEV_PATH, json={"params": {"unit_id": 5, "max_gap_bits": None, "vendor_note": "kept"}},
                headers=admin_w(c))
    assert r.status_code == 200, r.text


# --- filtering, sorting, paging (API-17, API-18) -----------------------------------------------


def _populate(c: TestClient) -> str:
    """Channels Plant10, plant2, Plant1 (natural order) and a device with tags A10, A2, A1, A12, A3."""
    for name in ("Plant10", "plant2", "Plant1"):
        assert c.post(f"{V1}/channels", json={"name": name}, headers=admin_w(c)).status_code == 201
    dev = {"name": "Dev", "driver": "simulator", "host": "192.0.2.5", "port": 1}
    assert c.post(f"{V1}/channels/Plant1/devices", json=dev, headers=admin_w(c)).status_code == 201
    tags = [{"name": f"A{n}", "address": f"CONST({n})", "dtype": "int16",
             "description": "Pump flow" if n % 2 else "Tank level", "scan_ms": 1000 + n} for n in (10, 2, 1, 12, 3)]
    assert c.post(f"{V1}/devices/Plant1/Dev/tags", json=tags, headers=admin_w(c)).status_code == 201
    return "Plant1/Dev"


def test_list_without_parameters_returns_everything_with_null_limit(env):
    c, *_ = env
    dev = _populate(c)
    body = c.get(f"{V1}/devices/{dev}/tags", headers=VIEWER).json()
    assert [t["name"] for t in body["tags"]] == ["A10", "A2", "A1", "A12", "A3"]      # file order, as before
    assert (body["total"], body["limit"], body["offset"]) == (5, None, 0)
    assert body["revision"] == rev(c)


def test_filter_matches_name_and_description_without_case(env):
    c, *_ = env
    dev = _populate(c)

    def names(url: str) -> list[str]:
        return [t["name"] for t in c.get(f"{V1}{url}", headers=VIEWER).json()["tags"]]

    assert names(f"/devices/{dev}/tags?q=a1") == ["A10", "A1", "A12"]
    assert names(f"/devices/{dev}/tags?q=TANK&sort=name") == ["A2", "A10", "A12"]
    assert c.get(f"{V1}/channels?q=PLANT1", headers=VIEWER).json()["total"] == 2          # Plant1, Plant10
    empty = c.get(f"{V1}/devices?q=zzz", headers=VIEWER).json()
    assert empty == {"devices": [], "total": 0, "limit": None, "offset": 0, "revision": rev(c)}


def test_sort_is_natural_for_strings_and_numeric_for_numbers(env):
    c, *_ = env
    dev = _populate(c)

    def get(path: str) -> dict:
        return c.get(f"{V1}{path}", headers=VIEWER).json()

    assert [t["name"] for t in get(f"/devices/{dev}/tags?sort=name")["tags"]] == ["A1", "A2", "A3", "A10", "A12"]
    desc = get(f"/devices/{dev}/tags?sort=name&order=desc")
    assert [t["name"] for t in desc["tags"]] == ["A12", "A10", "A3", "A2", "A1"]
    by_rate = get(f"/devices/{dev}/tags?sort=scan_ms&order=desc")          # 1012 > 1010 > 1003 > 1002 > 1001
    assert [t["name"] for t in by_rate["tags"]] == ["A12", "A10", "A3", "A2", "A1"]
    names = [ch["name"] for ch in get("/channels?sort=name")["channels"]]
    assert names.index("Plant1") < names.index("plant2") < names.index("Plant10")
    counts = [ch["devices"] for ch in get("/channels?sort=devices&order=desc")["channels"]]
    assert counts == sorted(counts, reverse=True) and counts[0] > counts[-1]                # numeric, not "10" < "9"


def test_unknown_sort_field_and_out_of_range_values_are_422(env):
    c, *_ = env
    dev = _populate(c)
    for url in ("/channels?sort=colour", "/devices?sort=params", f"/devices/{dev}/tags?sort=scale",
                "/channels?order=up", "/channels?limit=0", "/channels?limit=10001", "/channels?offset=-1",
                "/devices?limit=abc"):
        assert c.get(f"{V1}{url}", headers=VIEWER).status_code == 422, url
    r = c.get(f"{V1}/channels?sort=colour", headers=VIEWER)
    assert r.json()["detail"][0]["loc"] == ["query", "sort"]
    assert c.get(f"{V1}/channels?limit=10000", headers=VIEWER).status_code == 200


def test_paging_applies_after_filtering_and_sorting(env):
    c, *_ = env
    dev = _populate(c)

    def page(**kw) -> tuple[list[str], dict]:
        query = "&".join(f"{k}={v}" for k, v in kw.items())
        body = c.get(f"{V1}/devices/{dev}/tags?sort=name&{query}", headers=VIEWER).json()
        return [t["name"] for t in body["tags"]], body

    assert page(limit=2)[0] == ["A1", "A2"]                                   # first page
    names, body = page(limit=2, offset=2)
    assert names == ["A3", "A10"] and (body["total"], body["limit"], body["offset"]) == (5, 2, 2)   # middle
    assert page(limit=2, offset=4)[0] == ["A12"]                              # last, short page
    names, body = page(limit=2, offset=9)
    assert names == [] and body["total"] == 5                                 # empty page keeps the total
    names, body = page(q="a1", limit=2, offset=1)                             # filter first: A1, A10, A12
    assert names == ["A10", "A12"] and body["total"] == 3
    assert page(offset=3)[0] == ["A10", "A12"]                                # offset alone, no limit


def test_device_list_pages_and_filters_by_channel(env):
    c, *_ = env
    _populate(c)
    body = c.get(f"{V1}/devices?channel=Plant1&limit=1", headers=ADMIN).json()
    assert [d["id"] for d in body["devices"]] == ["Plant1/Dev"] and body["total"] == 1
