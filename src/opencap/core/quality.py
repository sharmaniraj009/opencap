"""opencap quality -> north protocol quality: the one mapping table (ADR-0006).

The core has five qualities (`opencap.config.models.Quality`). Every north plugin turns them
into its protocol's model through this module and never hard-codes a code:

| opencap             | OPC UA StatusCode                    | OPC DA | Sparkplug B            | SubDevice |
|---------------------|--------------------------------------|--------|------------------------|-----------|
| `good`              | Good 0x00000000                      | 0xC0   | value, Quality 192     | `Data`    |
| `bad_comm`          | BadCommunicationError 0x80050000     | 0x18   | last value, Quality 24 | `BadTags` |
| `bad_config`        | BadConfigurationError 0x80890000     | 0x04   | null, Quality 4        | `BadTags` |
| `bad_decode`        | BadDataEncodingInvalid 0x80380000    | 0x00   | null, Quality 0        | `BadTags` |
| `stale`             | UncertainLastUsableValue 0x40900000  | 0x44   | last value, Quality 68 | `BadTags` |
| `stale`, never good | BadWaitingForInitialData 0x80320000  | 0x00   | null, Quality 0        | `BadTags` |

`ever_good` says whether the tag has a last good value (the cache keeps the last value on
every non-good quality, so a tag that was never read good has none). It is ignored for
`good`, which always carries its value.

StatusCode values are those of OPC 10000-6 Annex A, as published in the OPC Foundation's
`UA-Nodeset/Schema/StatusCode.csv`; `bad_decode` is BadDataEncodingInvalid, not
BadDecodingError (which means a malformed OPC UA message). OPC DA bytes are the OPC DA 2.05/3.0
quality field (`QQSSSSLL`). Sparkplug B has no standard quality: Ignition reads a metric
property named `Quality` holding the OPC DA value. Conformance vectors:
`tests/conformance/quality/`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_args

from opencap.config.models import Quality

QUALITIES: tuple[Quality, ...] = get_args(Quality)

# Best to worst. The worst of several qualities is the one latest in this order.
QUALITY_ORDER: tuple[Quality, ...] = ("good", "stale", "bad_comm", "bad_decode", "bad_config")
_RANK = {q: i for i, q in enumerate(QUALITY_ORDER)}

# OPC UA StatusCodes used here: spec name -> value (OPC 10000-6 Annex A, StatusCode.csv).
STATUS_CODES: dict[str, int] = {
    "Good": 0x00000000,
    "UncertainLastUsableValue": 0x40900000,
    "BadCommunicationError": 0x80050000,
    "BadWaitingForInitialData": 0x80320000,
    "BadDataEncodingInvalid": 0x80380000,
    "BadConfigurationError": 0x80890000,
}

# OPC DA quality bytes used here (opcda.h names).
OPC_QUALITY_BAD = 0x00
OPC_QUALITY_CONFIG_ERROR = 0x04
OPC_QUALITY_COMM_FAILURE = 0x18
OPC_QUALITY_LAST_USABLE = 0x44
OPC_QUALITY_GOOD = 0xC0

#                 quality       OPC UA name                 OPC DA                    null without last value only
_TABLE: dict[str, tuple[str, int, bool]] = {
    "good":       ("Good",                     OPC_QUALITY_GOOD,         False),
    "bad_comm":   ("BadCommunicationError",    OPC_QUALITY_COMM_FAILURE, False),
    "bad_config": ("BadConfigurationError",    OPC_QUALITY_CONFIG_ERROR, True),
    "bad_decode": ("BadDataEncodingInvalid",   OPC_QUALITY_BAD,          True),
    "stale":      ("UncertainLastUsableValue", OPC_QUALITY_LAST_USABLE,  False),
}
# A stale tag that never had a good value: there is no "last usable value".
_STALE_NEVER_GOOD: tuple[str, int] = ("BadWaitingForInitialData", OPC_QUALITY_BAD)


def _row(quality: str) -> tuple[str, int, bool]:
    try:
        return _TABLE[quality]
    except KeyError:
        raise ValueError(f"unknown quality {quality!r} (known: {', '.join(QUALITIES)})") from None


def opcua_name(quality: Quality, ever_good: bool) -> str:
    """OPC UA StatusCode name, as in the specification ("BadCommunicationError")."""
    name, _, _ = _row(quality)
    if quality == "stale" and not ever_good:
        return _STALE_NEVER_GOOD[0]
    return name


def to_opcua(quality: Quality, ever_good: bool) -> int:
    """OPC UA StatusCode for a value of this quality."""
    return STATUS_CODES[opcua_name(quality, ever_good)]


def to_opcda(quality: Quality, ever_good: bool) -> int:
    """OPC DA quality byte (0xC0 good, 0x44 uncertain last usable, ...)."""
    _, da, _ = _row(quality)
    if quality == "stale" and not ever_good:
        return _STALE_NEVER_GOOD[1]
    return da


def to_sparkplug(quality: Quality, ever_good: bool = True) -> dict[str, Any]:
    """Sparkplug B metric: `is_null` (send no value) and the `Quality` property (OPC DA value).

    bad_config and bad_decode never carry a value; good carries the current value; stale and
    bad_comm carry the last value, unless the tag never had one (`ever_good` false).
    """
    _, _, null = _row(quality)
    no_value = quality != "good" and not ever_good
    return {"is_null": null or no_value, "Quality": to_opcda(quality, ever_good)}


def to_subdevice(quality: Quality) -> str:
    """SubDevice JSON section a tag of this quality goes to: "Data" (good) or "BadTags" (D-017)."""
    _row(quality)
    return "Data" if quality == "good" else "BadTags"


def worst(qualities: Iterable[Quality]) -> Quality:
    """The worst quality of `qualities` by `QUALITY_ORDER`; "good" for none."""
    out: Quality = "good"
    for q in qualities:
        _row(q)
        if _RANK[q] > _RANK[out]:
            out = q
    return out
