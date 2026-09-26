"""The Reference_Import parsers: external references → normalized intermediates.

A pure backend module (Req 8) that parses each of the four Import_Formats into a
common intermediate that maps onto ``Source_Metadata`` fields:

- **BibTeX**, **RIS**, and **CSL-JSON** are *structured*: each parser returns a
  list of :class:`ParsedEntry` in file order (a single-entry file yields a
  one-element list), or a :class:`ParseError`.
- **Verbatim** is a pre-formatted MLA/APA string: :func:`parse_verbatim`
  validates and wraps it as a :class:`ParsedVerbatim` (never parsed into
  fields), or returns a :class:`ParseError`.

Every parser is **pure and total**: it returns a normalized result or a typed
``ParseError`` and *never raises to the caller* (Req 8.3). All parsing is wrapped
in ``try/except`` so arbitrary text or decoded garbage bytes resolve to a
``ParseError`` rather than an uncontrolled exception. The API layer turns a
``ParseError`` into a clean 400.

The parsers are hand-rolled on the standard library (CSL-JSON via ``json``); no
new heavy dependencies. They are deliberately tolerant: unknown fields are
skipped, comments/whitespace/case variation are accepted, and anything that maps
onto our field set is captured while the rest is ignored.

Type inference (``source_type``) follows the design's mapping tables. When a
format supplies no type indicator, ``source_type`` is ``None`` so the API leaves
the source's existing type unchanged (Req 8.7). Any mappable field the entry
omits becomes ``None`` (persisted as absent, Req 8.6). Dates are normalized to
``YYYY-MM-DD`` at full precision, else stored at the precision the entry supplies
(``"2015"`` or ``"2015-06"``) with no fabricated month/day (Req 8.5).

``parse_verbatim`` accepts ``style`` as the string ``"MLA"`` or ``"APA"``. This
avoids an import cycle with the (parallel-authored) ``formatter`` module; the API
layer validates/normalizes styles at its boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Union


# --------------------------------------------------------------------------- #
# Intermediates
# --------------------------------------------------------------------------- #


@dataclass
class ParsedEntry:
    """One structured entry mapped onto ``Source_Metadata`` fields.

    A field is ``None`` when the entry does not supply it (the store persists it
    as absent, Req 8.6). ``source_type`` is ``None`` when the format gives no
    type indicator, signalling the API to leave the existing type unchanged
    (Req 8.7). ``authors`` is an ordered list that may be empty.
    """

    key: Optional[str]  # BibTeX cite key / RIS 1-based index / CSL id or index
    source_type: Optional[str]  # inferred; None => leave existing unchanged
    authors: list[str]
    title: Optional[str]
    container: Optional[str]
    publisher: Optional[str]
    publication_date: Optional[str]  # YYYY-MM-DD when precise, else as given
    url: Optional[str]


@dataclass
class ParsedVerbatim:
    """A pre-formatted citation string stored verbatim as a per-style override."""

    style: str  # "MLA" | "APA"
    text: str


@dataclass
class ParseError:
    """A typed, non-raising parse failure the API converts into a 400."""

    format: str  # the declared format, echoed in the 400 message
    message: str  # human-readable reason


StructuredResult = Union[list[ParsedEntry], ParseError]
VerbatimResult = Union[ParsedVerbatim, ParseError]


# --------------------------------------------------------------------------- #
# Type-inference tables (entry type indicator -> source_type)
# --------------------------------------------------------------------------- #

_BIBTEX_TYPE_MAP = {
    "article": "article",
    "book": "book",
    "inbook": "book",
    "booklet": "book",
    "techreport": "report",
    "report": "report",
    "misc": "website",
    "online": "website",
    "electronic": "website",
}

_RIS_TYPE_MAP = {
    "JOUR": "article",
    "BOOK": "book",
    "RPRT": "report",
    "ELEC": "website",
    "WEB": "website",
}

_CSL_TYPE_MAP = {
    "article-journal": "article",
    "book": "book",
    "report": "report",
    "webpage": "website",
}


def _infer_bibtex_type(entry_type: Optional[str]) -> Optional[str]:
    if entry_type is None:
        return None
    return _BIBTEX_TYPE_MAP.get(entry_type.strip().lower(), "other")


def _infer_ris_type(ty: Optional[str]) -> Optional[str]:
    if ty is None:
        return None
    return _RIS_TYPE_MAP.get(ty.strip().upper(), "other")


def _infer_csl_type(csl_type: Optional[str]) -> Optional[str]:
    if csl_type is None:
        return None
    return _CSL_TYPE_MAP.get(str(csl_type).strip().lower(), "other")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean(value: Optional[str]) -> Optional[str]:
    """Trim whitespace and return ``None`` for empty/absent values."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed if trimmed else None


