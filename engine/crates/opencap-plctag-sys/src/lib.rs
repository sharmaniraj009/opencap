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
    pub fn plc_tag_get_string(tag: i32, string_start_offset: c_int, buffer: *mut c_char, buffer_length: c_int)
        -> c_int;
    pub fn plc_tag_set_debug_level(debug_level: c_int);
    pub fn plc_tag_shutdown();
}

/// The safe, read-only layer the drivers use: create a tag, read it, copy its bytes.
pub mod safe {
    use super::*;
    use std::ffi::{CStr, CString};

    /// A libplctag tag handle, destroyed on drop.
    #[derive(Debug)]
    pub struct Tag(i32);

    impl Tag {
        /// Create a tag from an attribute string, waiting up to `timeout_ms` for its first connect.
        /// `Err` is the libplctag status code (negative).
        pub fn create(attributes: &str, timeout_ms: i32) -> Result<Tag, i32> {
            let attr = CString::new(attributes).map_err(|_| -7)?; // PLCTAG_ERR_BAD_PARAM
                                                                  // SAFETY: `attr` is a valid NUL-terminated string for the duration of the call.
            let h = unsafe { plc_tag_create(attr.as_ptr(), timeout_ms) };
            if h < 0 {
                Err(h)
            } else {
                Ok(Tag(h))
            }
        }

        /// Read the tag from the PLC, waiting up to `timeout_ms`, and return its raw bytes.
        pub fn read(&self, timeout_ms: i32) -> Result<Vec<u8>, i32> {
            // SAFETY: the handle is valid until drop; the buffer is sized by libplctag itself.
            unsafe {
                let rc = plc_tag_read(self.0, timeout_ms);
                if rc != PLCTAG_STATUS_OK {
                    return Err(rc);
                }
                let size = plc_tag_get_size(self.0);
                if size < 0 {
                    return Err(size);
                }
                let mut buf = vec![0u8; size as usize];
                let rc = plc_tag_get_raw_bytes(self.0, 0, buf.as_mut_ptr(), size);
                if rc != PLCTAG_STATUS_OK {
                    return Err(rc);
                }
                Ok(buf)
            }
        }
    }

    impl Drop for Tag {
        fn drop(&mut self) {
            // SAFETY: the handle came from plc_tag_create and is destroyed exactly once.
            unsafe { plc_tag_destroy(self.0) };
        }
    }

    /// libplctag's text for a status code.
    pub fn status_text(rc: i32) -> String {
        // SAFETY: plc_tag_decode_error returns a static NUL-terminated string.
        unsafe { CStr::from_ptr(plc_tag_decode_error(rc)) }.to_string_lossy().into_owned()
    }
}
