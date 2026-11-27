//! Stable reason codes (LOG-R6, #133): every `warning` and `error` log line carries `reason`
//! from a closed list next to the free-text `error`, and `/devices/{id}` shows `last_reason`
//! next to `last_error`.
//!
//! Drivers report errors as text, so the code is found from the class (`BlockConfigError`,
//! `ConnectionLost`, ...) and the words of the text. The mapping is pinned by the vectors in
//! `tests/conformance/reasons/` (the simulator's faults included).

/// Device communication (LOG-R6's starting list, plus `unsupported`: the driver is not built or
/// not implemented, and `internal`: a bug, not a device fault).
pub const DEVICE: [&str; 12] = [
    "timeout",
    "refused",
    "reset",
    "unreachable",
    "name_resolution",
    "port_missing",
    "port_busy",
    "line_settings",
    "protocol_error",
    "address_rejected",
    "unsupported",
    "internal",
];
/// Config writes.
pub const CONFIG: [&str; 5] = ["invalid", "read_only", "disk_full", "partial", "io"];
/// The outbox.
pub const OUTBOX: [&str; 3] = ["disk_full", "io", "corrupt"];

/// True for any code of any list.
pub fn is_reason(code: &str) -> bool {
    DEVICE.contains(&code) || CONFIG.contains(&code) || OUTBOX.contains(&code)
}

/// The device-communication code for a failure's text (`CommError`, `ConnectionLost`, ...).
pub fn classify_comm(text: &str) -> &'static str {
    let t = text.to_lowercase();
    let has = |words: &[&str]| words.iter().any(|w| t.contains(w));
    if has(&["name or service not known", "lookup", "resolve", "no such host", "nodename", "name resolution"]) {
        "name_resolution"
    } else if has(&["refused", "simulated connect failure"]) {
        "refused"
    } else if has(&["reset", "broken pipe", "closed by peer", "connection closed", "aborted", "unexpected eof"]) {
        "reset"
    } else if has(&["unreachable", "no route", "host is down", "network is down"]) {
        "unreachable"
    } else if has(&[
        "timed out",
        "timeout",
        "no answer",
        "no reply",
        "did not answer",
        "did not reply",
        "deadline",
        "simulated read failure",
    ]) {
        "timeout"
    } else if has(&["resource busy", "port is busy", "access is denied", "permission denied", "in use by"]) {
        "port_busy"
    } else if has(&["no such file", "could not open", "cannot open", "port not found", "does not exist"]) {
        "port_missing"
    } else if has(&["baud", "parity", "stop bits", "line settings", "framing"]) {
        "line_settings"
    } else {
        "protocol_error"
    }
}

/// The code for a driver error: its Python class name (`DriverError::class_name`) and text.
pub fn classify(class: &str, text: &str) -> &'static str {
    match class {
        "BlockConfigError" => "address_rejected",
        "NotImplementedError" => "unsupported",
        "Exception" => "internal",
        _ => classify_comm(text),
    }
}

/// The code for a device's `last_error` text, as the worker writes it (`<block>: CommError: ...`,
/// `connect failed: ...`, `<block>: rejected: BlockConfigError: ...`).
pub fn of_last_error(text: &str) -> &'static str {
    if text.contains("BlockConfigError") || text.contains(": rejected: ") {
        "address_rejected"
    } else if text.contains("NotImplementedError") {
        "unsupported"
    } else if text.starts_with("internal error") {
        "internal"
    } else {
        classify_comm(text)
    }
}

/// The code a warning or error event gets when its call site names none: by event name first,
/// then by the words of its `error` text, else `internal`.
pub fn event_default(event: &str, error: Option<&str>) -> &'static str {
    match event {
        "device.unsupported" => return "unsupported",
        "device.idle" => return "address_rejected",
        "device.loop_error"
        | "channel.on_cycle_error"
        | "north.lagged"
        | "north.plugin_failed"
        | "publisher_loop_ended" => return "internal",
        "north.plugin_stop_timeout" => return "timeout",
        "reload_failed" | "mqtt_settings_changed" | "sparkplug.command_invalid" => return "invalid",
        "saf_not_wal" | "sd_notify failed" => return "io",
        "saf_dropped_oldest" => return "disk_full",
        "mqtt_message_rejected" => return "protocol_error",
        _ => {}
    }
    if event.contains("rejected") {
        return "address_rejected";
    }
    match error {
        Some(e) => classify_comm(e),
        None if event.starts_with("plugin.") || event.starts_with("ab_logix.") || event.contains("cpu_error") => {
            "protocol_error"
        }
        None => "internal",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_default_is_in_a_closed_list() {
        for event in ["device.block_rejected", "mqtt_disconnected", "saf_dropped_oldest", "plugin.log", "x"] {
            for error in [None, Some("Connection refused (os error 111)")] {
                assert!(is_reason(event_default(event, error)), "{event}");
            }
        }
    }

    #[test]
    fn words_of_the_text_pick_the_code() {
        assert_eq!(classify_comm("Connection refused (os error 111)"), "refused");
        assert_eq!(classify_comm("failed to lookup address information: Name or service not known"), "name_resolution");
        assert_eq!(classify_comm("something else entirely"), "protocol_error");
        assert_eq!(classify("BlockConfigError", "x"), "address_rejected");
        assert_eq!(event_default("mqtt_disconnected", Some("Connection reset by peer")), "reset");
    }
}