def _strip_bibtex_delims(value: str) -> str:
    """Strip a single layer of surrounding ``{...}`` braces or ``"..."`` quotes."""
    v = value.strip()
    while len(v) >= 2 and (
        (v[0] == "{" and v[-1] == "}") or (v[0] == '"' and v[-1] == '"')
    ):
        v = v[1:-1].strip()
    return v


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except Exception:
        return None


def _month_to_int(value: Optional[str]) -> Optional[int]:
    """Map a month name (``"jan"``) or number (``"1"``, ``"01"``) to 1..12."""
    if value is None:
        return None
    token = value.strip().lower()
    if not token:
        return None
    if token in _MONTH_NAMES:
        return _MONTH_NAMES[token]
    n = _to_int(token)
    if n is not None and 1 <= n <= 12:
        return n
    return None


def _normalize_ymd(
    year: Optional[int], month: Optional[int], day: Optional[int]
) -> Optional[str]:
    """Assemble a date string at the precision available.

    - year + month + day -> ``"YYYY-MM-DD"`` (zero-padded)
    - year + month       -> ``"YYYY-MM"``
    - year               -> ``"YYYY"``
    - no year            -> ``None``

    Never fabricates a month or day; a month/day without a year is dropped.
    """
    if year is None:
        return None
    if month is None:
        return f"{year:04d}"
    if day is None:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def _normalize_date_string(raw: Optional[str]) -> Optional[str]:
    """Normalize a free-form date string (e.g. RIS ``DA`` / BibTeX ``date``).

    Accepts common separators (``-`` / ``/`` / ``.``) and preserves precision:
    a bare year stays a year, ``YYYY/MM`` becomes ``YYYY-MM``, and a full date
    becomes ``YYYY-MM-DD``. Falls back to the trimmed original if it cannot be
    interpreted, so unusual values still round-trip rather than being dropped.
    """
    cleaned = _clean(raw)
    if cleaned is None:
        return None

    # Split on the first run of any common date separator.
    parts: list[str] = []
    current = ""
    for ch in cleaned:
        if ch in "-/. ":
            if current:
                parts.append(current)
                current = ""
        else:
            current += ch
    if current:
        parts.append(current)

    if not parts:
        return cleaned

    year = _to_int(parts[0]) if len(parts) >= 1 else None
    if year is None:
        # Not a leading numeric year; keep the value as given.
        return cleaned

    month = _month_to_int(parts[1]) if len(parts) >= 2 else None
    day = _to_int(parts[2]) if len(parts) >= 3 else None
    if day is not None and not (1 <= day <= 31):
        day = None

    normalized = _normalize_ymd(year, month, day)
    return normalized if normalized is not None else cleaned


# --------------------------------------------------------------------------- #
# BibTeX
# --------------------------------------------------------------------------- #


