//! The `omron_fins/addresses` conformance vectors (tests/conformance/omron_fins/, format 1),
//! generated from the FINS simulator's own parser: every vector must give the same area code,
//! word address, bit, kind, dims, string length/order, BCD flag and words, or the same error class.

use std::path::Path;

use opencap_driver_omron_fins::{parse_fins_address, Kind};
use serde_json::{json, Value as Json};

fn vectors(file: &str) -> Vec<Json> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/conformance/omron_fins/addresses").join(file);
    let doc: Json = serde_json::from_str(&std::fs::read_to_string(&path).expect("vector file")).expect("json");
    assert_eq!(doc["format"], 1, "{file}: unknown format");
    assert_eq!(doc["area"], "omron_fins/addresses");
    doc["vectors"].as_array().expect("vectors").clone()
}

fn run(input: &Json) -> Json {
    let dtype = input["dtype"].as_str();
    let model = input["model"].as_str().unwrap_or("CJ1");
    match parse_fins_address(input["address"].as_str().expect("address"), dtype, model) {
        Err(e) => json!({"error": e.reason.as_str()}),
        Ok(a) => {
            let (code, word) = a.codes();
            json!({
                "area_code": format!("0x{code:02X}"),
                "word": word,
                "bit": a.bit,
                "kind": a.kind.as_str(),
                "dims": a.dims,
                "str_len": a.str_len,
                "str_order": a.str_order.map(|c| c.to_string()),
                "bcd": a.bcd,
                "words": a.words(dtype.filter(|d| !matches!(*d, "bool" | "string"))),
            })
        }
    }
}

#[test]
fn every_address_vector_matches() {
    let mut failures = Vec::new();
    let mut n = 0;
    for file in ["valid.json", "invalid.json"] {
        for v in vectors(file) {
            n += 1;
            let got = run(&v["input"]);
            if got != v["expected"] {
                failures.push(format!(
                    "{file}/{}: {}\n  expected {}\n  got      {got}",
                    v["id"], v["description"], v["expected"]
                ));
            }
        }
    }
    assert!(n >= 100, "only {n} vectors");
    assert!(failures.is_empty(), "{} of {n} vectors fail:\n{}", failures.len(), failures.join("\n"));
}

#[test]
fn kinds_cover_the_vector_set() {
    let kinds: std::collections::BTreeSet<String> =
        vectors("valid.json").iter().map(|v| v["expected"]["kind"].as_str().unwrap().to_string()).collect();
    for k in [Kind::Word, Kind::Bit, Kind::Flag, Kind::String] {
        assert!(kinds.contains(k.as_str()), "{}", k.as_str());
    }
}
