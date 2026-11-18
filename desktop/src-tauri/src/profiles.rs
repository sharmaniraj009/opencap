//! Connection profiles: which opencap engines the desktop app knows.
//!
//! A profile is a name and a base URL. Profiles are stored as JSON in the app's config
//! directory (`profiles.json`, mode 0600 on Unix). **Tokens are never written to that file**:
//! they live in the OS keychain (Secret Service on Linux, Credential Manager on Windows,
//! Keychain on macOS) through the [`SecretStore`] trait, keyed by the profile id.

use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// Keychain service name under which tokens are stored.
pub const KEYCHAIN_SERVICE: &str = "org.opencap.desktop";
/// Base URL of the default profile (an engine on this computer).
pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8080";

#[derive(Debug, thiserror::Error)]
pub enum ProfileError {
    #[error("{0}")]
    Invalid(String),
    #[error("unknown profile {0:?}")]
    NotFound(String),
    #[error("keychain: {0}")]
    Keychain(String),
    #[error("cannot read or write {path}: {source}")]
    Io {
        path: String,
        source: std::io::Error,
    },
    #[error("{path} is not valid: {source}")]
    Json {
        path: String,
        source: serde_json::Error,
    },
}

/// A stored profile (what `profiles.json` holds).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    pub id: String,
    pub name: String,
    pub base_url: String,
    /// The address the stored token was saved for. A token is only ever sent to that address:
    /// change the address and the token is no longer used (and is deleted from the keychain).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_origin: Option<String>,
}

/// What the launcher sees: a profile plus whether a token is stored for it (never the token).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProfileSummary {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub has_token: bool,
    /// Set when the keychain could not be asked (no Secret Service running, locked keychain).
    pub keychain_error: Option<String>,
    /// `http://` to a host that is not this computer: a token would travel unencrypted.
    pub cleartext: bool,
    /// Something the launcher should tell the user after a save (e.g. a token was removed).
    pub notice: Option<String>,
}

/// A create or update request from the launcher.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ProfileInput {
    /// None creates a new profile.
    pub id: Option<String>,
    pub name: String,
    pub base_url: String,
    /// A new token to store; None keeps the stored one.
    pub token: Option<String>,
    /// Remove the stored token (for an engine without tokens).
    #[serde(default)]
    pub clear_token: bool,
}

/// Where tokens are kept.
pub trait SecretStore: Send + Sync {
    fn get(&self, id: &str) -> Result<Option<String>, ProfileError>;
    fn set(&self, id: &str, secret: &str) -> Result<(), ProfileError>;
    fn delete(&self, id: &str) -> Result<(), ProfileError>;
}

/// The OS keychain.
pub struct KeyringStore;

impl KeyringStore {
    fn entry(id: &str) -> Result<keyring::Entry, ProfileError> {
        keyring::Entry::new(KEYCHAIN_SERVICE, id).map_err(|e| ProfileError::Keychain(e.to_string()))
    }
}

impl SecretStore for KeyringStore {
    fn get(&self, id: &str) -> Result<Option<String>, ProfileError> {
        match Self::entry(id)?.get_password() {
            Ok(secret) => Ok(Some(secret)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(ProfileError::Keychain(e.to_string())),
        }
    }

    fn set(&self, id: &str, secret: &str) -> Result<(), ProfileError> {
        Self::entry(id)?
            .set_password(secret)
            .map_err(|e| ProfileError::Keychain(e.to_string()))
    }

    fn delete(&self, id: &str) -> Result<(), ProfileError> {
        match Self::entry(id)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(ProfileError::Keychain(e.to_string())),
        }
    }
}

/// In-memory secrets, for tests and for systems without a keychain service.
#[derive(Default)]
pub struct MemoryStore(Mutex<HashMap<String, String>>);

impl SecretStore for MemoryStore {
    fn get(&self, id: &str) -> Result<Option<String>, ProfileError> {
        Ok(self.0.lock().unwrap().get(id).cloned())
    }
    fn set(&self, id: &str, secret: &str) -> Result<(), ProfileError> {
        self.0
            .lock()
            .unwrap()
            .insert(id.to_string(), secret.to_string());
        Ok(())
    }
    fn delete(&self, id: &str) -> Result<(), ProfileError> {
        self.0.lock().unwrap().remove(id);
        Ok(())
    }
}

/// The stored token was saved for the profile's current address.
fn token_is_for(p: &Profile) -> bool {
    p.token_origin.as_deref() == Some(p.base_url.as_str())
}

