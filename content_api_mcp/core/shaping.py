"""Response-size control for the three tools that can fill a chat context (daisy-content#20).

The upstream module answers for HTTP callers, not chat: `content_records_list`
returns every field and relation key of every record (about 950 B per `Page`
and far more per element on the daisy-content#10 testbed, so 47 KB at the module's default
limit of 50), `content_fingerprint` has no limit at all, and
`content_schema_site` grows with the site's class count. This module adds
arguments to those three tools' advertised schemas (the pinned
schema/endpoints.json is never edited), strips them before the upstream call,
and reshapes the response afterwards:

- `response_format`: `concise` or `detailed`.
  - `content_records_list` defaults to `concise`: each record becomes
    `{id, classRef, externalId, title, stage}`, plus any `fields` the caller
    names. `detailed` returns the upstream records unchanged. Its `limit`
    defaults to DEFAULT_RECORDS_LIMIT when omitted.
  - `content_fingerprint` and `content_schema_site` default to `detailed`
    (a fingerprint is for diffing, and the site schema is the call an agent
    makes first). `concise` replaces the fingerprint's rows with per-section
    counts (totals and violations stay whole), and groups the schema's
    classes into page, element and other class refs.
- A size cap (CONTENT_MAX_RESPONSE_CHARS, default DEFAULT_MAX_RESPONSE_CHARS)
  on the serialized result. Over it, list rows are dropped from the end and
  the response says so in a `truncated` block naming how to narrow the call;
  fingerprint `violations` and `totals` are never dropped, and an oversized
  detailed site schema falls back to the concise one.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from content_api_mcp._base.errors import ValidationError

LOGGER = logging.getLogger(__name__)

DEFAULT_RECORDS_LIMIT = 25

# About 10k tokens of JSON: well under Claude Code's 25k-token default tool
# response cap, leaving room for the rest of a turn.
DEFAULT_MAX_RESPONSE_CHARS = 40_000

RECORDS_LIST = "content_records_list"
FINGERPRINT = "content_fingerprint"
SCHEMA_SITE = "content_schema_site"

_DEFAULT_FORMAT = {
    RECORDS_LIST: "concise",
    FINGERPRINT: "detailed",
    SCHEMA_SITE: "detailed",
}

_FORMAT_DESCRIPTIONS = {
    RECORDS_LIST: (
        'Default "concise": each record is {id, classRef, externalId, title, '
        'stage}, plus the fields named in `fields`. "detailed" returns every '
        "field and relation key (about 1 KB per page record, more per "
        "element). Read one record in full with content_records_read."
    ),
    FINGERPRINT: (
        'Default "detailed": every row. "concise" returns totals, violations '
        "and, per section, its row count (and a related section's unresolved "
        "count), without the rows."
    ),
    SCHEMA_SITE: (
        'Default "detailed". "concise" lists class refs grouped into pages, '
        "elements and other, without per-class verbs and flags or the crud "
        "block; use content_schema_class for one class's contract."
    ),
}

_FIELDS_PROPERTY = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "uniqueItems": True,
    "description": (
        'With response_format "concise", also include these fields or '
        'relations by name (e.g. ["URLSegment", "Parent"]), taken from each '
        "record's `fields`, else its `relations` (a has_one such as Parent "
        "is a relation, not a ParentID field). Names found on no record are "
        'listed in meta.unknownFields. Not allowed with "detailed", which '
        "already returns everything."
    ),
}

_TITLE_FIELDS = ("Title", "Name", "MenuTitle")

# Room kept free for the `truncated` marker (and `nextOffset`) when rows are
# dropped, so the response including the marker still fits the cap.
_MARKER_RESERVE = 600


def shaped_tools() -> frozenset[str]:
    return frozenset(_DEFAULT_FORMAT)


def max_response_chars() -> int:
    """CONTENT_MAX_RESPONSE_CHARS, read per call so a compose override needs no
    rebuild. A missing, non-integer or sub-1000 value uses the default."""
    raw = os.getenv("CONTENT_MAX_RESPONSE_CHARS", "").strip()
    if not raw:
        return DEFAULT_MAX_RESPONSE_CHARS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1000:
        LOGGER.warning(
            "CONTENT_MAX_RESPONSE_CHARS=%r is not an integer >= 1000; using %d",
            raw,
            DEFAULT_MAX_RESPONSE_CHARS,
        )
        return DEFAULT_MAX_RESPONSE_CHARS
    return value


def inject_shaping(name: str, schema: dict) -> dict:
    """Add `response_format` (and `fields` for records_list) to a copy of a
    shaped tool's inputSchema; any other tool's schema is returned as is."""
    if name not in _DEFAULT_FORMAT:
        return schema
    schema = dict(schema)
    properties = dict(schema.get("properties", {}))
    properties["response_format"] = {
        "type": "string",
        "enum": ["concise", "detailed"],
        "default": _DEFAULT_FORMAT[name],
        "description": _FORMAT_DESCRIPTIONS[name],
    }
    if name == RECORDS_LIST:
        properties["fields"] = _FIELDS_PROPERTY
        limit = dict(properties.get("limit", {}))
        limit["default"] = DEFAULT_RECORDS_LIMIT
        limit["description"] = (
            f"Default {DEFAULT_RECORDS_LIMIT} here (the module's own default is "
            "50); 1 to 500."
        )
        properties["limit"] = limit
    schema["properties"] = properties
    return schema


def split_shaping_args(name: str, arguments: dict) -> tuple[dict, dict]:
    """Pop this module's arguments; return (upstream arguments, options).

    Applies DEFAULT_RECORDS_LIMIT to a records_list call that names no limit,
    so the default the schema advertises is the one the module receives.
    """
    if name not in _DEFAULT_FORMAT:
        return arguments, {}
    arguments = dict(arguments)
    response_format = arguments.pop("response_format", None) or _DEFAULT_FORMAT[name]
    options: dict[str, Any] = {"response_format": response_format}
    if name == RECORDS_LIST:
        fields = arguments.pop("fields", None)
        if fields and response_format == "detailed":
            raise ValidationError(
                'fields: only allowed with response_format "concise" ("detailed" '
                "already returns every field)."
            )
        options["fields"] = list(fields or [])
        arguments.setdefault("limit", DEFAULT_RECORDS_LIMIT)
        options["offset"] = arguments.get("offset", 0)
    return arguments, options


def shape_response(name: str, result: Any, options: dict) -> Any:
    """Reshape and cap one shaped tool's result; others pass through."""
    if name not in _DEFAULT_FORMAT or not isinstance(result, dict):
        return result
    cap = max_response_chars()
    if name == RECORDS_LIST:
        return _shape_records_list(result, options, cap)
    if name == FINGERPRINT:
        return _shape_fingerprint(result, options, cap)
    return _shape_schema_site(result, options, cap)


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False))


