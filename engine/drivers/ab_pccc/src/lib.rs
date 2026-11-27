//! Allen-Bradley PCCC data tables (SLC 500, MicroLogix, PLC-5): address parser, block
//! planner and value decoder, ported from Python `drivers/ab_pccc.py` and checked by the
//! shared vectors in `tests/conformance/ab_pccc`. The transports are separate crates
//! (`opencap-driver-ab-df1` for serial DF1). Read-only (ADR-0008): nothing here builds a write.

pub mod address;
pub mod decode;
pub mod plan;
pub mod status;
pub mod values;

pub use address::{check_dtype, parse_pccc_address, PcccAddress};
pub use decode::{decode_block, DecodeError};
pub use plan::{plan_pccc, PcccBlock, PcccItem, PcccParams};
