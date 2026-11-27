//! Read side of the configuration API (`/api/v1`, #140): the resources of `src/opencap/config/store.py`
//! (`ConfigStore.list_channels`, `get_device`, `get_map`, ...) built from the files of a config
//! directory, with the same shapes. A v1 directory is read from `devices.json`, `devices_map.yaml`
//! and `overrides.yaml`; a project v2 from `project.json` (its devices already carry their publish
//! settings, so it has no map entries and no overrides). Every answer carries the `revision`.
//!
//! Nothing here writes. `status` of an error is the HTTP status the API answers with.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use super::loader::{
    is_v2, load_config, load_devices, load_project, read_yaml, DEVICES_FILE, MAP_FILE, OVERRIDES_FILE, PROJECT_FILE,
};
use super::{config_revision, CONFIG_FILES, KNOWN_DRIVERS, V2_FILES};
use crate::pyfmt::py_repr_str as r;

/// A failed read: the HTTP status, a message, and pydantic-style `{loc, msg, type}` errors (422).
#[derive(Debug, Clone, PartialEq)]
pub struct ReadError {
    pub status: u16,
    pub message: String,
    pub errors: Vec<Json>,
}

impl ReadError {
    fn new(status: u16, message: impl Into<String>) -> Self {
        Self { status, message: message.into(), errors: Vec::new() }
    }
    fn not_found(message: impl Into<String>) -> Self {
        Self::new(404, message)
    }
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

pub const MAX_LIMIT: usize = 10_000;
pub const CHANNEL_SORT: [&str; 3] = ["name", "enabled", "devices"];
pub const DEVICE_SORT: [&str; 8] = ["id", "channel", "name", "driver", "host", "port", "enabled", "tags"];
pub const TAG_SORT: [&str; 6] = ["name", "address", "dtype", "scan_ms", "enabled", "description"];

/// `q`, `sort`, `order`, `limit`, `offset` of the list routes (API-17, API-18).
#[derive(Debug, Clone, Default)]
pub struct CollectionQuery {
    pub q: Option<String>,
    pub sort: Option<String>,
    pub descending: bool,
    pub limit: Option<usize>,
    pub offset: usize,
}

#[derive(Debug)]
pub struct Page {
    pub items: Vec<Json>,
    pub total: usize,
    pub limit: Option<usize>,
    pub offset: usize,
}

/// `A2` before `A10`, without case: runs of digits compare as numbers.
fn natural(text: &str) -> Vec<(u8, u64, String)> {
    let lower = text.to_lowercase();
    let mut out = Vec::new();
    let mut chars = lower.chars().peekable();
    while let Some(&c) = chars.peek() {
        let digit = c.is_ascii_digit();
        let mut run = String::new();
        while let Some(&c) = chars.peek() {
            if c.is_ascii_digit() != digit {
                break;
            }
            run.push(c);
            chars.next();
        }
        out.push(if digit { (0, run.parse().unwrap_or(u64::MAX), String::new()) } else { (1, 0, run) });
    }
    out
}

fn compare(a: &Json, b: &Json) -> std::cmp::Ordering {
    match (a, b) {
        (Json::String(x), Json::String(y)) => natural(x).cmp(&natural(y)),
        (Json::Number(x), Json::Number(y)) => x.as_f64().partial_cmp(&y.as_f64()).unwrap_or(std::cmp::Ordering::Equal),
        (Json::Bool(x), Json::Bool(y)) => x.cmp(y),
        _ => std::cmp::Ordering::Equal,
    }
}

/// Filter, sort, then page `items`; 422 for a `sort` field the collection does not offer.
pub fn apply_query(mut items: Vec<Json>, query: &CollectionQuery, sort_fields: &[&str]) -> Result<Page, ReadError> {
    if let Some(sort) = query.sort.as_deref().filter(|s| !sort_fields.contains(s)) {
        let _ = sort;
        return Err(ReadError {
            status: 422,
            message: "cannot sort by that field".into(),
            errors: vec![
                json!({"loc": ["query", "sort"], "msg": format!("sort must be one of {}", sort_fields.join(", ")), "type": "enum"}),
            ],
        });
    }
    if let Some(q) = query.q.as_deref().filter(|q| !q.is_empty()) {
        let needle = q.to_lowercase();
        items.retain(|i| {
            ["name", "description"]
                .iter()
                .any(|f| i.get(f).and_then(Json::as_str).is_some_and(|s| s.to_lowercase().contains(&needle)))
        });
    }
    if let Some(sort) = query.sort.as_deref() {
        let (mut present, missing): (Vec<Json>, Vec<Json>) =
            items.into_iter().partition(|i| i.get(sort).is_some_and(|v| !v.is_null()));
        present.sort_by(|a, b| {
            let o = compare(&a[sort], &b[sort]);
            if query.descending {
                o.reverse()
            } else {
                o
            }
        });
        present.extend(missing);
        items = present;
    }
    let total = items.len();
    let page: Vec<Json> = match query.limit {
        None => items.into_iter().skip(query.offset).collect(),
        Some(n) => items.into_iter().skip(query.offset).take(n).collect(),
    };
    Ok(Page { items: page, total, limit: query.limit, offset: query.offset })
}

/// Python `model_dump(mode="json", exclude_none=True)`: no `null` keys, at any depth.
fn without_nulls(v: Json) -> Json {
    match v {
        Json::Object(o) => {
            Json::Object(o.into_iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| (k, without_nulls(v))).collect())
        }
        Json::Array(a) => Json::Array(a.into_iter().map(without_nulls).collect()),
        other => other,
    }
}

