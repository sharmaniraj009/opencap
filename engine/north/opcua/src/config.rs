//! `poller.yaml` `opcua_server` as async-opcua settings: endpoint, security policies, users.

use opcua::crypto::SecurityPolicy;
use opcua::types::MessageSecurityMode;

use opencap_core::config::OpcUaServerCfg;

pub const USER_ANONYMOUS: &str = "ANONYMOUS";

/// (policy, mode) of a configured policy name (`OPCUA_SERVER_POLICIES`).
pub fn policy_of(name: &str) -> Option<(SecurityPolicy, MessageSecurityMode)> {
    use MessageSecurityMode::{Sign, SignAndEncrypt};
    Some(match name {
        "None" => (SecurityPolicy::None, MessageSecurityMode::None),
        "Basic256Sha256_Sign" => (SecurityPolicy::Basic256Sha256, Sign),
        "Basic256Sha256_SignAndEncrypt" => (SecurityPolicy::Basic256Sha256, SignAndEncrypt),
        "Aes128_Sha256_RsaOaep_Sign" => (SecurityPolicy::Aes128Sha256RsaOaep, Sign),
        "Aes128_Sha256_RsaOaep_SignAndEncrypt" => (SecurityPolicy::Aes128Sha256RsaOaep, SignAndEncrypt),
        "Aes256_Sha256_RsaPss_Sign" => (SecurityPolicy::Aes256Sha256RsaPss, Sign),
        "Aes256_Sha256_RsaPss_SignAndEncrypt" => (SecurityPolicy::Aes256Sha256RsaPss, SignAndEncrypt),
        _ => return None,
    })
}

/// `(host, port, path)` of an `opc.tcp://host:port/path` URL.
pub fn split_endpoint(url: &str) -> Result<(String, u16, String), String> {
    let rest = url.strip_prefix("opc.tcp://").ok_or_else(|| format!("endpoint {url:?} must start with opc.tcp://"))?;
    let (hostport, path) = match rest.split_once('/') {
        Some((hp, p)) => (hp, format!("/{p}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().map_err(|_| format!("endpoint {url:?}: bad port {p:?}"))?),
        None => (hostport.to_string(), 4840),
    };
    Ok((host, port, path))
}

/// The cfg's users as `(username, password)` read from the environment variable names. A missing
/// variable is an error: a server that silently drops a user would lock operators out.
pub fn users_of(cfg: &OpcUaServerCfg, env: &dyn Fn(&str) -> Option<String>) -> Result<Vec<(String, String)>, String> {
    let mut pairs: Vec<(&str, &str)> = Vec::new();
    if let (Some(u), Some(p)) = (cfg.username_env.as_deref(), cfg.password_env.as_deref()) {
        pairs.push((u, p));
    }
    for u in &cfg.users {
        pairs.push((&u.username_env, &u.password_env));
    }
    pairs
        .into_iter()
        .map(|(ue, pe)| {
            let user = env(ue)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| format!("opcua_server: environment variable {ue} is not set"))?;
            let pass = env(pe)
                .filter(|s| !s.is_empty())
                .ok_or_else(|| format!("opcua_server: environment variable {pe} is not set"))?;
            Ok((user, pass))
        })
        .collect()
}
