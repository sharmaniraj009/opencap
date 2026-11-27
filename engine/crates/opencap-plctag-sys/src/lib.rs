//! Raw, read-only bindings to libplctag. Only the functions below are declared; there is no
//! `plc_tag_write`, no `plc_tag_set_*` value setter, no raw-CIP write. `tests/read_only.rs`
//! fails if a write-capable symbol is declared or called anywhere in the engine (ADR-0008).
use std::os::raw::{c_char, c_int};

pub const PLCTAG_STATUS_OK: c_int = 0;
pub const PLCTAG_STATUS_PENDING: c_int = 1;

extern "C" {
    pub fn plc_tag_create(attrib_str: *const c_char, timeout: c_int) -> i32;
    pub fn plc_tag_destroy(tag: i32) -> c_int;
    pub fn plc_tag_read(tag: i32, timeout: c_int) -> c_int;
    pub fn plc_tag_status(tag: i32) -> c_int;
    pub fn plc_tag_decode_error(err: c_int) -> *const c_char;
    pub fn plc_tag_get_size(tag: i32) -> c_int;
    pub fn plc_tag_get_bit(tag: i32, offset_bit: c_int) -> c_int;
    pub fn plc_tag_get_uint32(tag: i32, offset: c_int) -> u32;
    pub fn plc_tag_get_int32(tag: i32, offset: c_int) -> i32;
    pub fn plc_tag_get_uint16(tag: i32, offset: c_int) -> u16;
    pub fn plc_tag_get_int16(tag: i32, offset: c_int) -> i16;
    pub fn plc_tag_get_float32(tag: i32, offset: c_int) -> f32;
    pub fn plc_tag_get_raw_bytes(tag: i32, start_offset: c_int, buffer: *mut u8, buffer_length: c_int) -> c_int;
    pub fn plc_tag_get_string_length(tag: i32, string_start_offset: c_int) -> c_int;
    pub fn plc_tag_get_string(tag: i32, string_start_offset: c_int, buffer: *mut c_char, buffer_length: c_int) -> c_int;
    pub fn plc_tag_set_debug_level(debug_level: c_int);
    pub fn plc_tag_shutdown();
}
