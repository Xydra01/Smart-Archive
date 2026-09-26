// Feature: citation-formatting, Property 16 / Property 17 (client mirror)
//
// Tests the pure client-side helpers behind the Ask_View Bibliography_Block:
//   - distinctCitedSourcePaths — Property 16 (Req 6.4, 6.5): one entry per
//     distinct cited source, null paths ignored.
//   - orderCitations — client mirror of Property 17 (Req 6.8, 6.9): leading
//     element case-insensitively, tie-break on full text, no-leading last;
//     total, deterministic, idempotent.

import { describe, it, expect } from "vitest";
import { Citation, FormattedCitation } from "./api";
import { distinctCitedSourcePaths, orderCitations } from "./bibliography";

// Minimal Citation factory: only the fields the helpers read matter.
function cite(source_path: string | null, index = 0): Citation {
  return {
    index,
    source_file: source_path ? source_path.split("/").pop()! : "unknown",
    source_path,
    location: "",
    file_type: null,
    matched_by: [],
    rrf_score: null,
  };
}

function fc(
  source_path: string,
  text: string,
  leading_element: string | null,
  incomplete = false
): FormattedCitation {
  return { source_path, text, leading_element, incomplete, missing_required: [] };
}

describe("distinctCitedSourcePaths — Property 16 (Req 6.4, 6.5)", () => {
  it("returns [] for no citations", () => {
    expect(distinctCitedSourcePaths([])).toEqual([]);
  });

  it("ignores citations with a null source_path", () => {
    const list = [cite(null), cite(null, 1)];
    expect(distinctCitedSourcePaths(list)).toEqual([]);
  });

  it("collapses duplicate source_paths to one entry, first-seen order", () => {
    const list = [
      cite("/a.pdf"),
      cite("/b.pdf"),
      cite("/a.pdf"), // duplicate
      cite(null),
      cite("/c.txt"),
      cite("/b.pdf"), // duplicate
    ];
    expect(distinctCitedSourcePaths(list)).toEqual(["/a.pdf", "/b.pdf", "/c.txt"]);
  });

  // Light property-style loop: for arrays built with intentional duplicates,
  // nulls, and varied order, the result must equal the distinct set of
  // non-null paths (each once) and preserve first-seen order.
  it("holds over many generated citation lists (uniqueness + membership)", () => {
    const universe = ["/x.pdf", "/y.pdf", "/z.pdf", "/w.md", "/v.epub"];
    // Deterministic PRNG so failures reproduce.
    let seed = 1234567;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
    for (let iter = 0; iter < 200; iter++) {
      const n = Math.floor(rand() * 12);
      const list: Citation[] = [];
      for (let i = 0; i < n; i++) {
        // ~25% chance of a null-path citation.
        if (rand() < 0.25) {
          list.push(cite(null, i));
        } else {
          const p = universe[Math.floor(rand() * universe.length)];
          list.push(cite(p, i));
        }
      }
      const result = distinctCitedSourcePaths(list);

      // No duplicates.
      expect(new Set(result).size).toBe(result.length);
      // No nulls.
      expect(result.every((p) => p != null && p !== "")).toBe(true);
      // Exactly the distinct non-null input paths.
      const expected = Array.from(
        new Set(
          list.map((c) => c.source_path).filter((p): p is string => p != null)
        )
      );
      expect(new Set(result)).toEqual(new Set(expected));
      // First-seen order preserved.
      const firstSeen: string[] = [];
      const seen = new Set<string>();
      for (const c of list) {
        const p = c.source_path;
        if (p != null && !seen.has(p)) {
          seen.add(p);
          firstSeen.push(p);
        }
      }
      expect(result).toEqual(firstSeen);
    }
  });
});

describe("orderCitations — Property 17 client mirror (Req 6.8, 6.9)", () => {
  it("orders case-insensitively by leading element", () => {
    const cits = [
      fc("/1", "Zebra work", "Zebra"),
      fc("/2", "apple work", "apple"),
      fc("/3", "Mango work", "Mango"),
    ];
    const ordered = orderCitations(cits).map((c) => c.leading_element);
    expect(ordered).toEqual(["apple", "Mango", "Zebra"]);
  });

  it("treats leading elements case-insensitively (a == A) then tie-breaks on text", () => {
    const cits = [
      fc("/1", "Smith, Jane. Zeta.", "Smith"),
      fc("/2", "smith, Alan. Alpha.", "smith"),
    ];
    // Equal leading elements (case-insensitive, "Smith" == "smith") -> order by
    // full text, compared case-insensitively: "smith, alan..." < "smith, jane..."
    // so /2 comes first.
    const ordered = orderCitations(cits).map((c) => c.source_path);
    expect(ordered).toEqual(["/2", "/1"]);
  });

  it("places null/blank leading elements last", () => {
    const cits = [
      fc("/1", "No leading zzz", null),
      fc("/2", "Anderson work", "Anderson"),
      fc("/3", "Blank leading", "   "),
      fc("/4", "Baker work", "Baker"),
    ];
    const ordered = orderCitations(cits).map((c) => c.source_path);
    // Anderson, Baker first (have leading); then the two no-leading entries
    // ordered by full text ("Blank leading" < "No leading zzz").
    expect(ordered).toEqual(["/2", "/4", "/3", "/1"]);
  });

  it("is deterministic and idempotent (ordering twice == once)", () => {
    const cits = [
      fc("/1", "Zebra", "Zebra"),
      fc("/2", "apple", "apple"),
      fc("/3", "no-lead b", null),
      fc("/4", "Mango", "Mango"),
      fc("/5", "no-lead a", null),
    ];
    const once = orderCitations(cits);
    const twice = orderCitations(once);
    expect(twice).toEqual(once);
    // Running on the original again yields the identical order.
    expect(orderCitations(cits)).toEqual(once);
  });

  it("does not mutate the input array", () => {
    const cits = [fc("/1", "B", "B"), fc("/2", "A", "A")];
    const snapshot = cits.map((c) => c.source_path);
    orderCitations(cits);
    expect(cits.map((c) => c.source_path)).toEqual(snapshot);
  });

  it("produces a total order over generated inputs (idempotent + all present)", () => {
    const leadPool: (string | null)[] = ["Alpha", "beta", "Gamma", null, "  ", "alpha"];
    let seed = 987654;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
    for (let iter = 0; iter < 150; iter++) {
      const n = Math.floor(rand() * 8);
      const cits: FormattedCitation[] = [];
      for (let i = 0; i < n; i++) {
        const lead = leadPool[Math.floor(rand() * leadPool.length)];
        cits.push(fc(`/p${i}`, `text-${Math.floor(rand() * 1000)}-${i}`, lead));
      }
      const ordered = orderCitations(cits);
      // Same multiset of source_paths (a permutation).
      expect(new Set(ordered.map((c) => c.source_path))).toEqual(
        new Set(cits.map((c) => c.source_path))
      );
      expect(ordered.length).toBe(cits.length);
      // Idempotent.
      expect(orderCitations(ordered)).toEqual(ordered);
    }
  });
});