def _split_bibtex_entries(text: str) -> list[tuple[str, str, str]]:
    """Split BibTeX text into ``(entry_type, cite_key, body)`` tuples.

    A tolerant, brace-aware scanner: it finds each ``@type{key, ... }`` block by
    tracking brace depth so nested braces in field values do not end the entry
    early. ``@comment``/``@string``/``@preamble`` blocks are skipped. Malformed
    trailing blocks are ignored rather than raising.
    """
    entries: list[tuple[str, str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "@":
            i += 1
            continue
        # Read the entry type up to the opening brace/paren.
        j = i + 1
        while j < n and text[j] not in "{(":
            j += 1
        if j >= n:
            break
        entry_type = text[i + 1 : j].strip().lower()
        opener = text[j]
        closer = "}" if opener == "{" else ")"

        # Scan the balanced body.
        depth = 1
        k = j + 1
        while k < n and depth > 0:
            c = text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif opener == "(" and c == closer and depth == 1:
                depth = 0
                break
            k += 1
        body = text[j + 1 : k if depth == 0 else n]
        i = k + 1

        if entry_type in ("comment", "string", "preamble"):
            continue

        # First comma separates the cite key from the field list.
        comma = body.find(",")
        if comma == -1:
            cite_key = body.strip()
            fields_str = ""
        else:
            cite_key = body[:comma].strip()
            fields_str = body[comma + 1 :]
        entries.append((entry_type, cite_key, fields_str))
    return entries


def _parse_bibtex_fields(fields_str: str) -> dict[str, str]:
    """Parse a BibTeX field list ``name = value, name = value`` into a dict.

    Brace/quote-aware so commas inside ``{...}`` or ``"..."`` values do not split
    fields. Field names are lower-cased; values keep a single layer of delimiter
    stripping. Unknown/garbled fragments are skipped tolerantly.
    """
    fields: dict[str, str] = {}
    i = 0
    n = len(fields_str)
    while i < n:
        # Field name up to '='.
        eq = fields_str.find("=", i)
        if eq == -1:
            break
        name = fields_str[i:eq].strip().strip(",").lower()
        # Value: skip whitespace, then read a brace/quote-balanced token.
        v = eq + 1
        while v < n and fields_str[v] in " \t\r\n":
            v += 1
        if v >= n:
            break
        value_chars: list[str] = []
        if fields_str[v] in '{"':
            opener = fields_str[v]
            closer = "}" if opener == "{" else '"'
            depth = 1
            value_chars.append(fields_str[v])
            v += 1
            while v < n and depth > 0:
                c = fields_str[v]
                if opener == "{" and c == "{":
                    depth += 1
                elif opener == "{" and c == "}":
                    depth -= 1
                elif opener == '"' and c == '"':
                    depth -= 1
                value_chars.append(c)
                v += 1
            raw_value = "".join(value_chars)
        else:
            # Bare value up to the next top-level comma.
            while v < n and fields_str[v] != ",":
                value_chars.append(fields_str[v])
                v += 1
            raw_value = "".join(value_chars)
        # Advance past a trailing comma.
        while v < n and fields_str[v] in " \t\r\n":
            v += 1
        if v < n and fields_str[v] == ",":
            v += 1
        i = v

        if name:
            fields[name] = _strip_bibtex_delims(raw_value)
    return fields


def _bibtex_authors(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    parts = raw.split(" and ")
    authors = [_strip_bibtex_delims(p).strip() for p in parts]
    return [a for a in authors if a]


def _bibtex_date(fields: dict[str, str]) -> Optional[str]:
    if "date" in fields:
        normalized = _normalize_date_string(fields["date"])
        if normalized is not None:
            return normalized
    year = _to_int(fields.get("year"))
    if year is None:
        return None
    month = _month_to_int(fields.get("month"))
    day = _to_int(fields.get("day"))
    if day is not None and not (1 <= day <= 31):
        day = None
    return _normalize_ymd(year, month, day)


def parse_bibtex(text: str) -> StructuredResult:
    """Parse BibTeX text into a list of :class:`ParsedEntry`, or a ParseError.

    Total: any exception resolves to a ``ParseError`` (Req 8.3). An input with no
    recognizable entries is a ``ParseError`` (nothing to import).
    """
    try:
        raw_entries = _split_bibtex_entries(text)
        entries: list[ParsedEntry] = []
        for idx, (entry_type, cite_key, fields_str) in enumerate(raw_entries):
            fields = _parse_bibtex_fields(fields_str)
            key = cite_key.strip() if cite_key.strip() else str(idx + 1)
            container = _clean(fields.get("journal")) or _clean(fields.get("booktitle"))
            url = _clean(fields.get("url")) or _clean(fields.get("doi"))
            entries.append(
                ParsedEntry(
                    key=key,
                    source_type=_infer_bibtex_type(entry_type),
                    authors=_bibtex_authors(fields.get("author")),
                    title=_clean(fields.get("title")),
                    container=container,
                    publisher=_clean(fields.get("publisher")),
                    publication_date=_bibtex_date(fields),
                    url=url,
                )
            )
        if not entries:
            return ParseError(
                format="bibtex",
                message="No BibTeX entries found.",
            )
        return entries
    except Exception as exc:  # totality: never raise to the caller
        return ParseError(format="bibtex", message=f"Could not parse BibTeX: {exc}")


# --------------------------------------------------------------------------- #
# RIS
# --------------------------------------------------------------------------- #


def _parse_ris_line(line: str) -> Optional[tuple[str, str]]:
    """Parse an ``"XX  - value"`` RIS line into ``(tag, value)``.

    Tolerant of spacing around the dash. Returns ``None`` for lines that do not
    look like a tagged RIS line (continuation/blank lines).
    """
    if len(line) < 2:
        return None
    tag = line[:2]
    if not (tag[0].isalpha() and (tag[1].isalnum())):
        return None
    rest = line[2:]
    # Expect optional spaces then a dash separator.
    stripped = rest.lstrip()
    if not stripped.startswith("-"):
        return None
    value = stripped[1:].strip()
    return tag.upper(), value


def parse_ris(text: str) -> StructuredResult:
    """Parse RIS text into a list of :class:`ParsedEntry`, or a ParseError.

    Records start at a ``TY  - X`` tag and end at ``ER  -``. Repeatable author
    tags (``AU``/``A1``) accumulate. Total: any exception -> ``ParseError``
    (Req 8.3); zero records -> ``ParseError``.
    """
    try:
        records: list[dict[str, list[str]]] = []
        current: Optional[dict[str, list[str]]] = None
        for raw_line in text.splitlines():
            parsed = _parse_ris_line(raw_line)
            if parsed is None:
                continue
            tag, value = parsed
            if tag == "TY":
                current = {"TY": [value]}
                records.append(current)
                continue
            if current is None:
                # A tag before any TY: start a lenient record so nothing is lost.
                current = {}
                records.append(current)
            if tag == "ER":
                current = None
                continue
            current.setdefault(tag, []).append(value)

        entries: list[ParsedEntry] = []
        for idx, record in enumerate(records):
            authors = [
                a.strip()
                for a in (record.get("AU", []) + record.get("A1", []))
                if a.strip()
            ]
            title = _first_nonempty(record, ["TI", "T1"])
            container = _first_nonempty(record, ["JO", "JF", "T2"])
            publisher = _first_nonempty(record, ["PB"])
            url = _first_nonempty(record, ["UR"])
            date_raw = _first_nonempty(record, ["DA", "PY", "Y1"])
            ty = record.get("TY", [None])[0]
            entries.append(
                ParsedEntry(
                    key=str(idx + 1),
                    source_type=_infer_ris_type(ty),
                    authors=authors,
                    title=title,
                    container=container,
                    publisher=publisher,
                    publication_date=_normalize_date_string(date_raw),
                    url=url,
                )
            )
        if not entries:
            return ParseError(format="ris", message="No RIS records found.")
        return entries
    except Exception as exc:  # totality
        return ParseError(format="ris", message=f"Could not parse RIS: {exc}")


def _first_nonempty(record: dict[str, list[str]], tags: list[str]) -> Optional[str]:
    for tag in tags:
        for value in record.get(tag, []):
            cleaned = _clean(value)
            if cleaned is not None:
                return cleaned
    return None


# --------------------------------------------------------------------------- #
# CSL-JSON
# --------------------------------------------------------------------------- #


def _csl_authors(author_list: object) -> list[str]:
    if not isinstance(author_list, list):
        return []
    out: list[str] = []
    for a in author_list:
        if not isinstance(a, dict):
            continue
        family = a.get("family")
        given = a.get("given")
        family = family.strip() if isinstance(family, str) else None
        given = given.strip() if isinstance(given, str) else None
        if family and given:
            out.append(f"{family}, {given}")
        elif family:
            out.append(family)
        elif given:
            out.append(given)
    return out


def _csl_date(issued: object) -> Optional[str]:
    if not isinstance(issued, dict):
        return None
    date_parts = issued.get("date-parts")
    if not isinstance(date_parts, list) or not date_parts:
        return None
    first = date_parts[0]
    if not isinstance(first, list) or not first:
        return None
    year = _to_int_any(first[0]) if len(first) >= 1 else None
    month = _to_int_any(first[1]) if len(first) >= 2 else None
    day = _to_int_any(first[2]) if len(first) >= 3 else None
    if month is not None and not (1 <= month <= 12):
        month = None
        day = None
    if day is not None and not (1 <= day <= 31):
        day = None
    return _normalize_ymd(year, month, day)


def _to_int_any(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _to_int(value)
    if isinstance(value, float):
        return int(value)
    return None


def _csl_str(value: object) -> Optional[str]:
    if isinstance(value, str):
        return _clean(value)
    return None


def parse_csljson(text: str) -> StructuredResult:
    """Parse CSL-JSON text into a list of :class:`ParsedEntry`, or a ParseError.

    Accepts a single object or an array of objects. Total: any exception (incl.
    invalid JSON) -> ``ParseError`` (Req 8.3); zero entries -> ``ParseError``.
    """
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            return ParseError(
                format="csljson",
                message="CSL-JSON must be an object or an array of objects.",
            )

        entries: list[ParsedEntry] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            csl_id = item.get("id")
            if isinstance(csl_id, (str, int)) and str(csl_id).strip():
                key = str(csl_id).strip()
            else:
                key = str(idx + 1)
            entries.append(
                ParsedEntry(
                    key=key,
                    source_type=_infer_csl_type(item.get("type")),
                    authors=_csl_authors(item.get("author")),
                    title=_csl_str(item.get("title")),
                    container=_csl_str(item.get("container-title")),
                    publisher=_csl_str(item.get("publisher")),
                    publication_date=_csl_date(item.get("issued")),
                    url=_csl_str(item.get("URL")),
                )
            )
        if not entries:
            return ParseError(format="csljson", message="No CSL-JSON entries found.")
        return entries
    except Exception as exc:  # totality (incl. json.JSONDecodeError)
        return ParseError(format="csljson", message=f"Could not parse CSL-JSON: {exc}")


# --------------------------------------------------------------------------- #
# Verbatim
# --------------------------------------------------------------------------- #

_VALID_STYLES = ("MLA", "APA")


def parse_verbatim(text: str, style: str) -> VerbatimResult:
    """Validate and wrap a pre-formatted citation as a :class:`ParsedVerbatim`.

    Not parsed into fields (Req 8.9): the text is stored verbatim as a per-style
    override. ``style`` is validated against ``{"MLA", "APA"}`` and ``text`` must
    contain at least one non-whitespace character. Total: never raises.
    """
    try:
        if style not in _VALID_STYLES:
            return ParseError(
                format="verbatim",
                message="style must be one of MLA, APA.",
            )
        if text is None or not text.strip():
            return ParseError(format="verbatim", message="empty citation")
        # Preserve internal content; trim only a trailing newline artifact.
        stored = text.rstrip("\n") if text.endswith("\n") else text
        return ParsedVerbatim(style=style, text=stored)
    except Exception as exc:  # totality
        return ParseError(
            format="verbatim", message=f"Could not accept verbatim citation: {exc}"
        )


# --------------------------------------------------------------------------- #
# Dispatch helper
# --------------------------------------------------------------------------- #

_STRUCTURED_PARSERS = {
    "bibtex": parse_bibtex,
    "ris": parse_ris,
    "csljson": parse_csljson,
}


def parse_structured(fmt: str, text: str) -> StructuredResult:
    """Dispatch to the structured parser for ``fmt`` (``bibtex``/``ris``/``csljson``).

    Returns a ``ParseError`` for an unknown format. Verbatim is intentionally not
    handled here (it has a different signature and result type).
    """
    parser = _STRUCTURED_PARSERS.get((fmt or "").strip().lower())
    if parser is None:
        return ParseError(format=fmt, message=f"Unknown structured format: {fmt!r}")
    return parser(text)
