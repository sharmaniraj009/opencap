//! `opencap-engine healthcheck`: the container's HEALTHCHECK without curl or Python. One HTTP/1.0
//! GET of `$OPENCAP_HEALTH_URL` (default `http://127.0.0.1:8080/health/live`) with short
//! timeouts; exit status 0 for a 200, else 1 with the reason on stderr.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use anyhow::Result;

const DEFAULT_URL: &str = "http://127.0.0.1:8080/health/live";
const TIMEOUT: Duration = Duration::from_secs(4);

/// `http://host:port/path` -> (`host:port`, `/path`). Only plain http: the check runs inside the
/// container, against the engine's own listener.
fn split(url: &str) -> Result<(String, String), String> {
    let rest = url.strip_prefix("http://").ok_or_else(|| format!("{url}: only http:// URLs are supported"))?;
    let (authority, path) = match rest.split_once('/') {
        Some((a, p)) => (a, format!("/{p}")),
        None => (rest, "/".to_string()),
    };
    let authority = if authority.contains(':') && !authority.starts_with('[')
        || authority.starts_with('[') && authority.contains("]:")
    {
        authority.to_string()
    } else {
        format!("{authority}:80")
    };
    Ok((authority, path))
}

/// The status code of the answer to `GET url`.
pub fn status_of(url: &str) -> Result<u16, String> {
    let (authority, path) = split(url)?;
    let addr = authority
        .to_socket_addrs()
        .map_err(|e| format!("{authority}: {e}"))?
        .next()
        .ok_or_else(|| format!("{authority}: no address"))?;
    let mut sock = TcpStream::connect_timeout(&addr, TIMEOUT).map_err(|e| format!("{authority}: {e}"))?;
    sock.set_read_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    sock.set_write_timeout(Some(TIMEOUT)).map_err(|e| e.to_string())?;
    write!(sock, "GET {path} HTTP/1.0\r\nHost: {authority}\r\nConnection: close\r\n\r\n").map_err(|e| e.to_string())?;
    let mut head = [0u8; 32];
    let mut got = 0;
    while got < head.len() {
        match sock.read(&mut head[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) => return Err(format!("{authority}: {e}")),
        }
    }
    let line = String::from_utf8_lossy(&head[..got]);
    line.split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or_else(|| format!("{authority}: not an HTTP answer: {line:?}"))
}

pub fn run() -> Result<i32> {
    let url = std::env::var("OPENCAP_HEALTH_URL").unwrap_or_else(|_| DEFAULT_URL.to_string());
    match status_of(&url) {
        Ok(200) => Ok(0),
        Ok(code) => {
            eprintln!("{url}: HTTP {code}");
            Ok(1)
        }
        Err(e) => {
            eprintln!("{e}");
            Ok(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn serve_once(status: &'static str) -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 512];
            let _ = s.read(&mut buf);
            let _ = write!(s, "HTTP/1.1 {status}\r\ncontent-length: 2\r\n\r\n{{}}");
        });
        format!("http://127.0.0.1:{port}/health/live")
    }

    #[test]
    fn a_200_is_healthy_and_anything_else_is_not() {
        assert_eq!(status_of(&serve_once("200 OK")), Ok(200));
        assert_eq!(status_of(&serve_once("503 Service Unavailable")), Ok(503));
    }

    #[test]
    fn a_closed_port_and_a_bad_url_are_errors() {
        let free = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert!(status_of(&format!("http://127.0.0.1:{free}/")).is_err());
        assert!(status_of("https://127.0.0.1/").unwrap_err().contains("only http://"));
        assert_eq!(split("http://localhost/x/y").unwrap(), ("localhost:80".to_string(), "/x/y".to_string()));
        assert_eq!(split("http://[::1]:8080").unwrap(), ("[::1]:8080".to_string(), "/".to_string()));
    }
}