fn summary(
    p: Profile,
    has_token: bool,
    keychain_error: Option<String>,
    notice: Option<String>,
) -> ProfileSummary {
    let cleartext = is_cleartext_remote(&p.base_url);
    ProfileSummary {
        id: p.id,
        name: p.name,
        base_url: p.base_url,
        has_token,
        keychain_error,
        cleartext,
        notice,
    }
}

/// `http://` to a host other than this computer: what is sent (a token) is readable on the way.
pub fn is_cleartext_remote(base_url: &str) -> bool {
    let Ok(u) = url::Url::parse(base_url) else {
        return false;
    };
    if u.scheme() != "http" {
        return false;
    }
    match u.host() {
        Some(url::Host::Domain(d)) => !d.eq_ignore_ascii_case("localhost"),
        Some(url::Host::Ipv4(ip)) => !ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => !ip.is_loopback(),
        None => false,
    }
}

/// An engine's base URL as an origin: `http(s)://host[:port]`, no path, query, fragment or
/// credentials. A pasted UI address such as `http://127.0.0.1:8080/ui/#/dashboard` is accepted.
pub fn normalize_base_url(raw: &str) -> Result<String, ProfileError> {
    let raw = raw.trim();
    let parsed = url::Url::parse(raw).map_err(|e| {
        ProfileError::Invalid(format!("not a URL ({e}): use e.g. {DEFAULT_BASE_URL}"))
    })?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err(ProfileError::Invalid(format!(
            "the URL must start with http:// or https://, not {}:",
            parsed.scheme()
        )));
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err(ProfileError::Invalid(
            "put the token in the token field, not in the URL".into(),
        ));
    }
    let host = parsed
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| ProfileError::Invalid("the URL has no host".into()))?;
    let path = parsed.path().trim_end_matches('/');
    if !(path.is_empty() || path == "/ui") {
        return Err(ProfileError::Invalid(format!(
            "give the engine's address, without a path (got {path:?})"
        )));
    }
    let host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    Ok(match parsed.port() {
        Some(port) => format!("{}://{}:{}", parsed.scheme(), host, port),
        None => format!("{}://{}", parsed.scheme(), host),
    })
}

fn new_id(existing: &[Profile]) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut n = nanos;
    loop {
        let id = format!("p{n:x}");
        if !existing.iter().any(|p| p.id == id) {
            return id;
        }
        n += 1;
    }
}

/// Profiles in a JSON file, tokens in a [`SecretStore`].
pub struct ProfileStore {
    path: PathBuf,
    secrets: Box<dyn SecretStore>,
    lock: Mutex<()>,
}

impl ProfileStore {
    pub fn new(path: PathBuf, secrets: Box<dyn SecretStore>) -> Self {
        Self {
            path,
            secrets,
            lock: Mutex::new(()),
        }
    }

