#!/usr/bin/env python3
"""CLI: import a portable bundle into this installation's archive.

Merges the bundle's chunks by content id: identical chunks dedup, new chunks
(e.g. vision-derived charts/figures) are added. Re-importing is safe (adds
nothing). Refuses a bundle built with a different embedding model/dimension,
since mixing embedding spaces would corrupt retrieval.

Examples (run from the backend/ directory with the venv):
    ./.venv/bin/python import_index.py archive.jsonl.gz
    ./.venv/bin/python import_index.py project.jsonl.gz --replace-sources
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.bundles.import_ import ImportError_, import_bundle
from app.indexing.jobs import manager as job_manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a portable index bundle.")
    parser.add_argument("bundle", help="Bundle path to import (.jsonl or .jsonl.gz)")
    parser.add_argument(
        "--replace-sources",
        action="store_true",
        help=(
            "Before merging, delete existing chunks for each source present in "
            "the bundle (scoped to those sources only). Default is union merge."
        ),
    )
    args = parser.parse_args(argv)

    path = Path(args.bundle)
    if not path.is_file():
        print(f"Import failed: no such file: {path}", file=sys.stderr)
        return 1

    # Lock is also enforced inside import_bundle; check here for a clean message
    # and exit code before doing any work.
    if job_manager.is_indexing():
        print("Refused: indexing is in progress. Try again once it finishes.", file=sys.stderr)
        return 2

    try:
        report = import_bundle(path, replace_sources=args.replace_sources)
    except ImportError_ as e:
        print(f"Import refused: {e}", file=sys.stderr)
        return 1

    print(f"Imported from {path}")
    print(f"  added:            {report['added']}")
    print(f"  skipped (already present): {report['skipped_existing']}")
    print(f"  skipped (invalid records): {report['invalid_skipped']}")
    if report.get("replaced"):
        print(f"  replaced sources: {', '.join(report['replaced'])}")
    if report.get("checksum_ok") is False:
        print("  WARNING: checksum mismatch — the bundle may be truncated or corrupt.")
    if report["added"]:
        print("  sources added to:")
        for sp, n in sorted(report["sources"].items()):
            print(f"    {n:>6}  {sp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
