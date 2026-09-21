"""Property-based tests for the QueryScope value object.

Covers task 2.2 of the source-selection-and-groups spec:
  - Property 7: Selection is treated as a set (duplicates do not matter).
  - Property 3 (scope side): an empty scope is distinct from unscoped.

The distinction that anchors QueryScope is:
    selection is None        -> whole archive (unscoped)
    selection == frozenset() -> explicit empty scope (zero results)
    selection == {...}       -> restricted to those source_path values
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.search.scope import QueryScope


# A source_path-like string. Kept small so duplicates are actually generated
# with reasonable frequency, which is the point of the Property 7 tests.
source_paths = st.text(
    alphabet="abcde/._", min_size=0, max_size=6
)


def _with_duplicates(values: list[str]) -> list[str]:
    """Return ``values`` with every element repeated, preserving order.

    Duplicating every element guarantees the resulting list contains
    duplicates whenever the input is non-empty, so the dedup property is
    exercised meaningfully.
    """
    duplicated: list[str] = []
    for v in values:
        duplicated.append(v)
        duplicated.append(v)
    return duplicated


@settings(max_examples=100)
@given(st.lists(source_paths))
def test_selection_is_a_set_duplicates_do_not_matter(sources: list[str]) -> None:
    """Feature: source-selection-and-groups, Property 7: Selection is treated as a set (duplicates do not matter).

    ``QueryScope.of`` must collapse duplicates by construction, so a list and
    that same list with duplicates injected produce identical selections, and
    the selection equals ``frozenset`` of the input.
    """
    with_dups = _with_duplicates(sources)

    from_original = QueryScope.of(sources)
    from_dups = QueryScope.of(with_dups)

    # Duplicates do not matter: both selections are identical.
    assert from_original.selection == from_dups.selection
    # And the selection is exactly the set of the input source paths.
    assert from_original.selection == frozenset(sources)
    assert from_dups.selection == frozenset(sources)


@settings(max_examples=100)
@given(st.lists(source_paths, min_size=1))
def test_explicit_dup_list_still_matches_deduped_list(sources: list[str]) -> None:
    """Feature: source-selection-and-groups, Property 7: Selection is treated as a set (duplicates do not matter).

    For any non-empty list, building a scope from the list with duplicates
    removed yields the same selection as building it from the raw list. This
    checks the property against a genuinely dup-free reference list rather than
    only the every-element-doubled construction.
    """
    deduped = list(dict.fromkeys(sources))  # order-preserving unique
    raw_with_dups = _with_duplicates(sources)

    assert QueryScope.of(raw_with_dups).selection == QueryScope.of(deduped).selection
    assert QueryScope.of(deduped).selection == frozenset(sources)


def test_whole_archive_is_unscoped() -> None:
    """Feature: source-selection-and-groups, Property 3: Empty scope is distinct from unscoped.

    ``whole_archive()`` carries no selection: it is unscoped and its selection
    is ``None`` (the whole-archive sentinel), never an empty frozenset.
    """
    scope = QueryScope.whole_archive()
    assert scope.is_unscoped is True
    assert scope.selection is None


def test_empty_selection_is_scoped_and_empty() -> None:
    """Feature: source-selection-and-groups, Property 3: Empty scope is distinct from unscoped.

    An explicit empty selection is *not* unscoped: it is a zero-results scope
    whose selection is an empty frozenset, distinct from the ``None`` sentinel.
    """
    scope = QueryScope.of([])
    assert scope.is_unscoped is False
    assert scope.selection == frozenset()
    # The empty scope must be distinguishable from whole-archive.
    assert scope.selection is not None
    assert scope != QueryScope.whole_archive()


@settings(max_examples=100)
@given(st.lists(source_paths, min_size=1))
def test_non_empty_selection_is_never_unscoped(sources: list[str]) -> None:
    """Feature: source-selection-and-groups, Property 3: Empty scope is distinct from unscoped.

    For any non-empty list of sources, the resulting scope is scoped (not
    unscoped) and its selection is a non-None frozenset.
    """
    scope = QueryScope.of(sources)
    assert scope.is_unscoped is False
    assert scope.selection is not None
    assert scope.selection == frozenset(sources)
