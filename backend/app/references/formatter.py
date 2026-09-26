"""The Citation_Formatter: a pure, deterministic, offline component.

Given one effective ``SourceMetadata`` record and one ``CitationStyle``, it
assembles a single formatted citation string plus completeness metadata. There
is no LLM, no network, no clock and no randomness here, so it runs identically
on the CPU-only laptop and is fully property-testable. ``access_date``'s
"today" default is resolved upstream (the store, on creation), so the formatter
is a pure function of ``(SourceMetadata, style)``.

The formatter operates on an *effective* record: callers pass the output of
``effective_record(...)``, so the title has already fallen back to a file name
when absent (Req 3.1). Even so, the formatter keeps a minimal internal
guarantee that ``text`` is never empty (Req 3.7): if the title ends up blank it
falls back to the source-path basename with its extension removed.

The rendered MLA 9th / APA 7th strings are *pragmatic* renderings: element
selection, ordering and the documented date rules are exact and deterministic
(what the property tests depend on), but the fine bibliographic punctuation is
kept simple rather than perfect — this is a local text app with no markup.

Logic order in ``format_citation``:

1. Verbatim-override short-circuit (Req 8.10): if the record carries a
   non-empty override for the requested style, return it byte-for-byte, marked
   complete, with a ``leading_element`` derived from the override's leading
   token. The other style is unaffected and falls through to field assembly.
2. Otherwise field-based assembly (MLA or APA), computing ``missing_required``
   (including the MLA website access-date rule, Req 3.4) and ``leading_element``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .store import SourceMetadata


class CitationStyle(str, Enum):
    MLA = "MLA"
    APA = "APA"


@dataclass
class FormattedCitation:
    source_path: str
    style: CitationStyle
    text: str  # never empty (Req 3.7)
    missing_required: list[str] = field(default_factory=list)
    incomplete: bool = False
    leading_element: Optional[str] = None  # author surname else title; None if neither


# Base required-field sets per source_type (Req 2.3-2.7). A field is "present"
# only if it has >=1 non-whitespace character (for authors, >=1 such entry).
_REQUIRED_BY_TYPE: dict[str, set[str]] = {
    "book": {"title", "authors"},
    "article": {"title", "authors", "container"},
    "website": {"title", "url"},
    "report": {"title"},
    "other": {"title"},
}


def required_fields(source_type: str) -> set[str]:
    """Return the base fields required for a *complete* citation of this type.

    Encodes Req 2.3-2.7. Any unknown/absent ``source_type`` is treated as
    ``"other"`` -> ``{"title"}``. This is the base set; the MLA website
    access-date rule (Req 3.4) is applied inside ``format_citation`` because it
    depends on style as well as type.
    """
    key = source_type if isinstance(source_type, str) else "other"
    return set(_REQUIRED_BY_TYPE.get(key, {"title"}))


# -- presence helpers ------------------------------------------------------
def _present(value: Optional[str]) -> bool:
    """A scalar field is present iff it has >=1 non-whitespace character."""
    return isinstance(value, str) and bool(value.strip())


def _authors_present(authors: list[str]) -> bool:
    """``authors`` is present iff >=1 entry has a non-whitespace character."""
    if not authors:
        return False
    return any(isinstance(a, str) and a.strip() for a in authors)


def _clean_authors(authors: list[str]) -> list[str]:
    """Return the non-blank author entries, trimmed, in order."""
    out: list[str] = []
    for a in authors:
        if isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def _surname(author: str) -> str:
    """Best-effort surname for an author entry.

    Accepts either ``"Surname, First"`` (comma form) or ``"First Surname"``
    (space form). For the comma form the surname is the text before the first
    comma; for the space form it is the final whitespace-separated token.
    Returns the whole (trimmed) string when it has no separators.
    """
    a = author.strip()
    if "," in a:
        return a.split(",", 1)[0].strip()
    parts = a.split()
    if parts:
        return parts[-1]
    return a


def _given(author: str) -> str:
    """Best-effort given-name portion, complementing ``_surname``."""
    a = author.strip()
    if "," in a:
        return a.split(",", 1)[1].strip()
    parts = a.split()
    if len(parts) > 1:
        return " ".join(parts[:-1])
    return ""


def _effective_title(record: SourceMetadata) -> str:
    """The title to render, guaranteed non-empty (Req 3.1 / 3.7).

    Callers pass an effective record, so ``record.title`` is normally already
    set. This is a defensive floor: if the title is blank, fall back to the
    ``source_path`` basename with its extension removed; if even that is empty,
    fall back to the raw ``source_path``.
    """
    if _present(record.title):
        return record.title.strip() if record.title else record.title
    base = os.path.basename(record.source_path or "")
    root, _ext = os.path.splitext(base)
    if root.strip():
        return root
    if (record.source_path or "").strip():
        return record.source_path
    return "Untitled"


def _year(publication_date: Optional[str]) -> Optional[str]:
    """Extract a display year from a (possibly partial) publication date.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM`` or ``YYYY`` (imported partial dates).
    Returns the leading 4-digit year when present, else the raw trimmed value,
    else ``None``. Never raises on odd strings.
    """
    if not _present(publication_date):
        return None
    raw = publication_date.strip()
    head = raw.split("-", 1)[0]
    if len(head) == 4 and head.isdigit():
        return head
    return raw


# -- override leading element ---------------------------------------------
def _override_leading_element(text: str) -> Optional[str]:
    """Derive the ordering key from a verbatim override's leading token.

    Take the substring up to (but not including) the first period or comma,
    trimmed. Returns ``None`` when that leading token is empty.
    """
    cut = len(text)
    for i, ch in enumerate(text):
        if ch == "." or ch == ",":
            cut = i
            break
    token = text[:cut].strip()
    return token or None


# -- element assembly ------------------------------------------------------
def _join_period(parts: list[str]) -> str:
    """Join non-empty parts with ". " and end with a single period."""
    kept = [p for p in parts if p]
    if not kept:
        return ""
    joined = ". ".join(kept)
    if not joined.endswith("."):
        joined += "."
    return joined


def _format_mla(record: SourceMetadata) -> str:
    """Pragmatic MLA 9th rendering.

    Shape: ``Author. Title. Container, Publisher, Date, URL. Accessed <date>.``
    Only present elements are included. When there are no authors the title
    takes the author position and no placeholder is emitted (Req 3.3). An
    absent date is omitted except the website access-date rule, which appends
    ``Accessed <access_date>.`` for an undated website when an access date is
    present.
    """
    authors = _clean_authors(record.authors)
    title = _effective_title(record)

    segments: list[str] = []

    # Author position (or title in that position when no authors).
    if authors:
        rendered: list[str] = []
        for i, a in enumerate(authors):
            surname = _surname(a)
            given = _given(a)
            if i == 0:
                # First author: "Surname, First".
                rendered.append(f"{surname}, {given}".strip().rstrip(","))
            else:
                # Subsequent authors: "First Surname".
                rendered.append(f"{given} {surname}".strip())
        author_text = ", ".join(rendered)
        segments.append(author_text)
        segments.append(title)
    else:
        # Title takes the author position; no placeholder author text.
        segments.append(title)

    # Container, publisher, date, url as a comma-joined tail segment.
    tail: list[str] = []
    if _present(record.container):
        tail.append(record.container.strip())
    if _present(record.publisher):
        tail.append(record.publisher.strip())
    date_year = record.publication_date.strip() if _present(record.publication_date) else None
    if date_year:
        tail.append(date_year)
    if _present(record.url):
        tail.append(record.url.strip())
    if tail:
        segments.append(", ".join(tail))

    text = _join_period(segments)

    # Website access-date rule: for an undated website, append the access date.
    if (
        record.source_type == "website"
        and not _present(record.publication_date)
        and _present(record.access_date)
    ):
        text = f"{text} Accessed {record.access_date.strip()}."

    return text or title


def _format_apa(record: SourceMetadata) -> str:
    """Pragmatic APA 7th rendering.

    Shape: ``Author (Year). Title. Container. Publisher. URL`` with a literal
    ``(n.d.)`` when there is no publication date (Req 3.2). When there are no
    authors the title takes the author position and no placeholder is emitted
    (Req 3.3).
    """
    authors = _clean_authors(record.authors)
    title = _effective_title(record)

    year = _year(record.publication_date)
    date_token = f"({year})" if year else "(n.d.)"

    segments: list[str] = []

    if authors:
        rendered: list[str] = []
        for a in authors:
            surname = _surname(a)
            given = _given(a)
            if given:
                # Initials from the given-name tokens: "F. M.".
                initials = " ".join(
                    f"{tok[0].upper()}." for tok in given.split() if tok
                )
                rendered.append(f"{surname}, {initials}".strip())
            else:
                rendered.append(surname)
        author_text = ", ".join(rendered)
        # "Author (Year)." as the leading segment.
        segments.append(f"{author_text} {date_token}")
        segments.append(title)
    else:
        # Title occupies the author position, followed by the date token.
        segments.append(f"{title} {date_token}")

    if _present(record.container):
        segments.append(record.container.strip())
    if _present(record.publisher):
        segments.append(record.publisher.strip())
    if _present(record.url):
        segments.append(record.url.strip())

    text = _join_period(segments)
    return text or title


# -- public entry point ----------------------------------------------------
def format_citation(
    record: SourceMetadata, style: CitationStyle
) -> FormattedCitation:
    """Format one citation for one source in one style.

    Pure, deterministic and total: no clock, no I/O, no randomness, and never
    raises. The output ``text`` is never empty (Req 3.7).

    1. Verbatim-override short-circuit (Req 8.10): a non-empty override for the
       requested style is returned byte-for-byte, marked complete.
    2. Otherwise field-based assembly, computing ``missing_required``
       (including the MLA website access-date rule, Req 3.4) and
       ``leading_element`` (author surname else effective title).
    """
    style_key = style.value if isinstance(style, CitationStyle) else str(style)

    # 1) Verbatim-override short-circuit (Req 8.10).
    overrides = record.verbatim_overrides or {}
    override_text = overrides.get(style_key)
    if isinstance(override_text, str) and override_text.strip():
        return FormattedCitation(
            source_path=record.source_path,
            style=style,
            text=override_text,  # byte-for-byte
            missing_required=[],
            incomplete=False,
            leading_element=_override_leading_element(override_text),
        )

    # 2) Field-based assembly.
    if style == CitationStyle.APA:
        text = _format_apa(record)
    else:
        text = _format_mla(record)

    # Defensive non-empty guarantee (Req 3.7).
    if not text or not text.strip():
        text = _effective_title(record)

    # missing_required: base set minus present fields.
    missing: list[str] = []
    required = required_fields(record.source_type)
    # Deterministic, stable ordering for the property tests.
    for f in ("title", "authors", "container", "publisher", "url"):
        if f not in required:
            continue
        if f == "authors":
            if not _authors_present(record.authors):
                missing.append("authors")
        else:
            if not _present(getattr(record, f, None)):
                missing.append(f)

    # MLA website access-date rule (Req 3.4): an undated website in MLA must
    # include the access date; if it too is absent, access_date is missing.
    if (
        style == CitationStyle.MLA
        and record.source_type == "website"
        and not _present(record.publication_date)
        and not _present(record.access_date)
    ):
        if "access_date" not in missing:
            missing.append("access_date")

    # leading_element: author surname else effective title; None if neither.
    authors = _clean_authors(record.authors)
    if authors:
        leading = _surname(authors[0])
    else:
        eff_title = _effective_title(record)
        leading = eff_title if eff_title.strip() else None

    return FormattedCitation(
        source_path=record.source_path,
        style=style,
        text=text,
        missing_required=missing,
        incomplete=bool(missing),
        leading_element=leading,
    )
