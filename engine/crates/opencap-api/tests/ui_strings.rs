//! Discipline of the web UI bundle (`engine/ui/`), ported from the Python engine's `tests/unit/test_ui.py`
//! (which only checks the old copy): every visible string comes from `i18n/en.json`, every key the code uses
//! is in the table and none is left over, and nothing in the bundle offers to write to a PLC (ADR-0008).

use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

fn ui_file(name: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../ui").join(name);
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn table() -> BTreeMap<String, String> {
    let v: Value = serde_json::from_str(&ui_file("i18n/en.json")).expect("en.json is JSON");
    v.as_object()
        .expect("en.json is an object")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("string").to_string()))
        .collect()
}

/// The first argument of every call of one of `names` in `code` (`t('a.b', {..})` gives `'a.b'`).
fn first_arguments(code: &str, names: &[&str]) -> Vec<String> {
    let bytes = code.as_bytes();
    let mut out = Vec::new();
    for name in names {
        let needle = format!("{name}(");
        let mut from = 0;
        while let Some(pos) = code[from..].find(&needle) {
            let start = from + pos;
            from = start + needle.len();
            let before = if start == 0 { b' ' } else { bytes[start - 1] };
            if before.is_ascii_alphanumeric() || before == b'_' || before == b'$' {
                continue;
            }
            let (mut depth, mut i, mut quote) = (0i32, from, 0u8);
            while i < bytes.len() {
                let c = bytes[i];
                if quote != 0 {
                    if c == b'\\' {
                        i += 1;
                    } else if c == quote {
                        quote = 0;
                    }
                } else if c == b'\'' || c == b'"' {
                    quote = c;
                } else if c == b'(' || c == b'[' || c == b'{' {
                    depth += 1;
                } else if (c == b')' || c == b']' || c == b'}') && depth > 0 {
                    depth -= 1;
                } else if (c == b',' || c == b')') && depth == 0 {
                    break;
                }
                i += 1;
            }
            out.push(code[from..i.min(code.len())].to_string());
        }
    }
    out
}

/// `("a.b")` gives a key, `("a." + x)` gives a prefix; both as (keys, prefixes).
fn keys_in(arg: &str, keys: &mut BTreeSet<String>, prefixes: &mut BTreeSet<String>) {
    let b = arg.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\'' || b[i] == b'"' {
            let q = b[i];
            let end = match arg[i + 1..].find(q as char) {
                Some(e) => i + 1 + e,
                None => return,
            };
            let lit = &arg[i + 1..end];
            let is_key = lit.contains('.')
                && lit.split('.').next().is_some_and(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_lowercase()))
                && lit.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_' || c == b'.');
            if is_key {
                if arg[end + 1..].trim_start().starts_with('+') {
                    prefixes.insert(lit.to_string());
                } else {
                    keys.insert(lit.to_string());
                }
            }
            i = end + 1;
        } else {
            i += 1;
        }
    }
}

fn used() -> (BTreeSet<String>, BTreeSet<String>) {
    let (page, js) = (ui_file("index.html"), ui_file("app.js"));
    let (mut keys, mut prefixes) = (BTreeSet::new(), BTreeSet::new());
    for arg in first_arguments(&page, &["t", "rich"]).into_iter().chain(first_arguments(&js, &["tr", "hasText"])) {
        keys_in(&arg, &mut keys, &mut prefixes);
    }
    (keys, prefixes)
}

#[test]
fn every_key_the_code_uses_is_in_the_english_table_and_none_is_left_over() {
    let table = table();
    let (keys, prefixes) = used();
    assert!(keys.len() > 250 && !prefixes.is_empty(), "the call scan is out of date ({} keys)", keys.len());
    let missing: Vec<_> = keys.iter().filter(|k| !table.contains_key(*k)).collect();
    assert!(missing.is_empty(), "keys the code uses but en.json lacks: {missing:?}");
    let unused: Vec<_> =
        table.keys().filter(|k| !keys.contains(*k) && !prefixes.iter().any(|p| k.starts_with(p.as_str()))).collect();
    assert!(unused.is_empty(), "keys in en.json that nothing uses: {unused:?}");
    for p in &prefixes {
        assert!(table.keys().any(|k| k.starts_with(p.as_str())), "no key under the prefix {p}");
    }
}

