"""Comment-preserving edits of the hand-owned YAML files (`devices_map.yaml`, `overrides.yaml`).

The config API changes these files as *plain data* (what PyYAML, i.e. the loader, reads).
`render()` then carries the change into the existing text with ruamel.yaml's round-trip
mode, so comments, key order and scalar styles of everything that did not change survive:

1. the old text is loaded as a ruamel document (comments attached to nodes);
2. the document is synced to the new plain data: removed keys are deleted, changed values
   replaced, new keys appended, renamed keys (device ids, tag names) keep their position
   and their comments;
3. the result is parsed again with PyYAML, the parser the loader uses. If it does not give
   back exactly the new data (YAML 1.1 vs 1.2 corner cases, odd layouts), the file is
   rewritten from the data with PyYAML instead: the leading comment block is kept, other
   comments are lost, and the caller is told so (`preserved=False`). The `.bak` written
   next to the file still has the old text.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import yaml

try:  # ruamel.yaml is part of the `north` extra (it is only needed by the config API)
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
except ModuleNotFoundError:  # pragma: no cover - exercised only without the extra
    YAML = None  # type: ignore[assignment]

Renames = dict[tuple[str, ...], dict[str, str]]   # mapping path -> {old key: new key}


def parse(text: str | None) -> dict[str, Any]:
    """The loader's view of a YAML file: missing/empty -> {}; top level must be a mapping."""
    if not text:
        return {}
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"top level must be a mapping, got {type(data).__name__}")
    return data


def _round_trip() -> Any:
    rt = YAML(typ="rt")
    rt.preserve_quotes = True
    rt.width = 4096                      # never fold long lines
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.default_flow_style = False        # new nested values in block style, like the importer
    rt.representer.add_representer(
        type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return rt


def _needs_quotes(s: str) -> bool:
    """True when PyYAML (YAML 1.1) would not read the bare scalar back as this string."""
    try:
        return yaml.safe_load(s) != s
    except yaml.YAMLError:
        return True


def _fresh(value: Any) -> Any:
    """A new value to put into the document: strings PyYAML would misread are quoted."""
    if isinstance(value, str):
        return DoubleQuotedScalarString(value) if _needs_quotes(value) else value
    if isinstance(value, dict):
        out = CommentedMap()
        for k, v in value.items():
            out[_fresh(k)] = _fresh(v)
        return out
    if isinstance(value, list):
        return [_fresh(v) for v in value]
    return value


def _plain_equal(node: Any, value: Any) -> bool:
    """ruamel node vs plain value, compared as PyYAML would read the node."""
    if isinstance(node, dict) and isinstance(value, dict):
        return (len(node) == len(value)
                and all(k in value and _plain_equal(node[k], value[k]) for k in node))
    if isinstance(node, list) and isinstance(value, list):
        return len(node) == len(value) and all(_plain_equal(a, b) for a, b in zip(node, value))
    if isinstance(node, (dict, list)) or isinstance(value, (dict, list)):
        return False
    if isinstance(node, bool) or isinstance(value, bool):
        return isinstance(node, bool) and isinstance(value, bool) and node == value
    if isinstance(node, str) and _needs_quotes(node) and not getattr(node, "style", None):
        return False                     # a bare `off` / `yes`: PyYAML reads something else
    return node == value


def _rename_in_place(cm: Any, old: str, new: str) -> None:
    pos = list(cm.keys()).index(old)
    comment = cm.ca.items.pop(old, None)
    value = cm.pop(old)
    cm.insert(pos, _fresh(new), value)
    if comment is not None:
        cm.ca.items[new] = comment


def _sync(cm: Any, new: dict[str, Any], path: tuple[str, ...], renames: Renames) -> None:
    for old_key, new_key in (renames.get(path) or {}).items():
        if old_key in cm and new_key not in cm and old_key != new_key:
            _rename_in_place(cm, old_key, new_key)
    for key in [k for k in cm if k not in new]:
        del cm[key]
    if len(cm) == 0 and new and cm.fa.flow_style():
        cm.fa.set_block_style()          # `devices: {}` grows into a block mapping
    for key, value in new.items():
        if key in cm:
            node = cm[key]
            if isinstance(node, dict) and isinstance(value, dict) and hasattr(node, "fa"):
                _sync(node, value, (*path, key), renames)
            elif not _plain_equal(node, value):
                cm[key] = _fresh(value)
        else:
            cm[_fresh(key)] = _fresh(value)


def _leading_comments(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if line.strip() == "" or line.lstrip().startswith("#"):
            lines.append(line)
        else:
            break
    return "".join(lines)


def _plain_dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)


def rewrite(old_text: str | None, new: dict[str, Any], file_name: str) -> str:
    """Comment-free rewrite (fallback): keep the leading comment block, say what happened."""
    head = _leading_comments(old_text or "")
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    note = (f"# Rewritten by the opencap config API on {stamp}: comments below the header could not\n"
            f"# be kept. The previous text is in {file_name}.bak.\n")
    return head + note + _plain_dump(new)


def render(old_text: str | None, new: dict[str, Any], *, file_name: str, template: str = "",
           renames: Renames | None = None) -> tuple[str, bool]:
    """Text for `new`, keeping the comments of `old_text` (or of `template` for a new file).

    Returns (text, preserved). `preserved` is False when the comment-free fallback was used.
    """
    base = old_text if old_text is not None else template
    if YAML is not None and base.strip():
        try:
            rt = _round_trip()
            doc = rt.load(base)
            if isinstance(doc, CommentedMap):
                _sync(doc, new, (), renames or {})
                buf = io.StringIO()
                rt.dump(doc, buf)
                text = buf.getvalue()
                if parse(text) == new:
                    return text, True
        except Exception:  # noqa: BLE001 - any ruamel trouble: fall back to the plain rewrite
            pass
    if old_text is None or not old_text.strip():
        head = _leading_comments(base)
        return head + _plain_dump(new), True
    return rewrite(old_text, new, file_name), False
