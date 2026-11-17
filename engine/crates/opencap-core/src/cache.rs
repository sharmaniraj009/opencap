//! Value cache (Python `opencap.core.cache.ValueCache`).
//!
//! One cache per process: channel workers write, the publisher and the API read. A single
//! short-held mutex replaces Python's "one event loop, no locking".
//!
//! Rules:
//! - A good value replaces value, quality and `source_ts`.
//! - Any other quality keeps the last value and the last good `source_ts`; only the
//!   quality changes. A tag never read good has value `None` and `source_ts` `None`.
//! - `version()` bumps on every `update()` and `register()`, and on an `expire()` that
//!   changed something.
//! - Reads of an unknown device are lenient (empty, 0, `None`); `update`/`expire` of an
//!   unknown device are ignored (Python raises KeyError; nothing in the engine relies on it).

use std::collections::HashMap;
use std::sync::Mutex;

use chrono::{DateTime, Utc};
use indexmap::IndexMap;

use crate::config::Device;
use crate::value::{Quality, TagValue};

pub type Snapshot = IndexMap<String, TagValue>;

#[derive(Default)]
struct Inner {
    tags: HashMap<String, Snapshot>,
    versions: HashMap<String, u64>,
    last_good: HashMap<String, Option<DateTime<Utc>>>,
}

impl Inner {
    fn bump(&mut self, device_id: &str) {
        *self.versions.entry(device_id.to_string()).or_insert(0) += 1;
    }
}

#[derive(Default)]
pub struct ValueCache {
    inner: Mutex<Inner>,
}

impl ValueCache {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Pre-fill every enabled tag with `TagValue(None, stale, None)`. Idempotent: on a
    /// second call (reload) tags that still exist keep their value and quality, new tags
    /// are added as stale and removed tags are dropped.
    pub fn register(&self, device: &Device) {
        let mut inner = self.lock();
        let old = inner.tags.remove(&device.id).unwrap_or_default();
        let tags: Snapshot = device
            .tags
            .iter()
            .filter(|t| t.enabled)
            .map(|t| {
                let tv = old.get(&t.name).cloned().unwrap_or_else(|| TagValue::bad(Quality::Stale, None));
                (t.name.clone(), tv)
            })
            .collect();
        inner.tags.insert(device.id.clone(), tags);
        inner.last_good.entry(device.id.clone()).or_insert(None);
        inner.bump(&device.id);
    }

    /// Apply one batch of results. Names that are not registered are ignored.
    pub fn update<'a, I>(&self, device_id: &str, values: I)
    where
        I: IntoIterator<Item = (&'a String, &'a TagValue)>,
    {
        let mut inner = self.lock();
        let Some(tags) = inner.tags.get_mut(device_id) else { return };
        let mut newest: Option<DateTime<Utc>> = None;
        for (name, tv) in values {
            let Some(entry) = tags.get_mut(name) else { continue };
            if tv.quality == Quality::Good {
                *entry = tv.clone();
                if let Some(ts) = tv.source_ts {
                    if newest.is_none_or(|n| ts > n) {
                        newest = Some(ts);
                    }
                }
            } else {
                entry.quality = tv.quality; // keep last value and last good ts
            }
        }
        let slot = inner.last_good.entry(device_id.to_string()).or_insert(None);
        if let Some(ts) = newest {
            if slot.is_none_or(|old| ts > old) {
                *slot = Some(ts);
            }
        }
        inner.bump(device_id);
    }

    /// Good tags whose last good read is older than `max_age_s[name]` become stale.
    pub fn expire(&self, device_id: &str, now: DateTime<Utc>, max_age_s: &HashMap<String, f64>) {
        let mut inner = self.lock();
        let Some(tags) = inner.tags.get_mut(device_id) else { return };
        let mut changed = false;
        for (name, max_age) in max_age_s {
            let Some(entry) = tags.get_mut(name) else { continue };
            if entry.quality != Quality::Good {
                continue;
            }
            let Some(ts) = entry.source_ts else { continue };
            let age = (now - ts).num_microseconds().map(|us| us as f64 / 1e6).unwrap_or(f64::MAX);
            if age > *max_age {
                entry.quality = Quality::Stale;
                changed = true;
            }
        }
        if changed {
            inner.bump(device_id);
        }
    }

    pub fn unregister(&self, device_id: &str) {
        let mut inner = self.lock();
        inner.tags.remove(device_id);
        inner.versions.remove(device_id);
        inner.last_good.remove(device_id);
    }

    /// Copy of every registered tag of the device, in tag order (empty if not registered).
    pub fn snapshot(&self, device_id: &str) -> Snapshot {
        self.lock().tags.get(device_id).cloned().unwrap_or_default()
    }