def _concise_record(record: Any, fields: list[str]) -> Any:
    if not isinstance(record, dict):
        return record
    record_fields = record.get("fields")
    record_fields = record_fields if isinstance(record_fields, dict) else {}
    title = next(
        (record_fields[f] for f in _TITLE_FIELDS if record_fields.get(f)), None
    )
    concise = {
        "id": record.get("id"),
        "classRef": record.get("classRef"),
        "externalId": record.get("externalId"),
        "title": title,
        "stage": record.get("stage"),
    }
    if fields:
        relations = record.get("relations")
        relations = relations if isinstance(relations, dict) else {}
        picked = {}
        for name in fields:
            if name in record_fields:
                picked[name] = record_fields[name]
            elif name in relations:
                picked[name] = relations[name]
        concise["fields"] = picked
    return concise


def _shape_records_list(result: dict, options: dict, cap: int) -> dict:
    rows = result.get("data")
    if not isinstance(rows, list):
        return result
    shaped = dict(result)
    originals = rows
    fields = options.get("fields", [])
    meta = shaped.get("meta")
    meta = dict(meta) if isinstance(meta, dict) else {}
    shaped["meta"] = meta
    if options.get("response_format") == "concise":
        rows = [_concise_record(row, fields) for row in rows]
        if fields and rows:
            found = {
                name
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("fields"), dict)
                for name in row["fields"]
            }
            unknown = [name for name in fields if name not in found]
            if unknown:
                meta["unknownFields"] = unknown
    kept = _fit_rows(rows, lambda r: {**shaped, "data": r}, cap)
    oversized = None
    if len(kept) < len(rows):
        kept = _fit_rows(rows, lambda r: {**shaped, "data": r}, cap - _MARKER_RESERVE)
        if not kept:
            # Even the first row alone is over the cap: return its minimal
            # concise form, so the page still advances by one record.
            kept = [_concise_record(originals[0], [])]
            oversized = kept[0].get("id") if isinstance(kept[0], dict) else None
    shaped["data"] = kept
    offset = meta.get("offset", options.get("offset", 0)) or 0
    if not isinstance(offset, int):
        offset = 0
    total = meta.get("total")
    if isinstance(total, int) and offset + len(kept) < total:
        meta["nextOffset"] = offset + len(kept)
    if len(kept) < len(rows) or oversized is not None:
        narrow = (
            f"page on with offset={offset + len(kept)}, pass a smaller limit, "
            'or use response_format "concise" with only the `fields` you need'
        )
        if oversized is not None:
            narrow = (
                f"record {oversized} alone is over the cap and is shown in its "
                "minimal concise form; read it with content_records_read, then "
                + narrow
            )
        meta["truncated"] = {
            "returned": len(kept),
            "dropped": len(rows) - len(kept),
            "maxResponseChars": cap,
            "narrowWith": narrow,
        }
    return shaped


