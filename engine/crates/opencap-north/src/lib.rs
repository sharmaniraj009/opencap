//! opencap engine north side: SubDevice JSON payload (byte-identical to the Python
//! engine's), SQLite store-and-forward outbox (same file format), MQTT link with LWT and
//! status topics, the north plugin contract (03 §3.5) and the publisher (the `mqtt` plugin).

pub mod mqtt;
pub mod payload;
pub mod plugin;
pub mod publisher;
pub mod saf;