/// Text runs of the page outside the elements whose content is data (`code`), not shown (`script`, `style`,
/// `title`, `noscript`) or the brand; plus the translatable attributes written without a binding.
fn page_text_and_attributes(html: &str) -> (Vec<String>, Vec<String>) {
    let b = html.as_bytes();
    let (mut text, mut attrs) = (Vec::new(), Vec::new());
    let mut stack: Vec<(String, bool)> = Vec::new();
    let (mut i, mut run) = (0, String::new());
    let has_word = |s: &str| s.as_bytes().windows(2).any(|w| w[0].is_ascii_alphabetic() && w[1].is_ascii_alphabetic());
    const VOID: [&str; 6] = ["input", "meta", "link", "br", "img", "hr"];
    while i < b.len() {
        if html[i..].starts_with("<!--") {
            i = html[i..].find("-->").map_or(b.len(), |e| i + e + 3);
            continue;
        }
        if html[i..].starts_with("<!") {
            i = html[i..].find('>').map_or(b.len(), |e| i + e + 1); // <!doctype html>
            continue;
        }
        if b[i] == b'<' && b.get(i + 1).is_some_and(|c| c.is_ascii_alphabetic() || *c == b'/') {
            if has_word(&run) && !stack.last().is_some_and(|s| s.1) {
                text.push(run.trim().to_string());
            }
            run.clear();
            // the tag, up to the '>' outside quotes
            let (mut j, mut quote) = (i + 1, 0u8);
            while j < b.len() {
                let c = b[j];
                if quote != 0 {
                    if c == quote {
                        quote = 0;
                    }
                } else if c == b'\'' || c == b'"' {
                    quote = c;
                } else if c == b'>' {
                    break;
                }
                j += 1;
            }
            let tag = &html[i + 1..j.min(b.len())];
            if let Some(name) = tag.strip_prefix('/') {
                let name = name.trim();
                while let Some((n, _)) = stack.pop() {
                    if n == name {
                        break;
                    }
                }
            } else {
                let name: String = tag.chars().take_while(|c| c.is_ascii_alphanumeric()).collect();
                let skip = ["code", "script", "style", "title", "noscript"].contains(&name.as_str())
                    || tag.contains("class=\"brand\"")
                    || stack.last().is_some_and(|s| s.1);
                for attr in ["placeholder=", "title=", "aria-label=", "data-label=", "alt="] {
                    let mut f = 0;
                    while let Some(p) = tag[f..].find(attr) {
                        let at = f + p;
                        f = at + attr.len();
                        let prev = tag.as_bytes()[at.saturating_sub(1)];
                        if at > 0 && !prev.is_ascii_whitespace() {
                            continue; // ":placeholder=" is a binding
                        }
                        let value: String =
                            tag[f..].trim_start_matches('"').chars().take_while(|c| *c != '"').collect();
                        if has_word(&value) {
                            attrs.push(format!("{name} {attr}{value}"));
                        }
                    }
                }
                if !VOID.contains(&name.as_str()) && !tag.ends_with('/') {
                    stack.push((name, skip));
                }
            }
            i = j + 1;
            continue;
        }
        run.push(html[i..].chars().next().unwrap());
        i += html[i..].chars().next().unwrap().len_utf8();
    }
    (text, attrs)
}

#[test]
fn the_page_has_no_text_outside_the_table() {
    let (text, attrs) = page_text_and_attributes(&ui_file("index.html"));
    assert!(text.is_empty(), "text written into index.html: {text:?}");
    assert!(attrs.is_empty(), "translatable attributes written into index.html: {attrs:?}");
}

#[test]
fn no_loop_variable_hides_the_translation_helpers() {
    let page = ui_file("index.html");
    for part in page.split("x-for=\"").skip(1) {
        let head = part.split('"').next().unwrap_or("");
        let vars = head.split(" in ").next().unwrap_or("").trim_matches(|c| c == '(' || c == ')');
        for v in vars.split(',') {
            assert!(!["t", "rich"].contains(&v.trim()), "x-for variable hides a helper: {head}");
        }
    }
}

#[test]
fn the_ui_never_offers_a_plc_write() {
    let all = format!("{}{}{}", ui_file("index.html"), ui_file("app.js"), ui_file("i18n/en.json"))
        .replace("never writes a PLC value", "");
    let lower = all.to_lowercase();
    for bad in [
        "write a value",
        "write the value",
        "write value",
        "write a tag",
        "write the tag",
        "write tag",
        "setpoint",
        "set point",
        "set value",
        "set-value",
    ] {
        assert!(!lower.contains(bad), "the bundle mentions \"{bad}\"");
    }
}