struct State {
    revision: String,
    /// `{"version", "channels": [...], "excluded": [...]}` as the models dump it.
    devices: Json,
    map: Map<String, Json>,
    overrides: Map<String, Json>,
    v2: bool,
}

pub struct ConfigReader {
    dir: PathBuf,
}

impl ConfigReader {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn revision(&self) -> String {
        config_revision(&self.dir)
    }

    fn state(&self) -> Result<State, ReadError> {
        let revision = self.revision();
        if is_v2(&self.dir) {
            let project = load_project(&self.dir.join(PROJECT_FILE))
                .map_err(|e| ReadError::new(409, format!("{PROJECT_FILE} is invalid: {e}")))?;
            let mut devices =
                without_nulls(serde_json::to_value(&project).map_err(|e| ReadError::new(500, e.to_string()))?);
            if let Some(o) = devices.as_object_mut() {
                for key in ["revision", "name", "title", "generated_from", "generated_at"] {
                    o.remove(key);
                }
            }
            return Ok(State { revision, devices, map: Map::new(), overrides: Map::new(), v2: true });
        }
        let path = self.dir.join(DEVICES_FILE);
        let devices = if path.exists() {
            let file =
                load_devices(&path).map_err(|e| ReadError::new(409, format!("{DEVICES_FILE} is invalid: {e}")))?;
            without_nulls(serde_json::to_value(&file).map_err(|e| ReadError::new(500, e.to_string()))?)
        } else {
            json!({"version": 1, "channels": [], "excluded": []})
        };
        // a YAML file that does not parse reads as empty (the summary reports why)
        let yaml = |name: &str| read_yaml(&self.dir.join(name)).unwrap_or_default();
        Ok(State { revision, devices, map: yaml(MAP_FILE), overrides: yaml(OVERRIDES_FILE), v2: false })
    }

    fn channels(state: &State) -> Vec<&Json> {
        state.devices["channels"].as_array().map(|a| a.iter().collect()).unwrap_or_default()
    }

    fn section(map: &Map<String, Json>, key: &str) -> Map<String, Json> {
        map.get(key).and_then(Json::as_object).cloned().unwrap_or_default()
    }