    pub fn get(&self, device_id: &str, name: &str) -> Option<TagValue> {
        self.lock().tags.get(device_id).and_then(|t| t.get(name)).cloned()
    }

    pub fn version(&self, device_id: &str) -> u64 {
        self.lock().versions.get(device_id).copied().unwrap_or(0)
    }

    /// Newest good `source_ts` seen on the device.
    pub fn last_good(&self, device_id: &str) -> Option<DateTime<Utc>> {
        self.lock().last_good.get(device_id).copied().flatten()
    }

    pub fn devices(&self) -> Vec<String> {
        self.lock().tags.keys().cloned().collect()
    }

    pub fn contains(&self, device_id: &str) -> bool {
        self.lock().tags.contains_key(device_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{DType, Tag};
    use crate::value::Value;
    use chrono::{Duration, TimeZone};

    fn dev(tags: &[(&str, bool)]) -> Device {
        Device::new(
            "S/D",
            "S",
            "D",
            "modbus_tcp",
            "h",
            1,
            tags.iter().map(|(n, e)| Tag { enabled: *e, ..Tag::new(n, "400001", DType::Float32) }).collect(),
        )
    }

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 16, 10, 0, 0).unwrap()
    }

    fn upd(c: &ValueCache, vals: &[(&str, TagValue)]) {
        let m: IndexMap<String, TagValue> = vals.iter().map(|(k, v)| (k.to_string(), v.clone())).collect();
        c.update("S/D", &m);
    }

    #[test]
    fn register_prefills_enabled_tags_as_stale() {
        let c = ValueCache::new();
        c.register(&dev(&[("A", true), ("B", false), ("C", true)]));
        let s = c.snapshot("S/D");
        assert_eq!(s.keys().collect::<Vec<_>>(), vec!["A", "C"]);
        assert!(s.values().all(|tv| *tv == TagValue::bad(Quality::Stale, None)));
        assert_eq!(c.version("S/D"), 1);
        assert_eq!(c.last_good("S/D"), None);
        assert!(c.snapshot("nope").is_empty() && c.version("nope") == 0);
    }

    #[test]
    fn good_replaces_bad_keeps_last_value() {
        let c = ValueCache::new();
        c.register(&dev(&[("A", true), ("B", true)]));
        upd(&c, &[("A", TagValue::good(Value::Float(1.5), t0())), ("GHOST", TagValue::good(Value::Int(1), t0()))]);
        assert_eq!(c.get("S/D", "A").unwrap().value, Some(Value::Float(1.5)));
        upd(&c, &[("A", TagValue::bad(Quality::BadComm, Some(t0() + Duration::seconds(9))))]);
        let a = c.get("S/D", "A").unwrap();
        assert_eq!((a.value, a.quality, a.source_ts), (Some(Value::Float(1.5)), Quality::BadComm, Some(t0())));
        assert_eq!(c.last_good("S/D"), Some(t0()));
        assert_eq!(c.version("S/D"), 3);
        assert!(c.get("S/D", "GHOST").is_none());
    }

    #[test]
    fn expire_marks_old_good_tags_stale() {
        let c = ValueCache::new();
        c.register(&dev(&[("A", true), ("B", true)]));
        upd(
            &c,
            &[
                ("A", TagValue::good(Value::Int(1), t0())),
                ("B", TagValue::good(Value::Int(2), t0() + Duration::seconds(5))),
            ],
        );
        let v = c.version("S/D");
        let ages: HashMap<String, f64> = [("A".to_string(), 3.0), ("B".to_string(), 3.0)].into();
        c.expire("S/D", t0() + Duration::seconds(6), &ages);
        assert_eq!(c.get("S/D", "A").unwrap().quality, Quality::Stale);
        assert_eq!(c.get("S/D", "B").unwrap().quality, Quality::Good);
        assert_eq!(c.version("S/D"), v + 1);
        c.expire("S/D", t0() + Duration::seconds(6), &ages);
        assert_eq!(c.version("S/D"), v + 1); // nothing changed, no bump
    }

    #[test]
    fn reregister_keeps_existing_values() {
        let c = ValueCache::new();
        c.register(&dev(&[("A", true), ("B", true)]));
        upd(&c, &[("A", TagValue::good(Value::Int(1), t0()))]);
        c.register(&dev(&[("A", true), ("N", true)]));
        let s = c.snapshot("S/D");
        assert_eq!(s.keys().collect::<Vec<_>>(), vec!["A", "N"]);
        assert_eq!(s["A"].value, Some(Value::Int(1)));
        c.unregister("S/D");
        assert!(!c.contains("S/D"));
    }
}
