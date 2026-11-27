//! Packing of several read var items into as few requests as the PDU allows (#227, D-125).
//!
//! S7 read var carries up to 20 items; the request is `10 + 2 + 12 n` bytes and the reply
//! `12 + 2 + sum(4 + data)` bytes with each odd-length data part padded to an even length
//! (the last needs none; counting it anyway keeps a spare byte). Both must fit the negotiated PDU.
//! Next-fit in the given order: blocks come from the planner sorted by area, DB and offset, so
//! neighbours stay neighbours and the result is stable from cycle to cycle.

use crate::proto::ReadItem;

/// Items per request most CPUs accept (S7-300/400 allow 20 parallel items; 1200/1500 more).
pub const MAX_ITEMS: usize = 20;
/// Request header (10) + function and item count (2).
const REQUEST_FIXED: usize = 12;
const REQUEST_ITEM: usize = 12;
/// Reply header (12) + function and item count (2).
const REPLY_FIXED: usize = 14;
const REPLY_ITEM_HEADER: usize = 4;

/// Reply bytes of one item inside a packed reply.
fn reply_bytes(item: &ReadItem) -> usize {
    let n = item.nbytes();
    REPLY_ITEM_HEADER + n + (n & 1)
}

/// Indices into `items`, grouped into requests for a PDU of `pdu` bytes. Every index appears once,
/// in order. An item that does not fit alone still gets its own request (the planner and
/// `S7Block::parts` keep items within a PDU).
pub fn pack(items: &[ReadItem], pdu: usize) -> Vec<Vec<usize>> {
    let mut out: Vec<Vec<usize>> = Vec::new();
    let mut current: Vec<usize> = Vec::new();
    let (mut request, mut reply) = (REQUEST_FIXED, REPLY_FIXED);
    for (i, it) in items.iter().enumerate() {
        let (r, p) = (REQUEST_ITEM, reply_bytes(it));
        let fits = current.len() < MAX_ITEMS && request + r <= pdu && reply + p <= pdu;
        if !current.is_empty() && !fits {
            out.push(std::mem::take(&mut current));
            request = REQUEST_FIXED;
            reply = REPLY_FIXED;
        }
        current.push(i);
        request += r;
        reply += p;
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::WL_BYTE;

    fn item(start: u32, count: u16) -> ReadItem {
        ReadItem { area: 0x84, db: 1, start, word_len: WL_BYTE, count }
    }

    #[test]
    fn small_items_share_requests_up_to_twenty() {
        let items: Vec<ReadItem> = (0..45).map(|i| item(i * 4, 2)).collect();
        let groups = pack(&items, 480);
        assert_eq!(groups.iter().map(Vec::len).collect::<Vec<_>>(), [20, 20, 5]);
        assert_eq!(groups.concat(), (0..45).collect::<Vec<_>>());
    }

    #[test]
    fn the_reply_must_fit_the_pdu() {
        // 100 data bytes + 4 header = 104 each; a 240-byte PDU holds 14 + 2 x 104 = 222, a third does not
        let items: Vec<ReadItem> = (0..5).map(|i| item(i * 100, 100)).collect();
        let groups = pack(&items, 240);
        assert_eq!(groups.iter().map(Vec::len).collect::<Vec<_>>(), [2, 2, 1]);
    }

    #[test]
    fn an_odd_item_counts_its_padding_and_a_full_pdu_item_is_alone() {
        let items = vec![item(0, 3), item(10, 3)];
        // the request (12 + 2 x 12 = 36) is the tighter limit here; the reply needs 14 + 2 x (4 + 3 + 1) = 30
        assert_eq!(pack(&items, 36).len(), 1);
        assert_eq!(pack(&items, 35).len(), 2);
        // three odd items: reply 14 + 3 x 8 = 38, request 12 + 36 = 48
        let three = vec![item(0, 3), item(10, 3), item(20, 3)];
        assert_eq!(pack(&three, 48).len(), 1);
        let big = vec![item(0, 462), item(500, 2)];
        assert_eq!(pack(&big, 480).iter().map(Vec::len).collect::<Vec<_>>(), [1, 1]);
    }

    #[test]
    fn no_items_no_requests() {
        assert!(pack(&[], 480).is_empty());
    }
}
