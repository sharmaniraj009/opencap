# Omron FINS conformance vectors

These are vectors for the FINS driver (ADR-0014; the Rust driver is #54). They use conformance
format 1, and their area is `omron_fins/addresses`. `../README.md` describes the format, the
input and expected shapes, and how to regenerate them:

```bash
python scripts/gen_conformance.py            # writes addresses/valid.json and addresses/invalid.json
python scripts/gen_conformance.py --check
```

- **Inputs and hand-written expectations:** `scripts/gen_conformance_fins.py`, written from PTC's
  *Omron FINS Ethernet Driver* manual v1.083 (CS1 pp. 57–61, CJ1 pp. 62–66, CJ2 pp. 66–70) and
  Omron W342 5-2-2.
- **Expected values in the files:** the FINS simulator's own parser (`tests/helpers/fins_sim.py`),
  through `tests/conformance/reference.py`.
- **Independence:** `tests/unit/test_fins_sim.py` fails if the files differ from the hand-written
  expectations. Mutants in `tests/unit/test_conformance_vectors.py` (a dropped counter offset, or
  a wrong DM bit code) must fail vectors.

The protocol notes are in [`docs/protocols/omron-fins.md`](../../../docs/protocols/omron-fins.md) §5.
`E0_100` (Omron's notation) and `EM100` are listed as invalid: KEPServerEX writes `E00:00100` and
`E00100`.
