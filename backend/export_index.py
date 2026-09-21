#!/usr/bin/env python3
"""CLI: export indexed chunks to a portable bundle.

Build the index once on a powerful machine, then hand the bundle to weaker
machines (or collaborators) to query without re-embedding.

Examples (run from the backend/ directory with the venv):
    ./.venv/bin/python export_index.py archive.jsonl.gz
    ./.venv/bin/python export_index.py project.jsonl.gz --sources a.pdf b.pdf
    ./.venv/bin/python export_index.py grp.jsonl.gz --group <group_id>
    ./.venv/bin/python export_index.py plain.jsonl --no-gzip
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.bundles.export import ExportError, export_bundle
from app.indexing.jobs import manager as job_manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a portable index bundle.")
    parser.add_argument("out", help="Output bundle path (e.g. archive.jsonl.gz)")
    parser.add_argument(
        "--sources",
        nargs="+",
        metavar="SOURCE_PATH",
        help="Export only these source paths (space-separated).",
    )
    parser.add_argument(
        "--group",
        metavar="GROUP_ID",
        help="Export only the sources in this group.",
    )
    parser.add_argument(
        "--no-gzip",
        action="store_true",
        help="Write plain JSONL instead of gzip (default is gzip).",
    )
    args = parser.parse_args(argv)

    # Never run concurrently with an index job.
    if job_manager.is_indexing():
        print("Refused: indexing is in progress. Try again once it finishes.", file=sys.stderr)
        return 2

    try:
        report = export_bundle(
            Path(args.out),
            sources=args.sources,
            group_id=args.group,
            gzip_output=not args.no_gzip,
        )
    except ExportError as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1

    print(f"Exported {report['chunk_count']} chunks to {report['path']}")
    print(f"  embedding model: {report['embed_model']} ({report['embed_dim']}-d)")
    print(f"  vision-derived chunks included: {report['vision_included']}")
    print("  sources:")
    for sp, n in sorted(report["sources"].items()):
        print(f"    {n:>6}  {sp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
