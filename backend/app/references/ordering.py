"""Bibliography ordering comparator.

Shared by the Ask Bibliography_Block and the (stretch) export. A single sort
key implements the documented ordering rules; Python's stable sort applied
with this key yields the required total order.

Rules (see design "Alphabetical ordering and sorting rules", Req 6.8/6.9/10.3):

- Each orderable item has a ``leading_element`` (author surname else title,
  ``None`` when it has neither) and the full citation ``text``.
- Primary key: the leading element, case-folded via :func:`str.casefold`. The
  export variant additionally diacritic-folds (Unicode NFKD then drop combining
  marks) so "Éluard" and "Eluard" compare equal on the primary key. Select the
  variant with ``diacritic_insensitive``.
- Tie-break: the full citation ``text``, folded the same way. Because distinct
  texts fold to distinct-or-tie-broken strings, distinct citation texts never
  fully tie unless their texts are byte-identical.
- Entries with no leading element (``None`` or blank after folding) sort last,
  after every entry that has one, ordered among themselves by full text.

The resulting relation is a total order: every pair is comparable, and a stable
sort is a deterministic permutation of the input.
"""

from __future__ import annotations

import unicodedata
from typing import Callable, TypeVar

__all__ = ["sort_key", "order_bibliography"]

T = TypeVar("T")


def _fold(s: str, *, diacritics: bool) -> str:
    """Case-fold ``s``; optionally also strip diacritics (NFKD + drop marks).

    Always case-folds. When ``diacritics`` is true, first normalizes to NFKD
    and drops Unicode combining marks so accented and unaccented letters
    compare equal, then case-folds the result.
    """
    if diacritics:
        decomposed = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return s.casefold()


def sort_key(
    leading_element: str | None,
    text: str,
    *,
    diacritic_insensitive: bool = False,
) -> tuple[int, str, str]:
    """Return the sort key implementing the bibliography ordering rules.

    The returned tuple is ``(no_leading_flag, folded_leading, folded_text)``:

    - ``no_leading_flag``: ``0`` when the entry has a leading element, ``1``
      when it does not (``None`` or blank after stripping). ``1`` sorts after
      ``0``, so no-leading entries come last.
    - ``folded_leading``: the folded leading element (empty string when absent).
    - ``folded_text``: the folded full citation text, used as the tie-break.

    Folding is case-insensitive, and additionally diacritic-insensitive when
    ``diacritic_insensitive`` is set.
    """
    if leading_element is None:
        has_leading = False
        folded_leading = ""
    else:
        folded_leading = _fold(leading_element, diacritics=diacritic_insensitive)
        has_leading = bool(folded_leading.strip())
        if not has_leading:
            folded_leading = ""

    no_leading_flag = 0 if has_leading else 1
    folded_text = _fold(text, diacritics=diacritic_insensitive)
    return (no_leading_flag, folded_leading, folded_text)


def order_bibliography(
    items: list[T],
    key: Callable[[T], tuple[str | None, str]] = lambda x: (x.leading_element, x.text),
    *,
    diacritic_insensitive: bool = False,
) -> list[T]:
    """Return ``items`` sorted by the bibliography ordering rules.

    Generic over any item type. ``key`` extracts a ``(leading_element, text)``
    pair from each item; it defaults to reading ``.leading_element`` and
    ``.text`` attributes, which fits :class:`FormattedCitation`. Pass a custom
    extractor to order plain tuples, e.g.
    ``order_bibliography(rows, key=lambda r: (r[0], r[1]))``.

    The sort is stable, so items whose keys are equal keep their input order.
    """

    def _key(item: T) -> tuple[int, str, str]:
        leading_element, text = key(item)
        return sort_key(
            leading_element, text, diacritic_insensitive=diacritic_insensitive
        )

    return sorted(items, key=_key)
