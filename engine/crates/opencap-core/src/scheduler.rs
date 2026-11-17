//! Rate-group schedule of one device (Python `opencap.core.scheduler`). Pure: time is
//! passed in as monotonic seconds.
//!
//! - One slot per rate group; start jitter `now + U(0, jitter * rate)` (one-sided).
//! - `due()` = earliest due time; ties go to the faster rate, then the lower index.
//! - `complete()`: next due = `due + rate`; if that has already passed when the cycle
//!   ends, missed slots are dropped: next = `max(ended, started + rate)`.
//! - Overrun = the cycle itself took longer than its rate (waiting for the wire counts).

use rand::Rng;

pub const START_JITTER: f64 = 0.05;
pub const MIN_RATE_S: f64 = 0.001;

#[derive(Debug, Clone, PartialEq)]
pub struct GroupSlot {
    pub index: usize,
    pub rate_s: f64,
    pub next_due: f64,
    pub cycles: u64,
    pub overruns: u64,
}

pub fn start_offset<R: Rng + ?Sized>(rate_s: f64, jitter: f64, rng: &mut R) -> f64 {
    if jitter <= 0.0 {
        return 0.0;
    }
    rng.random_range(0.0..=jitter * rate_s)
}

/// (next due time, overrun) for a cycle that was due at `due`.
pub fn next_due_after(due: f64, rate_s: f64, started: f64, ended: f64) -> (f64, bool) {
    let mut nxt = due + rate_s;
    if nxt <= ended {
        nxt = ended.max(started + rate_s);
    }
    (nxt, (ended - started) > rate_s)
}

#[derive(Debug, Clone)]
pub struct DeviceSchedule {
    slots: Vec<GroupSlot>,
}

impl DeviceSchedule {
    pub fn new<R: Rng + ?Sized>(rates_s: &[(usize, f64)], now: f64, jitter: f64, rng: &mut R) -> Self {
        let slots = rates_s
            .iter()
            .map(|&(index, rate)| {
                let rate = rate.max(MIN_RATE_S);
                GroupSlot {
                    index,
                    rate_s: rate,
                    next_due: now + start_offset(rate, jitter, rng),
                    cycles: 0,
                    overruns: 0,
                }
            })
            .collect();
        Self { slots }
    }

    pub fn len(&self) -> usize {
        self.slots.len()
    }

    pub fn is_empty(&self) -> bool {
        self.slots.is_empty()
    }

    pub fn slots(&self) -> &[GroupSlot] {
        &self.slots
    }

    /// The slot to run next (a copy; book it with `complete(index, ..)`).
    pub fn due(&self) -> Option<GroupSlot> {
        self.slots
            .iter()
            .min_by(|a, b| {
                a.next_due.total_cmp(&b.next_due).then(a.rate_s.total_cmp(&b.rate_s)).then(a.index.cmp(&b.index))
            })
            .cloned()
    }

    pub fn delay(&self, now: f64) -> f64 {
        self.due().map(|s| (s.next_due - now).max(0.0)).unwrap_or(0.0)
    }

    /// Book a finished cycle of slot `index`; true if it overran.
    pub fn complete(&mut self, index: usize, started: f64, ended: f64) -> bool {
        let Some(slot) = self.slots.iter_mut().find(|s| s.index == index) else { return false };
        let (next, overrun) = next_due_after(slot.next_due, slot.rate_s, started, ended);
        slot.next_due = next;
        slot.cycles += 1;
        if overrun {
            slot.overruns += 1;
        }
        overrun
    }

    pub fn remove(&mut self, index: usize) {
        self.slots.retain(|s| s.index != index);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::{rngs::StdRng, SeedableRng};

    fn rng() -> StdRng {
        StdRng::seed_from_u64(7)
    }

    #[test]
    fn no_jitter_starts_now_and_fixed_rate() {
        let mut s = DeviceSchedule::new(&[(0, 1.0), (1, 5.0)], 100.0, 0.0, &mut rng());
        let d = s.due().unwrap();
        assert_eq!((d.index, d.next_due), (0, 100.0));
        assert!(!s.complete(0, 100.0, 100.2));
        assert_eq!(s.slots()[0].next_due, 101.0);
        assert_eq!(s.due().unwrap().index, 1); // 100.0 < 101.0
    }

    #[test]
    fn jitter_is_one_sided_within_5_percent() {
        for seed in 0..50 {
            let s = DeviceSchedule::new(&[(0, 2.0)], 10.0, START_JITTER, &mut StdRng::seed_from_u64(seed));
            let t = s.due().unwrap().next_due;
            assert!((10.0..=10.1).contains(&t), "{t}");
        }
    }

    #[test]
    fn overrun_drops_missed_slots() {
        // due 0, rate 1, cycle 0 -> 3.5: next = max(3.5, 0 + 1) = 3.5, overrun
        assert_eq!(next_due_after(0.0, 1.0, 0.0, 3.5), (3.5, true));
        // late start: due 0, started 2.0, ended 2.2 -> next = max(2.2, 3.0) = 3.0, no overrun
        assert_eq!(next_due_after(0.0, 1.0, 2.0, 2.2), (3.0, false));
        // on time
        assert_eq!(next_due_after(5.0, 1.0, 5.0, 5.4), (6.0, false));
    }

    #[test]
    fn ties_go_to_the_faster_rate_and_remove() {
        let mut s = DeviceSchedule::new(&[(0, 5.0), (1, 1.0)], 0.0, 0.0, &mut rng());
        assert_eq!(s.due().unwrap().index, 1);
        s.remove(1);
        assert_eq!(s.len(), 1);
        assert_eq!(s.delay(-2.0), 2.0);
    }
}