def _fit_rows(rows: list, wrap, cap: int) -> list:
    """The longest prefix of `rows` whose wrapped response fits `cap`."""
    if _size(wrap(rows)) <= cap:
        return rows
    low, high = 0, len(rows)
    while low < high:
        mid = (low + high + 1) // 2
        if _size(wrap(rows[:mid])) <= cap:
            low = mid
        else:
            high = mid - 1
    return rows[:low]


def _fingerprint_sections(data: dict) -> dict[str, dict]:
    sections: dict[str, dict] = {}
    if isinstance(data.get("pages"), list):
        sections["pages"] = {"rows": len(data["pages"])}
    related = data.get("related")
    if isinstance(related, dict):
        for ref, section in related.items():
            if not isinstance(section, dict):
                continue
            records = section.get("records")
            summary = {"rows": len(records) if isinstance(records, list) else None}
            if "unresolved" in section:
                summary["unresolved"] = section["unresolved"]
            sections[ref] = summary
    return sections


def _shape_fingerprint(result: dict, options: dict, cap: int) -> dict:
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    shaped = dict(result)
    if options.get("response_format") == "concise":
        shaped["data"] = {
            "sections": _fingerprint_sections(data),
            "totals": data.get("totals"),
            "violations": data.get("violations"),
        }
        return shaped
    if _size(result) <= cap:
        return result

    # Over the cap: drop rows from the end of each row list, related
    # sections before pages, until it fits. totals and violations are never
    # touched.
    data = copy.deepcopy(data)
    shaped["data"] = data
    row_lists: list[tuple[str, list]] = []
    if isinstance(data.get("pages"), list):
        row_lists.append(("pages", data["pages"]))
    related = data.get("related")
    if isinstance(related, dict):
        for ref, section in related.items():
            if isinstance(section, dict) and isinstance(section.get("records"), list):
                row_lists.append((ref, section["records"]))
    original = {section: len(rows) for section, rows in row_lists}
    base_meta = shaped.get("meta")
    base_meta = dict(base_meta) if isinstance(base_meta, dict) else {}
    target = cap - _MARKER_RESERVE
    while True:
        for section, rows in reversed(row_lists):
            while rows and _size(shaped) > target:
                # Halve the overshoot per step instead of popping one row at a
                # time; a large snapshot can be thousands of rows over.
                over = _size(shaped) - target
                avg = max(_size(rows) // max(len(rows), 1), 1)
                drop = max(min(len(rows), over // avg // 2), 1)
                del rows[-drop:]
        marker: dict[str, Any] = {
            "sections": {
                section: {
                    "returned": len(rows),
                    "dropped": original[section] - len(rows),
                }
                for section, rows in row_lists
                if len(rows) < original[section]
            },
            "maxResponseChars": cap,
            "narrowWith": (
                'pass `classes` to request fewer sections, or response_format "concise" '
                "for counts only; totals and violations are complete"
            ),
        }
        rows_left = any(rows for _, rows in row_lists)
        if not rows_left:
            marker["narrowWith"] = (
                "every row was dropped and the response is still this size: "
                "totals and violations are never dropped, and `classes` does not "
                "restrict violations"
            )
        shaped["meta"] = {**base_meta, "truncated": marker}
        over = _size(shaped) - cap
        # The marker grows with each truncated section, so re-fit until the
        # whole response, marker included, is under the cap.
        if over <= 0 or not rows_left:
            return shaped
        target -= over


def concise_schema_site(data: dict) -> dict:
    concise = {k: v for k, v in data.items() if k not in ("classes", "crud")}
    groups: dict[str, list[str]] = {"pages": [], "elements": [], "other": []}
    classes = data.get("classes")
    if isinstance(classes, dict):
        for ref, info in classes.items():
            info = info if isinstance(info, dict) else {}
            if info.get("page"):
                groups["pages"].append(ref)
            elif info.get("element"):
                groups["elements"].append(ref)
            else:
                groups["other"].append(ref)
    concise["classes"] = groups
    return concise


def _shape_schema_site(result: dict, options: dict, cap: int) -> dict:
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    if options.get("response_format") == "concise":
        return {**result, "data": concise_schema_site(data)}
    if _size(result) <= cap:
        return result
    meta = dict(result.get("meta") or {})
    meta["truncated"] = {
        "replacedWith": "concise",
        "maxResponseChars": cap,
        "narrowWith": "call content_schema_class for one class's verbs and fields",
    }
    return {**result, "data": concise_schema_site(data), "meta": meta}
