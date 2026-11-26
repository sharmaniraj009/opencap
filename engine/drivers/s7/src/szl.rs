//! Decoders for the three SZL lists `identity` reads (python-snap7 `snap7.szl`): 0x001C
//! component identification, 0x0011 order code and firmware, 0x0424 operating mode.
//! `data` is the SZL record data after the SZL id and index, partial-list header included.

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct CpuInfo {
    pub as_name: Vec<u8>,
    pub module_name: Vec<u8>,
    pub copyright: Vec<u8>,
    pub serial_number: Vec<u8>,
    pub module_type_name: Vec<u8>,
}

fn rstrip_nul(b: &[u8]) -> Vec<u8> {
    let end = b.iter().rposition(|c| *c != 0).map_or(0, |i| i + 1);
    b[..end].to_vec()
}

pub fn parse_cpu_info(data: &[u8]) -> CpuInfo {
    let mut info = CpuInfo::default();
    if data.len() >= 30 {
        info.as_name = rstrip_nul(&data[6..30]);
    }
    if data.len() >= 64 {
        info.module_name = rstrip_nul(&data[40..64]);
    }
    if data.len() >= 134 {
        info.copyright = rstrip_nul(&data[108..134]);
    }
    if data.len() >= 166 {
        info.serial_number = rstrip_nul(&data[142..166]);
    }
    if data.len() >= 208 {
        info.module_type_name = rstrip_nul(&data[176..208]);
    }
    info
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct OrderCode {
    pub order_code: Vec<u8>,
    pub version: (u8, u8, u8),
}

fn rstrip_nul_space(b: &[u8]) -> Vec<u8> {
    let end = b.iter().rposition(|c| *c != 0 && *c != b' ').map_or(0, |i| i + 1);
    b[..end].to_vec()
}

pub fn parse_order_code(data: &[u8]) -> OrderCode {
    let mut oc = OrderCode::default();
    if data.len() < 4 {
        return oc;
    }
    let record_len = usize::from(u16::from_be_bytes([data[0], data[1]]));
    let ndr = usize::from(u16::from_be_bytes([data[2], data[3]]));
    if record_len < 22 || ndr < 1 || 4 + record_len * ndr > data.len() {
        return oc;
    }
    if !structured(data, record_len, ndr, &mut oc) {
        flat(data, &mut oc);
    }
    oc
}

/// S7-1200/1500 records with a 2-byte id; record 0x0007 (installed firmware) is preferred,
/// 0x0081 (boot loader) is ignored.
fn structured(data: &[u8], record_len: usize, ndr: usize, oc: &mut OrderCode) -> bool {
    let (mut found, mut has_7) = (false, false);
    let mut offset = 4;
    for _ in 0..ndr {
        let rec = &data[offset..(offset + record_len).min(data.len())];
        if rec.len() < 2 {
            break;
        }
        match u16::from_be_bytes([rec[0], rec[1]]) {
            0x0001 if record_len >= 22 => {
                found = true;
                oc.order_code = rstrip_nul_space(&rec[2..22]);
                if record_len >= 26 && !has_7 && (rec[23] != 0 || rec[24] != 0) {
                    oc.version = (rec[23], rec[24], rec[25]);
                }
            }
            0x0002 if record_len >= 26 && oc.version.0 == 0 && !has_7 => {
                found = true;
                oc.version = (rec[23], rec[24], rec[25]);
            }
            0x0007 if record_len >= 28 => {
                found = true;
                let n = rec.len();
                oc.version = (rec[n - 3], rec[n - 2], rec[n - 1]);
                has_7 = true;
            }
            0x0081 => found = true,
            _ => {}
        }
        offset += record_len;
    }
    found
}

/// S7-300: a flat ASCII stream; the MLFB is found by searching for "6ES7".
fn flat(data: &[u8], oc: &mut OrderCode) {
    let payload = &data[4..];
    let Some(start) = payload.windows(4).position(|w| w == b"6ES7") else { return };
    if payload.len() >= start + 20 {
        oc.order_code = rstrip_nul_space(&payload[start..start + 20]);
    }
    let v = start + 21;
    if payload.len() >= v + 3 {
        oc.version = (payload[v], payload[v + 1], payload[v + 2]);
    }
}

/// `run`, `stop` or `unknown` from SZL 0x0424 (only RUN is run, like native Snap7).
pub fn parse_cpu_state(data: &[u8]) -> Result<&'static str, String> {
    if data.len() < 8 {
        return Err(format!("SZL 0x0424 reply too short for an operating mode: {} bytes", data.len()));
    }
    let mode = data[7];
    Ok(if mode == 0 {
        "unknown"
    } else if mode & 0x0F == 0x08 {
        "run"
    } else {
        "stop"
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_info_fields_sit_at_the_python_offsets() {
        let mut d = vec![0u8; 208];
        d[6..12].copy_from_slice(b"SIM-AS");
        d[176..186].copy_from_slice(b"CPU 1511-1");
        let i = parse_cpu_info(&d);
        assert_eq!(i.as_name, b"SIM-AS");
        assert_eq!(i.module_type_name, b"CPU 1511-1");
        assert!(i.serial_number.is_empty());
        assert_eq!(parse_cpu_info(&d[..20]), CpuInfo::default());
    }

    #[test]
    fn order_code_structured_prefers_installed_firmware() {
        let mut data = vec![0, 28, 0, 2];
        let mut r1 = vec![0u8; 28];
        r1[1] = 1;
        r1[2..22].copy_from_slice(b"6ES7 511-1AK02-0AB0 ");
        r1[23..26].copy_from_slice(&[1, 2, 3]);
        let mut r7 = vec![0u8; 28];
        r7[1] = 7;
        r7[25..28].copy_from_slice(&[2, 9, 2]);
        data.extend(r1);
        data.extend(r7);
        let oc = parse_order_code(&data);
        assert_eq!(oc.order_code, b"6ES7 511-1AK02-0AB0");
        assert_eq!(oc.version, (2, 9, 2));
    }

    #[test]
    fn order_code_flat_and_state() {
        let mut data = vec![0, 22, 0, 1];
        data.extend_from_slice(b"..6ES7 315-2EH14-0AB0 \x00\x03\x02\x01......");
        let oc = parse_order_code(&data);
        assert_eq!(oc.order_code, b"6ES7 315-2EH14-0AB0");
        assert_eq!(oc.version, (3, 2, 1));
        assert_eq!(parse_cpu_state(&[0, 0, 0, 0, 0, 0, 0, 8]), Ok("run"));
        assert_eq!(parse_cpu_state(&[0, 0, 0, 0, 0, 0, 0, 3]), Ok("stop"));
        assert_eq!(parse_cpu_state(&[0; 8]), Ok("unknown"));
        assert!(parse_cpu_state(&[0; 3]).is_err());
    }
}
