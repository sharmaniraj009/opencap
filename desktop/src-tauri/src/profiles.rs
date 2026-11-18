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
                    Ok(t) => (t.is_some(), None),
                    Err(e) => (false, Some(e.to_string())),
                };
                ProfileSummary {
                    id: p.id,
                    name: p.name,
                    base_url: p.base_url,
                    has_token,
                    keychain_error,
                }
            })
            .collect())
    }

    pub fn get(&self, id: &str) -> Result<Profile, ProfileError> {
        self.load()?
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| ProfileError::NotFound(id.to_string()))
    }

    pub fn token(&self, id: &str) -> Result<Option<String>, ProfileError> {
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
        let id = match &input.id {
            Some(id) => {
                let p = profiles
                    .iter_mut()
                    .find(|p| &p.id == id)
                    .ok_or_else(|| ProfileError::NotFound(id.clone()))?;
                p.name = name;
                p.base_url = base_url;
                id.clone()
            }
            None => {
                let id = new_id(&profiles);
                profiles.push(Profile {
                    id: id.clone(),
                    name,
                    base_url,
                });
                id
            }
        };
        if let Some(token) = input
            .token
            .as_deref()
            .map(str::trim)
            .filter(|t| !t.is_empty())
        {
            self.secrets.set(&id, token)?;
        } else if input.clear_token {
            self.secrets.delete(&id)?;
        }
        self.save(&profiles)?;
        let p = profiles
            .into_iter()
            .find(|p| p.id == id)
            .expect("just saved");
        let has_token = self.secrets.get(&p.id)?.is_some();
        Ok(ProfileSummary {
            id: p.id,
            name: p.name,
            base_url: p.base_url,
            has_token,
            keychain_error: None,
        })
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
        assert_eq!(s.token(&p.id).unwrap().as_deref(), Some("t1"));
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
        assert_eq!(s.token(&p.id).unwrap(), None);
        assert!(matches!(s.get(&p.id), Err(ProfileError::NotFound(_))));
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
