"""Filtering, sorting and paging of the list routes (API-17, API-18).

`GET /api/v1/channels`, `/devices` and `/devices/{id}/tags` accept

    q       case-insensitive substring of `name` and `description`
    sort    a field documented for that collection (anything else: 422)
    order   asc (default) | desc
    limit   1..10000   page size; absent = the whole collection, as before paging existed
    offset  >= 0       items to skip

in that order: filter, sort, then page. The answer carries `total` (the count after filtering,
before paging), `limit` and `offset` next to the list; `revision` stays the whole config's.
String fields sort naturally and without case (`A2` before `A10`), numbers numerically, and an
item without a value for the field goes last in both directions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Query

from opencap.config.store import Invalid

MAX_LIMIT = 10_000

# The fields each collection can be sorted by (documented in api/openapi.json).
CHANNEL_SORT = ("name", "enabled", "devices")
DEVICE_SORT = ("id", "channel", "name", "driver", "host", "port", "enabled", "tags")
TAG_SORT = ("name", "address", "dtype", "scan_ms", "enabled", "description")

_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class CollectionQuery:
    q: str | None = None
    sort: str | None = None
    order: str = "asc"
    limit: int | None = None
    offset: int = 0


def collection_query(
    q: Annotated[str | None, Query(max_length=200, description="case-insensitive substring of name and description")] = None,
    sort: Annotated[str | None, Query(description="field to sort by; the fields are listed per route, anything "
                                                  "else is a 422")] = None,
    order: Annotated[Literal["asc", "desc"], Query(description="sort direction")] = "asc",
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT, description="page size; absent: the whole collection")] = None,
    offset: Annotated[int, Query(ge=0, description="items to skip, after filtering and sorting")] = 0,
) -> CollectionQuery:
    return CollectionQuery(q=q or None, sort=sort or None, order=order, limit=limit, offset=offset)


CollectionParams = Annotated[CollectionQuery, Depends(collection_query)]


def _natural(text: str) -> tuple[tuple[int, int, str], ...]:
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in _DIGITS.split(text.casefold()) if part)


def _key(value: Any) -> Any:
    if isinstance(value, str):
        return _natural(value)
    return value                                   # numbers and bools compare as numbers


def _matches(item: dict[str, Any], needle: str) -> bool:
    return any(isinstance(item.get(field), str) and needle in item[field].casefold()
               for field in ("name", "description"))


def apply_query(items: list[dict[str, Any]], query: CollectionQuery, sort_fields: tuple[str, ...]) -> dict[str, Any]:
    """Filter, sort and page `items`. Returns `{"items", "total", "limit", "offset"}`; raises
    `Invalid` (422) for a `sort` field the collection does not offer."""
    if query.sort is not None and query.sort not in sort_fields:
        raise Invalid("cannot sort by that field",
                      [{"loc": ["query", "sort"], "msg": f"sort must be one of {', '.join(sort_fields)}",
                        "type": "enum"}])
    if query.q:
        needle = query.q.casefold()
        items = [i for i in items if _matches(i, needle)]
    if query.sort is not None:
        present = [i for i in items if i.get(query.sort) is not None]
        missing = [i for i in items if i.get(query.sort) is None]
        present.sort(key=lambda i: _key(i[query.sort]), reverse=query.order == "desc")
        items = present + missing
    total = len(items)
    page = items[query.offset:] if query.limit is None else items[query.offset:query.offset + query.limit]
    return {"items": page, "total": total, "limit": query.limit, "offset": query.offset}