    /// The stored profiles; a first start gets one profile for an engine on this computer.
    pub fn load(&self) -> Result<Vec<Profile>, ProfileError> {
        let text = match fs::read_to_string(&self.path) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(vec![Profile {
                    id: "local".into(),
                    name: "This computer".into(),
                    base_url: DEFAULT_BASE_URL.into(),
                    token_origin: None,
                }])
            }
            Err(source) => {
                return Err(ProfileError::Io {
                    path: self.path.display().to_string(),
                    source,
                })
            }
        };
        serde_json::from_str(&text).map_err(|source| ProfileError::Json {
            path: self.path.display().to_string(),
            source,
        })
    }

    fn save(&self, profiles: &[Profile]) -> Result<(), ProfileError> {
        let io = |source| ProfileError::Io {
            path: self.path.display().to_string(),
            source,
        };
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir).map_err(io)?;
        }
        let tmp = self.path.with_extension("json.tmp");
        {
            let mut f = fs::File::create(&tmp).map_err(io)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                f.set_permissions(fs::Permissions::from_mode(0o600))
                    .map_err(io)?;
            }
            let text = serde_json::to_string_pretty(profiles).expect("profiles serialize");
            f.write_all(text.as_bytes()).map_err(io)?;
            f.sync_all().map_err(io)?;
        }
        fs::rename(&tmp, &self.path).map_err(io)
    }

    /// The profiles for the launcher. A keychain that cannot be asked does not hide them:
    /// `has_token` is false and `keychain_error` says why.
    pub fn summaries(&self) -> Result<Vec<ProfileSummary>, ProfileError> {
        Ok(self
            .load()?
            .into_iter()
            .map(|p| {
                let (has_token, keychain_error) = match self.secrets.get(&p.id) {
                    Ok(t) => (t.is_some() && token_is_for(&p), None),
                    Err(e) => (false, Some(e.to_string())),
                };
                summary(p, has_token, keychain_error, None)
            })
            .collect())
    }

    pub fn get(&self, id: &str) -> Result<Profile, ProfileError> {
        self.load()?
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| ProfileError::NotFound(id.to_string()))
    }

    /// The token to send to the profile's address: only one saved for that very address.
    pub fn token(&self, id: &str) -> Result<Option<String>, ProfileError> {
        if !token_is_for(&self.get(id)?) {
            return Ok(None);
        }
        self.secrets.get(id)
    }

    pub fn upsert(&self, input: ProfileInput) -> Result<ProfileSummary, ProfileError> {
        let _guard = self.lock.lock().unwrap();
        let name = input.name.trim().to_string();
        if name.is_empty() {
            return Err(ProfileError::Invalid("give the profile a name".into()));
        }
        let base_url = normalize_base_url(&input.base_url)?;
        let mut profiles = self.load()?;
        if profiles
            .iter()
            .any(|p| p.name.eq_ignore_ascii_case(&name) && Some(&p.id) != input.id.as_ref())
        {
            return Err(ProfileError::Invalid(format!(
                "a profile named {name:?} already exists"
            )));
        }
        let (idx, address_changed) = match &input.id {
            Some(id) => {
                let idx = profiles
                    .iter()
                    .position(|p| &p.id == id)
                    .ok_or_else(|| ProfileError::NotFound(id.clone()))?;
                let changed = profiles[idx].base_url != base_url;
                profiles[idx].name = name;
                profiles[idx].base_url = base_url.clone();
                (idx, changed)
            }
            None => {
                profiles.push(Profile {
                    id: new_id(&profiles),
                    name,
                    base_url: base_url.clone(),
                    token_origin: None,
                });
                (profiles.len() - 1, false)
            }
        };
        let id = profiles[idx].id.clone();
        let mut notice = None;
        if let Some(token) = input
            .token
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
        {
            self.secrets.set(&id, token)?;
            profiles[idx].token_origin = Some(base_url);
        } else if input.clear_token {
            self.secrets.delete(&id)?;
            profiles[idx].token_origin = None;
        } else if address_changed && profiles[idx].token_origin.is_some() {
            // The token belongs to the old address: never send it to the new one. Even if the
            // keychain cannot delete it now, `token()` refuses it (its token_origin is gone).
            profiles[idx].token_origin = None;
            let _ = self.secrets.delete(&id);
            notice = Some(
                "The stored token was removed because the address changed. \
                 Enter the token for the new engine."
                    .to_string(),
            );
        }
        self.save(&profiles)?;
        let p = profiles.swap_remove(idx);
        let has_token = token_is_for(&p) && self.secrets.get(&p.id)?.is_some();
        Ok(summary(p, has_token, None, notice))
    }

    pub fn remove(&self, id: &str) -> Result<(), ProfileError> {
        let _guard = self.lock.lock().unwrap();
        let mut profiles = self.load()?;
        let before = profiles.len();
        profiles.retain(|p| p.id != id);
        if profiles.len() == before {
            return Err(ProfileError::NotFound(id.to_string()));
        }
        self.secrets.delete(id)?;
        self.save(&profiles)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store(dir: &tempfile::TempDir) -> ProfileStore {
        ProfileStore::new(
            dir.path().join("profiles.json"),
            Box::<MemoryStore>::default(),
        )
    }

    #[test]
    fn base_urls_are_normalized_to_an_origin() {
        assert_eq!(
            normalize_base_url(" http://127.0.0.1:8080/ ").unwrap(),
            "http://127.0.0.1:8080"
        );
        assert_eq!(
            normalize_base_url("http://127.0.0.1:8080/ui/#/dashboard").unwrap(),
            "http://127.0.0.1:8080"
        );
        assert_eq!(
            normalize_base_url("https://opencap.example").unwrap(),
            "https://opencap.example"
        );
        assert_eq!(
            normalize_base_url("http://[::1]:8080").unwrap(),
            "http://[::1]:8080"
        );
        for bad in [
            "ftp://x",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://u:p@host",
            "http://h/other",
            "nope",
        ] {
            assert!(normalize_base_url(bad).is_err(), "{bad} should be refused");
        }
    }

    #[test]
    fn first_start_has_a_local_profile() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(&dir);
        let all = s.summaries().unwrap();
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].base_url, DEFAULT_BASE_URL);
        assert!(!all[0].has_token);
    }

    #[test]
    fn tokens_go_to_the_secret_store_never_to_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(&dir);
        let p = s
            .upsert(ProfileInput {
                name: "Plant A".into(),
                base_url: "http://192.0.2.10:8080".into(),
                token: Some("s3cret-token".into()),
                ..Default::default()
            })
            .unwrap();
        assert!(p.has_token);
        let file = fs::read_to_string(dir.path().join("profiles.json")).unwrap();
        assert!(
            !file.contains("s3cret-token"),
            "token leaked into profiles.json"
        );
        assert!(file.contains("Plant A"));
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("s3cret-token"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(dir.path().join("profiles.json"))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600);
        }
    }

    #[test]
    fn update_keeps_or_clears_the_token_and_remove_deletes_it() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(&dir);
        let p = s
            .upsert(ProfileInput {
                name: "A".into(),
                base_url: "http://192.0.2.1:8080".into(),
                token: Some("t1".into()),
                ..Default::default()
            })
            .unwrap();
        let p2 = s
            .upsert(ProfileInput {
                id: Some(p.id.clone()),
                name: "A2".into(),
                base_url: "http://192.0.2.2:8080".into(),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(p2.name, "A2");
        // the address changed and no new token was given: the old engine's token is gone
        assert_eq!(s.token(&p.id).unwrap(), None);
        assert!(!p2.has_token);
        assert!(p2.notice.as_deref().unwrap().contains("address changed"));
        s.upsert(ProfileInput {
            id: Some(p.id.clone()),
            name: "A2".into(),
            base_url: "http://192.0.2.2:8080".into(),
            token: Some("t1b".into()),
            ..Default::default()
        })
        .unwrap();
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("t1b"));
        s.upsert(ProfileInput {
            id: Some(p.id.clone()),
            name: "A2".into(),
            base_url: "http://192.0.2.2:8080".into(),
            clear_token: true,
            ..Default::default()
        })
        .unwrap();
        assert_eq!(s.token(&p.id).unwrap(), None);
        s.upsert(ProfileInput {
            id: Some(p.id.clone()),
            name: "A2".into(),
            base_url: "http://192.0.2.2:8080".into(),
            token: Some("t2".into()),
            ..Default::default()
        })
        .unwrap();
        s.remove(&p.id).unwrap();
        assert_eq!(
            s.secrets.get(&p.id).unwrap(),
            None,
            "remove deletes the keychain entry"
        );
        assert!(matches!(s.get(&p.id), Err(ProfileError::NotFound(_))));
        assert!(matches!(s.token(&p.id), Err(ProfileError::NotFound(_))));
    }

    #[test]
    fn a_token_never_follows_an_address_change() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(&dir);
        let save = |id: Option<String>, name: &str, url: &str, token: Option<&str>| {
            s.upsert(ProfileInput {
                id,
                name: name.into(),
                base_url: url.into(),
                token: token.map(String::from),
                ..Default::default()
            })
            .unwrap()
        };
        let p = save(None, "A", "http://192.0.2.1:8080", Some("engine-a-token"));
        // renaming, or re-entering the same origin in another spelling, keeps the token
        let same = save(
            Some(p.id.clone()),
            "A renamed",
            "http://192.0.2.1:8080/ui/",
            None,
        );
        assert!(same.has_token && same.notice.is_none());
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("engine-a-token"));
        // changing only the address removes it, and says so
        let moved = save(
            Some(p.id.clone()),
            "A renamed",
            "http://192.0.2.99:8080",
            None,
        );
        assert!(!moved.has_token);
        assert!(moved.notice.is_some());
        assert_eq!(s.token(&p.id).unwrap(), None);
        // changing the address and giving a new token stores the new token for the new address
        let next = save(
            Some(p.id.clone()),
            "A renamed",
            "http://192.0.2.50:8080",
            Some("engine-b-token"),
        );
        assert!(next.has_token && next.notice.is_none());
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("engine-b-token"));
    }

    struct UndeletableKeychain(MemoryStore);
    impl SecretStore for UndeletableKeychain {
        fn get(&self, id: &str) -> Result<Option<String>, ProfileError> {
            self.0.get(id)
        }
        fn set(&self, id: &str, token: &str) -> Result<(), ProfileError> {
            self.0.set(id, token)
        }
        fn delete(&self, _: &str) -> Result<(), ProfileError> {
            Err(ProfileError::Keychain("keychain locked".into()))
        }
    }

    #[test]
    fn a_token_the_keychain_cannot_delete_is_still_never_sent_elsewhere() {
        let dir = tempfile::tempdir().unwrap();
        let s = ProfileStore::new(
            dir.path().join("profiles.json"),
            Box::new(UndeletableKeychain(MemoryStore::default())),
        );
        let p = s
            .upsert(ProfileInput {
                name: "A".into(),
                base_url: "http://192.0.2.1:8080".into(),
                token: Some("engine-a-token".into()),
                ..Default::default()
            })
            .unwrap();
        let moved = s
            .upsert(ProfileInput {
                id: Some(p.id.clone()),
                name: "A".into(),
                base_url: "http://192.0.2.99:8080".into(),
                ..Default::default()
            })
            .unwrap();
        assert!(!moved.has_token);
        assert_eq!(s.token(&p.id).unwrap(), None);
        // the secret is still in the keychain; only the address binding decides
        let path = dir.path().join("profiles.json");
        let write = |base: &str, origin: &str| {
            let doc = serde_json::json!([{"id": p.id, "name": "A", "base_url": base, "token_origin": origin}]);
            fs::write(&path, doc.to_string()).unwrap();
        };
        // profiles.json edited by hand to point elsewhere: the token is not sent there
        write("http://192.0.2.99:8080", "http://192.0.2.1:8080");
        assert_eq!(s.token(&p.id).unwrap(), None);
        write("http://192.0.2.1:8080", "http://192.0.2.1:8080");
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("engine-a-token"));
    }

    #[test]
    fn cleartext_warning_only_for_http_to_another_host() {
        assert!(is_cleartext_remote("http://192.0.2.10:8080"));
        assert!(is_cleartext_remote("http://opencap.example"));
        assert!(!is_cleartext_remote("https://192.0.2.10"));
        assert!(!is_cleartext_remote("http://127.0.0.1:8080"));
        assert!(!is_cleartext_remote("http://localhost:8080"));
        assert!(!is_cleartext_remote("http://[::1]:8080"));
    }

    struct BrokenKeychain;
    impl SecretStore for BrokenKeychain {
        fn get(&self, _: &str) -> Result<Option<String>, ProfileError> {
            Err(ProfileError::Keychain("no Secret Service running".into()))
        }
        fn set(&self, _: &str, _: &str) -> Result<(), ProfileError> {
            Err(ProfileError::Keychain("no Secret Service running".into()))
        }
        fn delete(&self, _: &str) -> Result<(), ProfileError> {
            Ok(())
        }
    }

    #[test]
    fn an_unavailable_keychain_does_not_hide_profiles_but_refuses_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let s = ProfileStore::new(dir.path().join("profiles.json"), Box::new(BrokenKeychain));
        let all = s.summaries().unwrap();
        assert_eq!(all.len(), 1);
        assert!(!all[0].has_token);
        assert!(all[0]
            .keychain_error
            .as_deref()
            .unwrap()
            .contains("Secret Service"));
        let r = s.upsert(ProfileInput {
            name: "A".into(),
            base_url: "http://192.0.2.1".into(),
            token: Some("t".into()),
            ..Default::default()
        });
        assert!(
            matches!(r, Err(ProfileError::Keychain(_))),
            "a token must never be dropped silently"
        );
    }

    #[test]
    fn names_are_unique_and_inputs_validated() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(&dir);
        s.upsert(ProfileInput {
            name: "Plant A".into(),
            base_url: "http://192.0.2.1".into(),
            ..Default::default()
        })
        .unwrap();
        let dup = s.upsert(ProfileInput {
            name: "plant a".into(),
            base_url: "http://192.0.2.2".into(),
            ..Default::default()
        });
        assert!(matches!(dup, Err(ProfileError::Invalid(_))));
        let empty = s.upsert(ProfileInput {
            name: " ".into(),
            base_url: "http://192.0.2.2".into(),
            ..Default::default()
        });
        assert!(matches!(empty, Err(ProfileError::Invalid(_))));
        let missing = s.upsert(ProfileInput {
            id: Some("nope".into()),
            name: "X".into(),
            base_url: "http://192.0.2.2".into(),
            ..Default::default()
        });
        assert!(matches!(missing, Err(ProfileError::NotFound(_))));
    }
}