    fn device_summary(dev: &Json, dmap: &Map<String, Json>, ov: &Map<String, Json>, v2: bool) -> Json {
        let id = dev["id"].as_str().unwrap_or_default();
        let sub = if v2 {
            dev["publish"]["sub_device_id"].clone()
        } else {
            dmap.get(id).and_then(|e| e.get("sub_device_id")).cloned().unwrap_or(Json::Null)
        };
        json!({
            "id": dev["id"], "channel": dev["channel"], "name": dev["name"], "driver": dev["driver"],
            "host": dev["host"], "port": dev["port"], "enabled": dev.get("enabled").unwrap_or(&Json::Bool(true)),
            "tags": dev["tags"].as_array().map_or(0, Vec::len), "sub_device_id": sub, "has_override": ov.contains_key(id),
        })
    }

    pub fn list_channels(&self) -> Result<Json, ReadError> {
        let st = self.state()?;
        let ov = Self::section(&st.overrides, "channels");
        let channels: Vec<Json> = Self::channels(&st)
            .iter()
            .map(|c| {
                let name = c["name"].as_str().unwrap_or_default();
                json!({"name": c["name"], "enabled": c.get("enabled").unwrap_or(&Json::Bool(true)),
                       "devices": c["devices"].as_array().map_or(0, Vec::len), "override": ov.get(name)})
            })
            .collect();
        Ok(json!({"channels": channels, "revision": st.revision}))
    }

    pub fn get_channel(&self, name: &str) -> Result<Json, ReadError> {
        let st = self.state()?;
        let (dmap, ovd, ovc) = (
            Self::section(&st.map, "devices"),
            Self::section(&st.overrides, "devices"),
            Self::section(&st.overrides, "channels"),
        );
        for c in Self::channels(&st) {
            if c["name"] == name {
                let devices: Vec<Json> = c["devices"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|d| Self::device_summary(d, &dmap, &ovd, st.v2))
                    .collect();
                return Ok(json!({"name": name, "enabled": c.get("enabled").unwrap_or(&Json::Bool(true)),
                                 "override": ovc.get(name), "devices": devices, "revision": st.revision}));
            }
        }
        Err(ReadError::not_found(format!("unknown channel {}", r(name))))
    }

    /// Every device, or one channel's.
    pub fn list_devices(&self, channel: Option<&str>) -> Result<Json, ReadError> {
        let st = self.state()?;
        let (dmap, ovd) = (Self::section(&st.map, "devices"), Self::section(&st.overrides, "devices"));
        let devices: Vec<Json> = Self::channels(&st)
            .iter()
            .filter(|c| channel.is_none_or(|n| c["name"] == n))
            .flat_map(|c| c["devices"].as_array().into_iter().flatten())
            .map(|d| Self::device_summary(d, &dmap, &ovd, st.v2))
            .collect();
        Ok(json!({"devices": devices, "revision": st.revision}))
    }

    pub fn get_device(&self, id: &str) -> Result<Json, ReadError> {
        let st = self.state()?;
        for c in Self::channels(&st) {
            for d in c["devices"].as_array().into_iter().flatten() {
                if d["id"] == id {
                    return Ok(json!({"device": d, "map_entry": Self::section(&st.map, "devices").get(id),
                                     "override": Self::section(&st.overrides, "devices").get(id), "revision": st.revision}));
                }
            }
        }
        Err(ReadError::not_found(format!("unknown device {}", r(id))))
    }

    pub fn get_tag(&self, id: &str, tag: &str) -> Result<Json, ReadError> {
        let dev = self.get_device(id)?;
        for t in dev["device"]["tags"].as_array().into_iter().flatten() {
            if t["name"] == tag {
                let ov = dev["override"]["tags"].get(tag).cloned();
                return Ok(json!({"tag": t, "override": ov, "revision": dev["revision"]}));
            }
        }
        Err(ReadError::not_found(format!("device {} has no tag {}", r(id), r(tag))))
    }

    pub fn get_map(&self) -> Result<Json, ReadError> {
        let st = self.state()?;
        Ok(
            json!({"defaults": Self::section(&st.map, "defaults"), "devices": Self::section(&st.map, "devices"), "revision": st.revision}),
        )
    }

