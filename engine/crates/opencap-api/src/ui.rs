//! The web UI: a static bundle (`engine/ui/`, ADR-0010, copied from the Python engine's
//! `src/opencap/api/ui/`) compiled into the binary, so a single executable serves it. It uses
//! only `/api/v1`. No inline script or style (Alpine.js runs as its CSP build).

use axum::http::{header, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};

struct File {
    path: &'static str,
    mime: &'static str,
    bytes: &'static [u8],
}

macro_rules! file {
    ($path:literal, $mime:literal) => {
        File { path: $path, mime: $mime, bytes: include_bytes!(concat!("../../../ui/", $path)) }
    };
}

static FILES: &[File] = &[
    file!("index.html", "text/html; charset=utf-8"),
    file!("app.js", "text/javascript; charset=utf-8"),
    file!("app.css", "text/css; charset=utf-8"),
    file!("i18n/en.json", "application/json"),
    file!("i18n/languages.json", "application/json"),
    file!("vendor/alpine-csp-3.17.5.min.js", "text/javascript; charset=utf-8"),
    file!("vendor/alpine-LICENSE.md", "text/markdown; charset=utf-8"),
];

/// On every file of the bundle (Python `UI_HEADERS`).
const SECURITY_HEADERS: [(&str, &str); 4] = [
    (
        "content-security-policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; \
         form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    ),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "same-origin"),
];

/// `/ui` and `/` go to the bundle's index.
pub fn to_ui() -> Response {
    Redirect::temporary("/ui/").into_response()
}

/// One file of the bundle by its path below `/ui/` (empty = the index).
pub fn serve(path: &str) -> Response {
    let wanted = if path.is_empty() { "index.html" } else { path };
    let Some(file) = FILES.iter().find(|f| f.path == wanted) else {
        return (StatusCode::NOT_FOUND, axum::Json(serde_json::json!({"detail": "Not Found"}))).into_response();
    };
    let mut resp = (StatusCode::OK, file.bytes).into_response();
    let h = resp.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(file.mime));
    for (k, v) in SECURITY_HEADERS {
        h.insert(HeaderName::from_static(k), HeaderValue::from_static(v));
    }
    if wanted == "index.html" {
        h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    }
    resp
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_bundle_is_everything_under_engine_ui() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../ui");
        let mut on_disk = Vec::new();
        fn walk(dir: &std::path::Path, base: &std::path::Path, out: &mut Vec<String>) {
            for e in std::fs::read_dir(dir).unwrap() {
                let p = e.unwrap().path();
                if p.is_dir() {
                    walk(&p, base, out);
                } else {
                    out.push(p.strip_prefix(base).unwrap().to_string_lossy().replace('\\', "/"));
                }
            }
        }
        walk(&root, &root, &mut on_disk);
        on_disk.sort();
        let mut listed: Vec<&str> = FILES.iter().map(|f| f.path).collect();
        listed.sort_unstable();
        assert_eq!(on_disk, listed, "a file under engine/ui is missing from FILES (or the reverse)");
    }
}
