"""Plugin protocol (#49): the committed Python stubs match the .proto, and the host maps values,
qualities and errors onto the protocol as the driver contract defines them."""

from __future__ import annotations

import pickle
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("grpc")

from opencap.core.cache import TagValue  # noqa: E402
from opencap.drivers.ab_pccc import PcccBlock  # noqa: E402
from opencap.drivers.base import BlockConfigError, CommError, ConnectionLost, DecodeError  # noqa: E402
from opencap.plugin.v1 import driver_pb2 as pb  # noqa: E402
from opencap.plugin_host import Sealer, driver_error, micros, tag_value, variant  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ["driver_pb2.py", "driver_pb2.pyi", "driver_pb2_grpc.py"]


def test_committed_stubs_match_the_proto(tmp_path: Path) -> None:
    pytest.importorskip("grpc_tools")
    subprocess.run(
        [sys.executable, "-m", "grpc_tools.protoc", "-I", str(ROOT / "proto"), f"--python_out={tmp_path}",
         f"--pyi_out={tmp_path}", f"--grpc_python_out={tmp_path}", "opencap/plugin/v1/driver.proto"],
        check=True, cwd=ROOT)
    for name in GENERATED:
        fresh = (tmp_path / "opencap" / "plugin" / "v1" / name).read_text(encoding="utf-8")
        committed = (ROOT / "src" / "opencap" / "plugin" / "v1" / name).read_text(encoding="utf-8")
        assert fresh == committed, f"{name} is stale: regenerate the stubs (proto/README.md)"


@pytest.mark.parametrize(("value", "field", "want"), [
    (True, "bool_value", True),
    (-(2**63), "int_value", -(2**63)),
    (2**63, "uint_value", 2**63),            # above int64: only uint64 registers
    (1.5, "float_value", 1.5),
    ("x", "string_value", "x"),
])
def test_variant_scalars(value, field: str, want) -> None:  # noqa: ANN001
    v = variant(value)
    assert v is not None and v.WhichOneof("kind") == field and getattr(v, field) == want


def test_variant_lists_and_unrepresentable_values() -> None:
    v = variant([1, [2.5, False]])
    assert v is not None and [i.WhichOneof("kind") for i in v.list_value.items] == ["int_value", "list_value"]
    assert variant(2**64) is None and variant(b"raw") is None and variant([1, object()]) is None


def test_tag_values_follow_the_worker_rules() -> None:
    ts = datetime(2026, 11, 16, 10, 0, 0, 123456, tzinfo=UTC)
    good = tag_value(TagValue(7, "good", ts))
    assert good.quality == pb.GOOD and good.value.int_value == 7 and good.source_ts_us == micros(ts)
    assert micros(ts) == int(ts.replace(microsecond=0).timestamp()) * 10**6 + 123456
    decode = tag_value(TagValue(DecodeError("short reply"), "good", ts))
    assert decode.quality == pb.BAD_DECODE and not decode.HasField("value")
    assert tag_value(TagValue(b"\x00", "good", ts)).quality == pb.BAD_DECODE
    bad = tag_value(TagValue(None, "bad_config", None))
    assert bad.quality == pb.BAD_CONFIG and not bad.HasField("source_ts_us") and not bad.HasField("value")


@pytest.mark.parametrize(("exc", "kind", "message"), [
    (ConnectionLost("reset"), pb.DriverError.CONNECTION_LOST, "reset"),
    (CommError("timeout"), pb.DriverError.COMM, "timeout"),
    (BlockConfigError("no file"), pb.DriverError.BLOCK_CONFIG, "no file"),
    (NotImplementedError("later"), pb.DriverError.UNSUPPORTED, "later"),
    (ValueError("bad"), pb.DriverError.OTHER, "ValueError: bad"),
])
def test_errors_keep_their_class(exc: Exception, kind: int, message: str) -> None:
    err = driver_error(exc)
    assert (err.kind, err.message) == (kind, message)


def test_sealed_blocks_round_trip_and_forgeries_are_never_unpickled(monkeypatch: pytest.MonkeyPatch) -> None:
    block = PcccBlock(file_type="N", file_number=7, start=0, count=2, elem_size=2, address="N7:0{2}", tags=["a"])
    sealer = Sealer(b"k" * 32)
    payload = sealer.seal(block)
    assert sealer.open(payload) == block
    calls = []
    monkeypatch.setattr(pickle, "loads", lambda *a, **k: calls.append(a))
    forged = payload[:1] + b"\x00" * 32 + pickle.dumps(block)
    for bad in (forged, Sealer(b"j" * 32).seal(block), b"", b"\x02" + payload[1:]):
        with pytest.raises(ValueError):
            sealer.open(bad)
    assert calls == []