    pub fn get_map_entry(&self, id: &str) -> Result<Json, ReadError> {
        let st = self.state()?;
        match Self::section(&st.map, "devices").get(id) {
            Some(entry) => Ok(json!({"id": id, "entry": entry, "revision": st.revision})),
            None => Err(ReadError::not_found(format!("{MAP_FILE} has no entry for {}", r(id)))),
        }
    }

    pub fn get_overrides(&self) -> Result<Json, ReadError> {
        let st = self.state()?;
        Ok(
            json!({"channels": Self::section(&st.overrides, "channels"), "devices": Self::section(&st.overrides, "devices"),
                  "serial_ports": Self::section(&st.overrides, "serial_ports"), "revision": st.revision}),
        )
    }

    pub fn get_device_override(&self, id: &str) -> Result<Json, ReadError> {
        let st = self.state()?;
        match Self::section(&st.overrides, "devices").get(id) {
            Some(ov) => Ok(json!({"id": id, "override": ov, "revision": st.revision})),
            None => Err(ReadError::not_found(format!("{OVERRIDES_FILE} has no override for device {}", r(id)))),
        }
    }

    pub fn get_channel_override(&self, name: &str) -> Result<Json, ReadError> {
        let st = self.state()?;
        match Self::section(&st.overrides, "channels").get(name) {
            Some(ov) => Ok(json!({"name": name, "override": ov, "revision": st.revision})),
            None => Err(ReadError::not_found(format!("{OVERRIDES_FILE} has no override for channel {}", r(name)))),
        }
    }

    /// The config files' texts: a backup, or a body for a later import.
    pub fn export(&self) -> Result<Json, ReadError> {
        let names: &[&str] = if is_v2(&self.dir) { &V2_FILES } else { &CONFIG_FILES };
        let mut files = Map::new();
        for name in names {
            if let Ok(bytes) = std::fs::read(self.dir.join(name)) {
                let text = String::from_utf8_lossy(bytes.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(&bytes)).into_owned();
                files.insert((*name).to_string(), Json::String(text));
            }
        }
        Ok(
            json!({"format": "opencap-config", "version": 1, "opencap": crate::FIRM_VERSION, "revision": self.revision(), "files": files}),
        )
    }

