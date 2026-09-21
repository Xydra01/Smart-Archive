"""Tests for the portable-index-bundles format + export layers.

These exercise the on-disk bundle format (round-trip fidelity, self-describing
header accuracy, and containment of malformed/untrusted input) and the export
selection logic (all / by source / by group), without touching real Chroma or
Ollama.

Isolation & determinism:
  - Bundles are written under tmp_path (example tests) or a tmp_path_factory
    directory (Hypothesis tests, which can't take a function-scoped tmp_path
    fixture without tripping the function-scoped-fixture health check).
  - Export reaches the vector store and group store via ``get_store()`` and
    ``get_group_store()``; we monkeypatch those names *where they are used* — in
    ``app.bundles.export`` — with in-memory fakes, so the real singletons are
    never constructed or touched.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.bundles import export as export_mod
from app.bundles.export import ExportError, export_bundle
from app.bundles.format import (
    BUNDLE_FORMAT_VERSION,
    BundleError,
    BundleHeader,
    BundleWriter,
    ChunkRecord,
    read_bundle,
    read_header,
)

EMBED_DIM = 4
_VISION_TYPES = {"chart", "figure", "ocr"}


# ---------------------------------------------------------------------------
# Hypothesis strategies for ChunkRecords with a fixed embedding dimension.
# ---------------------------------------------------------------------------

_floats = st.floats(
    allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6, width=32
)


def _record_strategy() -> st.SearchStrategy:
    content_types = st.sampled_from(
        ["text", "chart", "figure", "ocr", "table", "code"]
    )
    metadata = st.fixed_dictionaries(
        {
            "source_path": st.sampled_from(["a.pdf", "b.pdf", "notes/c.md"]),
            "content_type": content_types,
        }
    )
    return st.builds(
        ChunkRecord,
        id=st.text(min_size=1, max_size=24),
        text=st.text(max_size=200),
        embedding=st.lists(_floats, min_size=EMBED_DIM, max_size=EMBED_DIM),
        metadata=metadata,
    )


# ===========================================================================
# FORMAT round-trip + header accuracy (Property 12)
# ===========================================================================


@settings(max_examples=100)
@given(records=st.lists(_record_strategy(), min_size=0, max_size=15), use_gzip=st.booleans())
def test_format_roundtrip_and_header_accuracy(records, use_gzip, tmp_path_factory):
    """Feature: portable-index-bundles, Property 12: header self-description is accurate.

    Writing N records and reading them back preserves id/text/embedding/metadata
    in order, and the header reports chunk_count == N, embed_dim == 4, a clean
    checksum, and zero invalid records. Validates 3.1-3.4.
    """
    out = tmp_path_factory.mktemp("bundle") / ("b.jsonl.gz" if use_gzip else "b.jsonl")

    with BundleWriter(out, "nomic-embed-text", gzip_output=use_gzip) as w:
        for rec in records:
            w.add(rec)

    header, gen, stats = read_bundle(out)
    read_back = list(gen)

    assert header.chunk_count == len(records)
    assert header.embed_dim == (EMBED_DIM if records else 0)
    assert len(read_back) == len(records)
    for original, got in zip(records, read_back):
        assert got.id == original.id
        assert got.text == original.text
        assert got.embedding == pytest.approx(original.embedding)
        assert got.metadata == original.metadata
    assert stats["checksum_ok"] is True
    assert stats["invalid"] == 0


@settings(max_examples=100)
@given(records=st.lists(_record_strategy(), min_size=1, max_size=15))
def test_format_vision_included_flag(records, tmp_path_factory):
    """Feature: portable-index-bundles, Property 12: vision_included reflects content.

    header.vision_included is True iff at least one record's metadata
    content_type is a vision-derived type (chart/figure/ocr). Validates 3.3.
    """
    out = tmp_path_factory.mktemp("bundle") / "b.jsonl"
    with BundleWriter(out, "nomic-embed-text", gzip_output=False) as w:
        for rec in records:
            w.add(rec)

    expected = any(r.metadata.get("content_type") in _VISION_TYPES for r in records)
    header = read_header(out)
    assert header.vision_included is expected


# ===========================================================================
# FORMAT malformed input containment (Property 10, format layer)
# ===========================================================================


def _write_lines(path: Path, lines: list[str]) -> None:
    """Write raw lines (each already without trailing newline) as a plain bundle."""
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
            f.write("\n")


def _good_header_line(dim: int, checksum: str = "sha256:0", **kw) -> str:
    return BundleHeader(
        embed_model="nomic-embed-text",
        embed_dim=dim,
        chunk_count=kw.pop("chunk_count", 1),
        checksum=checksum,
        **kw,
    ).to_line()


def test_read_header_not_json(tmp_path):
    """Feature: portable-index-bundles, Property 10: bad header refused (non-JSON).

    A first line that is not valid JSON raises BundleError. Validates 8.1.
    """
    p = tmp_path / "b.jsonl"
    _write_lines(p, ["this is not json {{{"])
    with pytest.raises(BundleError):
        read_header(p)


def test_read_header_missing_bundle_key(tmp_path):
    """Feature: portable-index-bundles, Property 10: bad header refused (no _bundle).

    A first line that is valid JSON but lacks the _bundle key raises BundleError.
    Validates 8.1.
    """
    p = tmp_path / "b.jsonl"
    _write_lines(p, [json.dumps({"not_a_bundle": True})])
    with pytest.raises(BundleError):
        read_header(p)


def test_read_header_unsupported_version(tmp_path):
    """Feature: portable-index-bundles, Property 10: unsupported version refused.

    A header with an unknown format_version raises BundleError. Validates 8.2.
    """
    p = tmp_path / "b.jsonl"
    _write_lines(p, [_good_header_line(EMBED_DIM, format_version=999)])
    with pytest.raises(BundleError):
        read_header(p)
    # Sanity: the current version is what the build supports.
    assert BUNDLE_FORMAT_VERSION != 999


def test_read_bundle_skips_and_counts_bad_records(tmp_path):
    """Feature: portable-index-bundles, Property 10: bad records skipped and counted.

    A hand-built bundle (valid header, dim=3) with one good record, one non-JSON
    line, one record missing a required field, and one record whose embedding
    length != 3 yields only the good record and counts 3 invalid. Validates
    8.3, 8.4.
    """
    dim = 3
    good = ChunkRecord(
        id="good", text="hello", embedding=[0.1, 0.2, 0.3], metadata={"source_path": "a.pdf"}
    )
    lines = [
        _good_header_line(dim),
        good.to_line(),
        "not json at all }{",
        json.dumps({"id": "x", "text": "t", "embedding": [1.0, 2.0, 3.0]}),  # no metadata
        json.dumps(
            {"id": "y", "text": "t", "embedding": [1.0, 2.0], "metadata": {}}
        ),  # wrong dim
    ]
    p = tmp_path / "b.jsonl"
    _write_lines(p, lines)

    header, gen, stats = read_bundle(p)
    assert header.embed_dim == dim
    yielded = list(gen)
    assert len(yielded) == 1
    assert yielded[0].id == "good"
    assert stats["invalid"] == 3


def test_read_bundle_oversized_record_capped(tmp_path):
    """Feature: portable-index-bundles, Property 10: oversized record is capped.

    With a small max_record_bytes, an oversized line is counted invalid and
    never parsed/loaded, while a small valid record still imports. Validates
    8.7.
    """
    dim = 3
    good = ChunkRecord(
        id="g", text="hi", embedding=[0.0, 0.0, 0.0], metadata={"source_path": "a.pdf"}
    )
    big = ChunkRecord(
        id="big", text="x" * 5000, embedding=[0.0, 0.0, 0.0], metadata={"source_path": "a.pdf"}
    )
    p = tmp_path / "b.jsonl"
    _write_lines(p, [_good_header_line(dim), good.to_line(), big.to_line()])

    header, gen, stats = read_bundle(p, max_record_bytes=256)
    yielded = list(gen)
    assert [r.id for r in yielded] == ["g"]
    assert stats["invalid"] == 1


def test_read_bundle_checksum_mismatch(tmp_path):
    """Feature: portable-index-bundles, Property 10: checksum mismatch detected.

    A bundle whose header checksum does not match the record lines reports
    checksum_ok False once the generator is exhausted. Validates 8.6.
    """
    dim = 3
    good = ChunkRecord(
        id="g", text="hi", embedding=[0.0, 0.0, 0.0], metadata={"source_path": "a.pdf"}
    )
    p = tmp_path / "b.jsonl"
    _write_lines(p, [_good_header_line(dim, checksum="sha256:deadbeef"), good.to_line()])

    header, gen, stats = read_bundle(p)
    assert stats["checksum_ok"] is None  # not computed until exhausted
    list(gen)
    assert stats["checksum_ok"] is False


# ===========================================================================
# EXPORT selectivity (Property 9)
# ===========================================================================


@dataclass
class _FakeChunk:
    id: str
    text: str
    metadata: dict
    embedding: list


class _FakeStore:
    """Minimal stand-in for the vector store's export surface.

    iter_export(source_paths) yields (id, text, metadata, embedding) tuples,
    filtered by metadata['source_path'] when a selection set is given (None ⇒
    everything).
    """

    def __init__(self, chunks: list[_FakeChunk]) -> None:
        self._chunks = chunks

    def iter_export(self, source_paths):
        for c in self._chunks:
            if source_paths is None or c.metadata.get("source_path") in source_paths:
                yield (c.id, c.text, c.metadata, c.embedding)


@dataclass
class _FakeGroup:
    members: set


class _FakeGroupStore:
    def __init__(self, groups: dict) -> None:
        self._groups = groups

    def get(self, group_id):
        return self._groups.get(group_id)


def _seed_chunks() -> list[_FakeChunk]:
    def mk(i, sp, ct="text"):
        return _FakeChunk(
            id=f"{sp}:{i}",
            text=f"chunk {i} of {sp}",
            metadata={"source_path": sp, "content_type": ct},
            embedding=[float(i)] * EMBED_DIM,
        )

    return [
        mk(0, "a.pdf"),
        mk(1, "a.pdf"),
        mk(2, "b.pdf", ct="chart"),
        mk(3, "notes/c.md"),
    ]


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeStore(_seed_chunks())
    monkeypatch.setattr(export_mod, "get_store", lambda: store)
    return store


def test_export_all(fake_store, tmp_path):
    """Feature: portable-index-bundles, Property 9: export-all includes everything.

    With no selection, the bundle contains every chunk and the header source
    summary counts all sources. Validates 2.1, 2.5.
    """
    out = tmp_path / "all.jsonl"
    report = export_bundle(out, gzip_output=False)

    header, gen, stats = read_bundle(out)
    ids = {r.id for r in gen}
    assert ids == {"a.pdf:0", "a.pdf:1", "b.pdf:2", "notes/c.md:3"}
    assert header.sources == {"a.pdf": 2, "b.pdf": 1, "notes/c.md": 1}
    assert report["chunk_count"] == 4
    assert stats["checksum_ok"] is True


def test_export_single_source(fake_store, tmp_path):
    """Feature: portable-index-bundles, Property 9: source selection is exact.

    Exporting sources=[a.pdf] yields only that source's chunks and a header
    source summary containing just it. Validates 2.2, 2.5.
    """
    out = tmp_path / "one.jsonl"
    export_bundle(out, sources=["a.pdf"], gzip_output=False)

    header, gen, _ = read_bundle(out)
    ids = {r.id for r in gen}
    assert ids == {"a.pdf:0", "a.pdf:1"}
    assert header.sources == {"a.pdf": 2}


def test_export_by_group(fake_store, tmp_path, monkeypatch):
    """Feature: portable-index-bundles, Property 9: group selection resolves members.

    Exporting group_id resolves the group's member source_paths and includes
    only those sources' chunks. Validates 2.3, 2.5.
    """
    groups = {"g1": _FakeGroup(members={"b.pdf", "notes/c.md"})}
    monkeypatch.setattr(export_mod, "get_group_store", lambda: _FakeGroupStore(groups))

    out = tmp_path / "grp.jsonl"
    export_bundle(out, group_id="g1", gzip_output=False)

    header, gen, _ = read_bundle(out)
    ids = {r.id for r in gen}
    assert ids == {"b.pdf:2", "notes/c.md:3"}
    assert header.sources == {"b.pdf": 1, "notes/c.md": 1}


def test_export_zero_match_raises_and_writes_nothing(fake_store, tmp_path):
    """Feature: portable-index-bundles, Property 9: empty selection is refused.

    A selection that matches no chunks raises ExportError and writes no bundle
    file. Validates 2.4.
    """
    out = tmp_path / "empty.jsonl"
    with pytest.raises(ExportError):
        export_bundle(out, sources=["does-not-exist.pdf"], gzip_output=False)
    assert not out.exists()
