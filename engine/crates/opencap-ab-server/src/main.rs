//! `ab_server` from libplctag 2.7.2 (EtherNet/IP PLC simulator: ControlLogix, Micro800, PLC/5, SLC 500).
use std::ffi::CString;
use std::os::raw::{c_char, c_int};
extern "C" {
    fn ab_server_main(argc: c_int, argv: *mut *mut c_char) -> c_int;
}
fn main() {
    let args: Vec<CString> = std::env::args().map(|a| CString::new(a).unwrap()).collect();
    let mut ptrs: Vec<*mut c_char> = args.iter().map(|a| a.as_ptr() as *mut c_char).collect();
    ptrs.push(std::ptr::null_mut());
    let rc = unsafe { ab_server_main(args.len() as c_int, ptrs.as_mut_ptr()) };
    std::process::exit(rc);
}
