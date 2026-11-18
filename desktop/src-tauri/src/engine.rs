//! Talk to an opencap engine: is it up, what does it report, does the stored token work?
//!
//! Runs in Rust (not in the launcher page), so the engine needs no CORS settings for it.
//! Works with the Python engine (`/health`, `/api/v1/auth-info`, `/api/v1/whoami`) and with
//! engines without the configuration API (falls back to `GET /channels` to test a token).

use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

const TIMEOUT: Duration = Duration::from_secs(4);

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct DeviceCounts {
    pub ok: u64,
    pub offline: u64,
    pub demoted: u64,
}

/// Whether the stored token is accepted.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Auth {
    /// The token works: its name and role.
    Ok { name: String, role: String },
    /// The engine has no token configured (read-only on localhost).
    Open,
    /// The engine needs a token and none is stored.
    TokenRequired,
    /// The stored token was refused.
    BadToken,
    /// Could not tell (unexpected answer).
    Unknown { detail: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EngineStatus {
    pub base_url: String,
    pub version: Option<String>,
    pub instance: Option<String>,
    pub mqtt_connected: Option<bool>,
    pub devices: DeviceCounts,
    pub auth: Auth,
    /// The engine serves the web UI at /ui/.
    pub has_ui: bool,
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(TIMEOUT)
        .redirects(0)
        .build()
}

fn get(
    agent: &ureq::Agent,
    url: &str,
    token: Option<&str>,
) -> Result<(u16, Option<Value>), String> {
    let mut req = agent.get(url).set("X-Opencap-Client", "desktop");
    if let Some(t) = token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    match req.call() {
        Ok(resp) => {
            let status = resp.status();
            Ok((status, resp.into_json::<Value>().ok()))
        }
        Err(ureq::Error::Status(code, resp)) => Ok((code, resp.into_json::<Value>().ok())),
        Err(ureq::Error::Transport(t)) => Err(t.to_string()),
    }
}

/// Ask the engine at `base_url` for its health and test `token`.
pub fn check(base_url: &str, token: Option<&str>) -> Result<EngineStatus, String> {
    let agent = agent();
    let (code, health) = get(&agent, &format!("{base_url}/health"), None).map_err(|e| {
        format!("cannot reach {base_url}: {e}. Is the engine running, and is the address right?")
    })?;
    if code != 200 {
        return Err(format!(
            "{base_url}/health answered HTTP {code}: is this an opencap engine?"
        ));
    }
    let health = health.ok_or_else(|| {
        format!("{base_url}/health did not answer JSON: is this an opencap engine?")
    })?;
    let str_of = |k: &str| health.get(k).and_then(Value::as_str).map(str::to_string);
    let count = |k: &str| {
        health
            .get("devices")
            .and_then(|d| d.get(k))
            .and_then(Value::as_u64)
            .unwrap_or(0)
    };

    let open = match get(&agent, &format!("{base_url}/api/v1/auth-info"), None) {
        Ok((200, Some(v))) => v.get("open").and_then(Value::as_bool),
        _ => None, // an engine without the configuration API
    };
    let auth = match (open, token) {
        (Some(true), _) => Auth::Open,
        (_, None) => Auth::TokenRequired,
        (Some(false), Some(t)) => {
            match get(&agent, &format!("{base_url}/api/v1/whoami"), Some(t)) {
                Ok((200, Some(me))) => Auth::Ok {
                    name: me
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("?")
                        .to_string(),
                    role: me
                        .get("role")
                        .and_then(Value::as_str)
                        .unwrap_or("?")
                        .to_string(),
                },
                Ok((401, _)) => Auth::BadToken,
                Ok((code, _)) => Auth::Unknown {
                    detail: format!("/api/v1/whoami answered HTTP {code}"),
                },
                Err(e) => Auth::Unknown { detail: e },
            }
        }
        (None, Some(t)) => match get(&agent, &format!("{base_url}/channels"), Some(t)) {
            Ok((200, _)) => Auth::Ok {
                name: "?".into(),
                role: "?".into(),
            },
            Ok((401, _)) => Auth::BadToken,
            Ok((code, _)) => Auth::Unknown {
                detail: format!("/channels answered HTTP {code}"),
            },
            Err(e) => Auth::Unknown { detail: e },
        },
    };
    let has_ui = matches!(get(&agent, &format!("{base_url}/ui/"), None), Ok((200, _)));
    Ok(EngineStatus {
        base_url: base_url.to_string(),
        version: str_of("version"),
        instance: str_of("instance"),
        mqtt_connected: health.get("mqtt_connected").and_then(Value::as_bool),
        devices: DeviceCounts {
            ok: count("ok"),
            offline: count("offline"),
            demoted: count("demoted"),
        },
        auth,
        has_ui,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;
    use std::thread;

    /// A tiny HTTP server: `routes` maps "METHOD path [token]" to (status, body).
    fn serve(routes: Vec<(&'static str, u16, &'static str)>) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut first = String::new();
                if reader.read_line(&mut first).is_err() {
                    continue;
                }
                let mut auth = String::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if line.to_ascii_lowercase().starts_with("authorization:") {
                        auth = line.split_once(':').unwrap().1.trim().to_string();
                    }
                }
                let path = first.split_whitespace().nth(1).unwrap_or("/").to_string();
                let key = if auth.is_empty() {
                    path.clone()
                } else {
                    format!("{path} {auth}")
                };
                let (status, body) = routes
                    .iter()
                    .find(|(k, _, _)| *k == key || (*k == path && !k.contains(' ')))
                    .map(|(_, s, b)| (*s, *b))
                    .unwrap_or((404, "{\"detail\": \"Not Found\"}"));
                let resp = format!(
                    "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
            }
        });
        format!("http://{addr}")
    }

    const HEALTH: &str = r#"{"status":"ok","version":"opencap-0.1.0","instance":"plant-a","mqtt_connected":true,"devices":{"ok":3,"offline":1,"demoted":2}}"#;

    #[test]
    fn healthy_engine_with_a_good_token() {
        let base = serve(vec![
            ("/health", 200, HEALTH),
            ("/api/v1/auth-info", 200, r#"{"open": false}"#),
            (
                "/api/v1/whoami Bearer good",
                200,
                r#"{"name":"alice","role":"admin","open":false}"#,
            ),
            (
                "/api/v1/whoami",
                401,
                r#"{"detail":"missing or bad bearer token"}"#,
            ),
            ("/ui/", 200, "{}"),
        ]);
        let st = check(&base, Some("good")).unwrap();
        assert_eq!(st.version.as_deref(), Some("opencap-0.1.0"));
        assert_eq!(
            st.devices,
            DeviceCounts {
                ok: 3,
                offline: 1,
                demoted: 2
            }
        );
        assert_eq!(
            st.auth,
            Auth::Ok {
                name: "alice".into(),
                role: "admin".into()
            }
        );
        assert!(st.has_ui);
        assert_eq!(check(&base, Some("wrong")).unwrap().auth, Auth::BadToken);
        assert_eq!(check(&base, None).unwrap().auth, Auth::TokenRequired);
    }

    #[test]
    fn open_engine_and_engine_without_config_api() {
        let open = serve(vec![
            ("/health", 200, HEALTH),
            ("/api/v1/auth-info", 200, r#"{"open": true}"#),
        ]);
        let st = check(&open, None).unwrap();
        assert_eq!(st.auth, Auth::Open);
        assert!(!st.has_ui);
        let legacy = serve(vec![
            ("/health", 200, HEALTH),
            ("/channels Bearer good", 200, "[]"),
            (
                "/channels",
                401,
                r#"{"detail":"missing or bad bearer token"}"#,
            ),
        ]);
        assert_eq!(
            check(&legacy, Some("good")).unwrap().auth,
            Auth::Ok {
                name: "?".into(),
                role: "?".into()
            }
        );
        assert_eq!(check(&legacy, Some("bad")).unwrap().auth, Auth::BadToken);
    }

    #[test]
    fn unreachable_or_foreign_servers_give_clear_errors() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener); // nothing listens there now
        let err = check(&format!("http://127.0.0.1:{port}"), None).unwrap_err();
        assert!(err.contains("cannot reach"), "{err}");
        let foreign = serve(vec![("/health", 404, "not here")]);
        assert!(check(&foreign, None)
            .unwrap_err()
            .contains("is this an opencap engine"));
    }
}