    /// `GET /api/v1/project/status`: the files, whether they load, and the counts.
    pub fn summary(&self) -> Result<Json, ReadError> {
        let st = self.state().unwrap_or_else(|_| State {
            revision: self.revision(),
            devices: json!({"channels": [], "excluded": []}),
            map: Map::new(),
            overrides: Map::new(),
            v2: is_v2(&self.dir),
        });
        let names: &[&str] = if st.v2 { &V2_FILES } else { &CONFIG_FILES };
        let mut files = BTreeMap::new();
        for name in names {
            let entry = match std::fs::metadata(self.dir.join(name)) {
                Ok(m) => {
                    let modified = m.modified().ok().map(|t| {
                        chrono::DateTime::<chrono::Utc>::from(t).format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
                    });
                    json!({"exists": true, "bytes": m.len(), "modified": modified})
                }
                Err(_) => json!({"exists": false}),
            };
            files.insert((*name).to_string(), entry);
        }
        let (valid, error, warnings) = match load_config(&self.dir) {
            Ok(l) => (true, Json::Null, l.warnings),
            Err(e) => (false, Json::String(e.to_string().lines().next().unwrap_or_default().to_string()), Vec::new()),
        };
        let channels = Self::channels(&st);
        let tags: usize = channels
            .iter()
            .flat_map(|c| c["devices"].as_array().into_iter().flatten())
            .map(|d| d["tags"].as_array().map_or(0, Vec::len))
            .sum();
        Ok(json!({
            "revision": st.revision, "config_dir": self.dir.display().to_string(), "files": files,
            "valid": valid, "error": error, "errors": [], "warnings": warnings,
            "counts": {"channels": channels.len(),
                       "devices": channels.iter().map(|c| c["devices"].as_array().map_or(0, Vec::len)).sum::<usize>(),
                       "tags": tags, "excluded": st.devices["excluded"].as_array().map_or(0, Vec::len)},
            "drivers": KNOWN_DRIVERS,
            "dtypes": ["bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64", "string"],
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("opencap-reader-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn devices() -> Json {
        let tag = |n: &str| json!({"name": n, "address": "400001", "dtype": "int16"});
        let dev = |ch: &str, n: &str, host: &str, tags: Vec<Json>| json!({"id": format!("{ch}/{n}"), "channel": ch, "name": n, "driver": "modbus_tcp", "host": host, "port": 502, "tags": tags});
        json!({"channels": [
            {"name": "Site 2", "devices": [dev("Site 2", "PLC10", "192.0.2.2", vec![tag("A"), tag("B")]), dev("Site 2", "PLC9", "192.0.2.3", vec![])]},
            {"name": "Site 10", "enabled": false, "devices": [dev("Site 10", "P", "192.0.2.4", vec![tag("T")])]},
        ]})
    }

    #[test]
    fn reads_of_a_v1_directory_have_the_store_shapes() {
        let d = dir("v1");
        std::fs::write(d.join(DEVICES_FILE), devices().to_string()).unwrap();
        std::fs::write(d.join(MAP_FILE), "devices:\n  Site 2/PLC10: {sub_device_id: SUB_10}\ndefaults: {env: test}\n")
            .unwrap();
        std::fs::write(
            d.join(OVERRIDES_FILE),
            "channels:\n  Site 2: {enabled: true}\ndevices:\n  Site 2/PLC9: {timeout_ms: 500, tags: {}}\n",
        )
        .unwrap();
        let rd = ConfigReader::new(&d);
        let rev = rd.revision();
        let ch = rd.list_channels().unwrap();
        assert_eq!(ch["revision"], rev);
        assert_eq!(
            ch["channels"][0],
            json!({"name": "Site 2", "enabled": true, "devices": 2, "override": {"enabled": true}})
        );
        assert_eq!(ch["channels"][1]["enabled"], false);
        let c = rd.get_channel("Site 2").unwrap();
        assert_eq!(c["devices"][0]["sub_device_id"], "SUB_10");
        assert_eq!(
            (c["devices"][0]["has_override"].as_bool(), c["devices"][1]["has_override"].as_bool()),
            (Some(false), Some(true))
        );
        assert_eq!(rd.get_channel("Nope").unwrap_err().to_string(), "unknown channel 'Nope'");
        assert_eq!(rd.list_devices(Some("Site 10")).unwrap()["devices"].as_array().unwrap().len(), 1);
        let dev = rd.get_device("Site 2/PLC9").unwrap();
        assert_eq!(dev["override"]["timeout_ms"], 500);
        assert_eq!(dev["device"]["timeout_ms"], 3000, "defaults are filled in, as the models dump them");
        assert!(dev["device"].get("plugin").is_none(), "null keys are left out");
        assert_eq!(rd.get_device("X/Y").unwrap_err().status, 404);
        assert_eq!(rd.get_tag("Site 2/PLC10", "B").unwrap()["tag"]["address"], "400001");
        assert_eq!(rd.get_tag("Site 2/PLC10", "Z").unwrap_err().to_string(), "device 'Site 2/PLC10' has no tag 'Z'");
        assert_eq!(rd.get_map().unwrap()["defaults"], json!({"env": "test"}));
        assert_eq!(rd.get_map_entry("Site 2/PLC10").unwrap()["entry"]["sub_device_id"], "SUB_10");
        assert_eq!(
            rd.get_map_entry("Site 2/PLC9").unwrap_err().to_string(),
            "devices_map.yaml has no entry for 'Site 2/PLC9'"
        );
        assert_eq!(rd.get_overrides().unwrap()["devices"]["Site 2/PLC9"]["timeout_ms"], 500);
        assert_eq!(rd.get_channel_override("Site 2").unwrap()["override"], json!({"enabled": true}));
        assert!(rd.get_device_override("Site 2/PLC10").is_err());
        let ex = rd.export().unwrap();
        assert_eq!(ex["format"], "opencap-config");
        assert!(ex["files"][MAP_FILE].as_str().unwrap().contains("SUB_10"));
        let s = rd.summary().unwrap();
        assert_eq!(
            (s["counts"]["channels"].as_i64(), s["counts"]["devices"].as_i64(), s["counts"]["tags"].as_i64()),
            (Some(2), Some(3), Some(3))
        );
        assert_eq!(s["files"][DEVICES_FILE]["exists"], true);
        assert_eq!(s["files"]["poller.yaml"]["exists"], false);
        assert_eq!(s["valid"], true);
    }

    #[test]
    fn a_missing_devices_file_is_empty_and_an_invalid_one_is_409() {
        let d = dir("bad");
        let rd = ConfigReader::new(&d);
        assert_eq!(rd.list_channels().unwrap()["channels"], json!([]));
        std::fs::write(d.join(DEVICES_FILE), "{nope").unwrap();
        let e = rd.list_channels().unwrap_err();
        assert_eq!(e.status, 409);
        assert!(e.message.starts_with("devices.json is invalid:"), "{}", e.message);
        assert_eq!(rd.summary().unwrap()["valid"], false);
    }

    #[test]
    fn a_project_v2_reads_without_map_or_overrides() {
        let d = dir("v2");
        let mut p = devices();
        p["version"] = json!(2);
        p["channels"][0]["devices"][0]["publish"] = json!({"sub_device_id": "SUB_V2"});
        std::fs::write(d.join(PROJECT_FILE), p.to_string()).unwrap();
        let rd = ConfigReader::new(&d);
        let list = rd.list_devices(None).unwrap();
        assert_eq!(list["devices"][0]["sub_device_id"], "SUB_V2");
        let dev = rd.get_device("Site 2/PLC10").unwrap();
        assert_eq!((dev["map_entry"].is_null(), dev["override"].is_null()), (true, true));
        assert!(rd.summary().unwrap()["files"].get(PROJECT_FILE).is_some());
    }

    #[test]
    fn collection_queries_filter_sort_and_page() {
        let items: Vec<Json> = ["A10", "a2", "B1", "A1"].iter().map(|n| json!({"name": n, "port": n.len()})).collect();
        let q = |sort: &str, desc: bool, limit: Option<usize>, offset: usize, text: Option<&str>| CollectionQuery {
            q: text.map(str::to_string),
            sort: Some(sort.to_string()).filter(|s| !s.is_empty()),
            descending: desc,
            limit,
            offset,
        };
        let names = |p: &Page| p.items.iter().map(|i| i["name"].as_str().unwrap().to_string()).collect::<Vec<_>>();
        let p = apply_query(items.clone(), &q("port", false, None, 0, None), &CHANNEL_SORT).unwrap_err();
        assert_eq!(p.status, 422, "port is not a channel sort field");
        assert_eq!(p.errors[0]["loc"], json!(["query", "sort"]));
        let p = apply_query(items.clone(), &q("name", false, None, 0, None), &CHANNEL_SORT).unwrap();
        assert_eq!(names(&p), ["A1", "a2", "A10", "B1"], "natural order, no case");
        let p = apply_query(items.clone(), &q("name", true, Some(2), 1, None), &CHANNEL_SORT).unwrap();
        assert_eq!((names(&p), p.total, p.limit, p.offset), (vec!["A10".to_string(), "a2".to_string()], 4, Some(2), 1));
        let p = apply_query(items, &q("", false, None, 0, Some("A1")), &CHANNEL_SORT).unwrap();
        assert_eq!((names(&p), p.total), (vec!["A10".to_string(), "A1".to_string()], 2), "substring, input order");
    }
}
