// Pure client-side helpers for the Ask_View Bibliography_Block (task 13.2).
//
// Two responsibilities, both pure and deterministic so they are trivially
// unit- and property-testable with no network:
//
//   1. `distinctCitedSourcePaths` — collapses the streamed `citations` list
//      (which has one entry per cited chunk, so duplicates and null paths are
//      common) into the distinct set of cited `source_path`s, in stable
//      first-seen order. Citations with a null `source_path` are ignored.
//      This is the client-side mapping behind Property 16 (Req 6.4, 6.5).
//
//   2. `orderCitations` — a client-side mirror of the backend bibliography
//      comparator (`backend/app/references/ordering.py`, design "Alphabetical
//      ordering and sorting rules", Req 6.8/6.9). Orders formatted citations by
//      the leading element case-insensitively, tie-breaking on the full text,
//      and placing entries with no (or blank) leading element last. The order
//      is total and deterministic, and the sort is stable.

import { Citation, FormattedCitation } from "./api";

// Returns the distinct non-null `source_path`s among `citations`, each exactly
// once, in first-seen order. A citation whose `source_path` is null/undefined
// is skipped (it cannot be formatted, Req 6.4).
export function distinctCitedSourcePaths(citations: Citation[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of citations) {
    const p = c.source_path;
    if (p == null) continue;
    if (seen.has(p)) continue;
    seen.add(p);
    out.push(p);
  }
  return out;
}

// Case-insensitive fold used for both the primary key and the tie-break. This
// mirrors the backend's `str.casefold()` closely enough for the Ask block,
// which does not diacritic-fold (that variant is export-only).
function fold(s: string): string {
  return s.toLocaleLowerCase();
}

// Compares two folded strings with locale-aware, case-insensitive semantics.
// `localeCompare` with sensitivity "base" ignores case and diacritics for the
// comparison itself; we fold first so ties are computed the same way the
// backend does with casefold.
function compareFolded(a: string, b: string): number {
  return fold(a).localeCompare(fold(b), undefined, { sensitivity: "base" });
}

// True when a leading element counts as present (non-null and has at least one
// non-whitespace character). Blank-leading entries sort last, same as the
// backend.
function hasLeading(leading: string | null): boolean {
  return leading != null && leading.trim().length > 0;
}

// Returns a NEW array of `cits` sorted by the bibliography ordering rules:
//   - entries with a leading element come before entries without one;
//   - within each group, order case-insensitively by leading element;
//   - ties (including all no-leading entries) break on the full citation text.
// Total, deterministic, and idempotent (ordering an ordered list is a no-op).
export function orderCitations(cits: FormattedCitation[]): FormattedCitation[] {
  return [...cits].sort((a, b) => {
    const aHas = hasLeading(a.leading_element);
    const bHas = hasLeading(b.leading_element);
    // No-leading entries sort last.
    if (aHas !== bHas) return aHas ? -1 : 1;
    if (aHas && bHas) {
      const byLeading = compareFolded(a.leading_element as string, b.leading_element as string);
      if (byLeading !== 0) return byLeading;
    }
    // Tie-break (and the sole key for no-leading entries): full text.
    const byText = compareFolded(a.text, b.text);
    if (byText !== 0) return byText;
    // Final deterministic tie-break so distinct source_paths never compare
    // equal, keeping the order total.
    return a.source_path.localeCompare(b.source_path);
  });
}
