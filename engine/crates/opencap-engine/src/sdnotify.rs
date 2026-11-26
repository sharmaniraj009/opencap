//! systemd readiness notifications for `Type=notify` units (#57, `deploy/opencapd.service`).
//!
//! The sd_notify protocol is one datagram of `KEY=value` lines to the unix socket named in
//! `$NOTIFY_SOCKET` (a path, or `@name` for Linux's abstract namespace). Speaking it directly
//! avoids linking libsystemd. Without `$NOTIFY_SOCKET` (not started by systemd, or not
//! `Type=notify`) every call is a no-op.

use std::io;
use std::path::Path;

/// Send `state` (e.g. `"READY=1"`) to `$NOTIFY_SOCKET`, if set. A failure is logged, never fatal:
/// the engine works the same without systemd.
pub fn notify(state: &str) {
    if let Some(socket) = std::env::var_os("NOTIFY_SOCKET") {
        if let Err(e) = notify_to(Path::new(&socket), state) {
            tracing::warn!(error = %e, state, "sd_notify failed");
        }
    }
}

/// Send `state` to the notification socket `socket`.
pub fn notify_to(socket: &Path, state: &str) -> io::Result<()> {
    use std::os::unix::net::UnixDatagram;
    let sock = UnixDatagram::unbound()?;
    let name = socket.as_os_str().as_encoded_bytes();
    if let Some(abstract_name) = name.strip_prefix(b"@") {
        return send_abstract(&sock, abstract_name, state);
    }
    sock.send_to(state.as_bytes(), socket).map(|_| ())
}

#[cfg(target_os = "linux")]
fn send_abstract(sock: &std::os::unix::net::UnixDatagram, name: &[u8], state: &str) -> io::Result<()> {
    use std::os::linux::net::SocketAddrExt;
    let addr = std::os::unix::net::SocketAddr::from_abstract_name(name)?;
    sock.send_to_addr(state.as_bytes(), &addr).map(|_| ())
}

#[cfg(not(target_os = "linux"))]
fn send_abstract(_: &std::os::unix::net::UnixDatagram, _: &[u8], _: &str) -> io::Result<()> {
    Err(io::Error::new(io::ErrorKind::Unsupported, "abstract unix sockets exist on Linux only"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixDatagram;

    fn recv(sock: &UnixDatagram) -> String {
        let mut buf = [0u8; 256];
        let n = sock.recv(&mut buf).expect("a datagram");
        String::from_utf8_lossy(&buf[..n]).into_owned()
    }

    #[test]
    fn sends_to_a_socket_path() {
        let dir = std::env::temp_dir().join(format!("opencap-sdnotify-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("notify.sock");
        let _ = std::fs::remove_file(&path);
        let server = UnixDatagram::bind(&path).unwrap();
        notify_to(&path, "READY=1").unwrap();
        assert_eq!(recv(&server), "READY=1");
        notify_to(&path, "RELOADING=1\nSTATUS=reloading").unwrap();
        assert_eq!(recv(&server), "RELOADING=1\nSTATUS=reloading");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn sends_to_an_abstract_socket() {
        use std::os::linux::net::SocketAddrExt;
        let name = format!("opencap-sdnotify-test-{}", std::process::id());
        let addr = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()).unwrap();
        let server = UnixDatagram::bind_addr(&addr).unwrap();
        notify_to(Path::new(&format!("@{name}")), "STOPPING=1").unwrap();
        assert_eq!(recv(&server), "STOPPING=1");
    }

    #[test]
    fn a_missing_socket_is_an_error_not_a_panic() {
        assert!(notify_to(Path::new("/nonexistent/opencap/notify.sock"), "READY=1").is_err());
    }
}
