//! OPC UA server for the opencap tag store (#50, D-123): the tags as a read-only, KEPServerEX-style
//! address space, on async-opcua (MPL-2.0).
//!
//! ```text
//! Objects/<channel>/<device>/<group>/.../<tag>     ns=2;s=<channel>.<device>.<group>...<tag>
//! ```
//!
//! A tag name's dots are its tag-group path (`LTI002.Pv` -> folder `LTI002`, variable `Pv`), as in
//! KEPServerEX, so an existing client only changes the endpoint URL. The tag namespace is the first
//! one registered after the server's own, so it is `ns=2`.
//!
//! Values are copied from the tag store when the change bus reports them; the SourceTimestamp is the
//! store's `source_ts`; the StatusCode follows ADR-0006 (`quality::status_code`).
//!
//! Read-only (ADR-0008): every variable has `AccessLevel = CurrentRead`, the node manager offers no
//! write callback, and the server's own services refuse writes, method calls, history updates and
//! node management for every session (tests in `tests/`).

pub mod address;
pub mod config;
pub mod pki;
pub mod server;
pub mod values;

pub use server::OpcUaNorth;

/// The agent name and plugin kind.
pub const KIND: &str = "opcua";
