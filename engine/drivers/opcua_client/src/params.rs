//! `Device.params` of an `opcua_client` device (Python `OpcUaParams`). Secrets are never params:
//! only the NAMES of environment variables.

use serde_json::{Map, Value as Json};

use opcua::crypto::SecurityPolicy;
use opcua::types::MessageSecurityMode;

use opencap_core::config::Device;
use opencap_core::pyfmt::py_repr_json;

pub const SECRET_KEYS: [(&str, &str); 5] = [
    ("username", "username_env"),
    ("user", "username_env"),
    ("password", "password_env"),
    ("passwd", "password_env"),
    ("private_key_password", "private_key_password_env"),
];

#[derive(Debug, Clone, PartialEq)]
pub struct OpcUaParams {
    pub endpoint: String,
    pub policy: SecurityPolicy,
    pub mode: MessageSecurityMode,
    pub username_env: Option<String>,
    pub password_env: Option<String>,
    pub certificate: Option<String>,
    pub private_key: Option<String>,
    /// client certificate store (own/, trusted/, rejected/); defaults to a folder under the temp directory
    pub pki_dir: Option<String>,
    /// accept a server certificate that is not in `trusted/` (off: fail closed)
    pub trust_server_certs: bool,
    pub application_uri: String,
    /// nodes per Read request (and per block)
    pub max_items: usize,
    pub session_timeout_ms: u32,
}

fn policy_of(text: &str) -> Option<SecurityPolicy> {
    Some(match text.to_lowercase().replace('_', "").as_str() {
        "none" => SecurityPolicy::None,
        "basic256sha256" => SecurityPolicy::Basic256Sha256,
        "aes128sha256rsaoaep" => SecurityPolicy::Aes128Sha256RsaOaep,
        "aes256sha256rsapss" => SecurityPolicy::Aes256Sha256RsaPss,
        _ => return None,
    })
}

fn string(p: &Map<String, Json>, key: &str) -> Result<Option<String>, String> {
    match p.get(key) {
        None | Some(Json::Null) => Ok(None),
        Some(Json::String(s)) if !s.is_empty() => Ok(Some(s.clone())),
        Some(v) => Err(format!("opcua_client param {key}: expected a non-empty string, got {}", py_repr_json(v))),
    }
}

fn int(p: &Map<String, Json>, key: &str, default: i64, min: i64) -> Result<i64, String> {
    match p.get(key) {
        None | Some(Json::Null) => Ok(default),
        Some(Json::Number(n)) if n.as_i64().is_some() => {
            let v = n.as_i64().unwrap_or(default);
            if v < min {
                Err(format!("opcua_client param {key}={v}: must be >= {min}"))
            } else {
                Ok(v)
            }
        }
        Some(v) => Err(format!("opcua_client param {key}={}: expected an integer", py_repr_json(v))),
    }
}

impl OpcUaParams {
    pub fn from_device(device: &Device) -> Result<Self, String> {
        let raw = &device.params;
        for (key, env_key) in SECRET_KEYS {
            if raw.contains_key(key) {
                return Err(format!(
                    "{}: opcua_client param '{key}' is not allowed: put the secret in an environment variable and name it in {env_key}",
                    device.id
                ));
            }
        }
        let endpoint = string(raw, "endpoint")?.unwrap_or_else(|| format!("opc.tcp://{}:{}", device.host, device.port));
        if !endpoint.starts_with("opc.tcp://") {
            return Err(format!(
                "{}: opcua_client param endpoint={}: must start with opc.tcp://",
                device.id,
                py_repr_json(&Json::String(endpoint))
            ));
        }
        let policy = match string(raw, "security_policy")? {
            None => SecurityPolicy::None,
            Some(t) => policy_of(&t).ok_or_else(|| {
                format!("{}: opcua_client param security_policy={}: expected None, Basic256Sha256, Aes128_Sha256_RsaOaep or Aes256_Sha256_RsaPss", device.id, py_repr_json(&Json::String(t.clone())))
            })?,
        };
        let mode = match string(raw, "message_mode")?.map(|m| m.to_lowercase()).as_deref() {
            None | Some("none") => MessageSecurityMode::None,
            Some("sign") => MessageSecurityMode::Sign,
            Some("signandencrypt") => MessageSecurityMode::SignAndEncrypt,
            Some(other) => {
                return Err(format!(
                    "{}: opcua_client param message_mode={}: expected None, Sign or SignAndEncrypt",
                    device.id,
                    py_repr_json(&Json::String(other.into()))
                ))
            }
        };
        if policy == SecurityPolicy::None && mode != MessageSecurityMode::None {
            return Err(format!(
                "{}: opcua_client message_mode {mode:?} needs a security_policy other than None",
                device.id
            ));
        }
        if policy != SecurityPolicy::None && mode == MessageSecurityMode::None {
            return Err(format!(
                "{}: opcua_client security_policy {policy:?} needs message_mode Sign or SignAndEncrypt",
                device.id
            ));
        }
        let certificate = string(raw, "certificate")?;
        let private_key = string(raw, "private_key")?;
        if policy != SecurityPolicy::None && (certificate.is_none() != private_key.is_none()) {
            return Err(format!("{}: opcua_client params certificate and private_key go together", device.id));
        }
        let trust = match raw.get("trust_server_certs") {
            None | Some(Json::Null) => false,
            Some(Json::Bool(b)) => *b,
            Some(v) => {
                return Err(format!(
                    "opcua_client param trust_server_certs={}: expected true or false",
                    py_repr_json(v)
                ))
            }
        };
        Ok(Self {
            endpoint,
            policy,
            mode,
            username_env: string(raw, "username_env")?,
            password_env: string(raw, "password_env")?,
            certificate,
            private_key,
            pki_dir: string(raw, "pki_dir")?,
            trust_server_certs: trust,
            application_uri: string(raw, "application_uri")?.unwrap_or_else(|| "urn:opencap:client".into()),
            max_items: int(raw, "max_items", 100, 1)? as usize,
            session_timeout_ms: int(raw, "session_timeout_ms", 60_000, 1000)? as u32,
        })
    }
}
