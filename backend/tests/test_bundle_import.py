"""Property-based + example tests for portable-index-bundle import.

Covers Properties 4, 5, 6, 7, 8, 11, 13, 14 of the ``portable-index-bundles``
spec, exercising ``app.bundles.import_.import_bundle`` (and, for the round-trip,
``app.bundles.export.export_bundle``) end-to-end against a fake in-memory vector
store.

Isolation & determinism
------------------------
No real Chroma, Ollama, manifest, or BM25 index is touched. Both ``import_`` and
``export`` resolve the vector store lazily through ``get_store()`` *at call
time*, so we monkeypatch that name **where it is used** — in both
``app.bundles.import_`` and ``app.bundles.export`` — to return the same
``FakeStore``. We likewise patch:

  * ``app.bundles.import_.job_manager.is_indexing`` — the ingest lock, default
    off so imports proceed.
  * ``app.indexing.indexer._rebuild_keyword_index`` — patched to a spy no-op
    because ``import_`` imports it lazily (``from ..indexing.indexer import
    _rebuild_keyword_index``) inside the function, so the *source* attribute is
    what the late import binds to.
  * ``app.bundles.import_.get_manifest`` — returns a ``FakeManifest`` that
    records ``record_imported(source_path, count)`` calls.

The embedding gate compares ``header.embed_model`` to ``settings.embed_model``
and ``header.embed_dim`` to ``_installation_embed_dim()``. We keep the model
name matched by building bundles with ``settings.embed_model`` (via
``export_bundle``) or by writing that model into hand-built headers, and we keep
the dimension gate happy by seeding the fake store with a same-dim vector
(``_installation_embed_dim`` reads the first vector via ``iter_export(None)``);
when the store is empty we patch ``_installation_embed_dim`` to return the dim
directly so no Ollama probe is attempted.

Hypothesis note: ``@given`` does not reset function-scoped fixtures between
generated inputs, so we (a) apply monkeypatches via a per-example
``pytest.MonkeyPatch.context()`` and (b) write bundle files under a module-level
temp dir rather than the function-scoped ``tmp_path`` fixture (which would trip
Hypothesis's health check).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.bundles import export as export_mod
from app.bundles import import_ as import_mod
from app.bundles.export import export_bundle
from app.bundles.format import BundleHeader, BundleWriter, ChunkRecord
from app.bundles.import_ import ImportError_, import_bundle
from app.config import settings as app_settings
from app.indexing import indexer as indexer_mod

DIM = 4
MODEL = app_settings.embed_model  # the installation's embedding model name


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
@dataclass
class FakeStore:
    """In-memory stand-in for the vector store, keyed by chunk id.

    Implements exactly the surface ``import_``/``export`` touch:
      * ``get_all_ids()``            -> set of current ids
      * ``add_precomputed(...)``     -> store rows, return count added
      * ``iter_export(source_paths)``-> yield (id, text, metadata, embedding),
                                        filtered by metadata['source_path']
      * ``delete_by_source(source_path)`` -> drop matching ids
    """

    rows: dict = field(default_factory=dict)  # id -> (text, metadata, embedding)

    def get_all_ids(self) -> set:
        return set(self.rows.keys())

    def add_precomputed(self, ids, texts, metadatas, embeddings, batch_size=256):
        for cid, text, meta, emb in zip(ids, texts, metadatas, embeddings):
            self.rows[cid] = (text, dict(meta), list(emb))
        return len(ids)

    def iter_export(self, source_paths=None):
        for cid, (text, meta, emb) in self.rows.items():
            if source_paths is not None and meta.get("source_path") not in source_paths:
                continue
            yield cid, text, dict(meta), list(emb)

    def delete_by_source(self, source_path):
        drop = [
            cid
            for cid, (_t, meta, _e) in self.rows.items()
            if meta.get("source_path") == source_path
        ]
        for cid in drop:
            del self.rows[cid]
        return len(drop)


@dataclass
class FakeManifest:
    """Records ``record_imported(source_path, count)`` calls for assertions."""

    imported: list = field(default_factory=list)  # list[(source_path, count)]

    def record_imported(self, source_path, count):
        self.imported.append((source_path, count))


class RebuildSpy:
    """Callable no-op standing in for ``_rebuild_keyword_index``; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _emb(seed: int) -> list:
    """A deterministic DIM-length embedding derived from ``seed``."""
    return [float((seed + i) % 7) * 0.5 for i in range(DIM)]


def _chunk(cid: str, source_path: str, content_type: str = "text", text=None):
    """Build a (id, text, metadata, embedding) row for the fake store."""
    return (
        cid,
        text if text is not None else f"text for {cid}",
        {"source_path": source_path, "content_type": content_type},
        _emb(hash(cid) % 100),
    )


def _seed_store(rows) -> FakeStore:
    store = FakeStore()
    for cid, text, meta, emb in rows:
        store.rows[cid] = (text, dict(meta), list(emb))
    return store


def _install(
    mp: pytest.MonkeyPatch,
    *,
    import_store: FakeStore,
    export_store: FakeStore = None,
    indexing: bool = False,
    manifest: FakeManifest = None,
    rebuild: RebuildSpy = None,
    force_dim: int = None,
) -> tuple:
    """Wire fakes into import_/export where the names are used.

    ``export_store`` defaults to ``import_store`` (round-trip uses two distinct
    stores; the enrichment/merge tests use one). ``force_dim`` patches
    ``_installation_embed_dim`` directly (used when the import store is empty so
    the gate has no stored vector to read and we avoid an Ollama probe).
    """
    if export_store is None:
        export_store = import_store
    manifest = manifest or FakeManifest()
    rebuild = rebuild or RebuildSpy()

    mp.setattr(import_mod, "get_store", lambda: import_store)
    mp.setattr(export_mod, "get_store", lambda: export_store)
    mp.setattr(import_mod.job_manager, "is_indexing", lambda: indexing)
    mp.setattr(import_mod, "get_manifest", lambda: manifest)
    mp.setattr(indexer_mod, "_rebuild_keyword_index", rebuild)
    if force_dim is not None:
        mp.setattr(import_mod, "_installation_embed_dim", lambda: force_dim)
    return manifest, rebuild


def _write_bundle(
    path: Path,
    records,
    *,
    embed_model: str = MODEL,
    gzip_output: bool = False,
) -> None:
    """Write a bundle via the real BundleWriter (accurate header + checksum)."""
    with BundleWriter(path, embed_model, gzip_output=gzip_output) as w:
        for cid, text, meta, emb in records:
            w.add(ChunkRecord(id=cid, text=text, embedding=list(emb), metadata=dict(meta)))


# Module-level temp dir for @given tests (function-scoped tmp_path trips a
# Hypothesis health check).
_TMPDIR = Path(tempfile.mkdtemp(prefix="bundle_import_test_"))
_counter = {"n": 0}


def _next_bundle_path() -> Path:
    _counter["n"] += 1
    return _TMPDIR / f"bundle_{_counter['n']}.jsonl"


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
source_paths = st.sampled_from(["a.pdf", "b.pdf", "c.pdf", "docs/d.pdf"])


@st.composite
def chunk_rows(draw, min_size=1, max_size=8, id_prefix="c"):
    """Generate a list of unique-id (id, text, metadata, embedding) rows."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    rows = []
    for i in range(n):
        cid = f"{id_prefix}{i}"
        sp = draw(source_paths)
        rows.append(_chunk(cid, sp))
    return rows


# ===========================================================================
# Property 4 — export/import round-trip preserves chunks
# ===========================================================================
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(rows=chunk_rows(min_size=1, max_size=8))
def test_roundtrip_preserves_chunks(rows) -> None:
    """Feature: portable-index-bundles, Property 4: Export/import round-trip preserves chunks.

    Exporting a seeded archive to a bundle and importing it into a fresh, empty
    archive reproduces the exact chunk set — ids, text, metadata, and (since the
    fakes store exact values) embeddings. Validates 1.1, 1.2, 4.1, 12.1.
    """
    source_store = _seed_store(rows)
    dest_store = FakeStore()
    out = _next_bundle_path()

    with pytest.MonkeyPatch.context() as mp:
        # Export reads the seeded store; the dim gate on import reads the dest
        # store which is empty, so force the installation dim to DIM.
        _install(mp, import_store=dest_store, export_store=source_store, force_dim=DIM)
        export_bundle(out, gzip_output=False)
        report = import_bundle(out)

    exported_ids = {cid for cid, *_ in rows}
    assert dest_store.get_all_ids() == exported_ids
    assert report["added"] == len(exported_ids)

    # Text + metadata preserved; embeddings exact (fakes store exact values).
    for cid, text, meta, emb in rows:
        got_text, got_meta, got_emb = dest_store.rows[cid]
        assert got_text == text
        assert got_meta == meta
        assert got_emb == pytest.approx(emb)


# ===========================================================================
# Property 5 — import is idempotent
# ===========================================================================
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(rows=chunk_rows(min_size=1, max_size=8))
def test_import_is_idempotent(rows) -> None:
    """Feature: portable-index-bundles, Property 5: Import is idempotent.

    Importing a bundle into a fresh store adds N and skips 0; importing the same
    bundle again adds 0 and skips N, leaving the id set unchanged.
    Validates 4.2, 4.3.
    """
    n = len({cid for cid, *_ in rows})
    out = _next_bundle_path()
    _write_bundle(out, rows)

    store = FakeStore()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, force_dim=DIM)
        first = import_bundle(out)
        ids_after_first = store.get_all_ids()
        second = import_bundle(out)

    assert first["added"] == n
    assert first["skipped_existing"] == 0
    assert second["added"] == 0
    assert second["skipped_existing"] == n
    assert store.get_all_ids() == ids_after_first


# ===========================================================================
# Property 6 — merge is a union by id
# ===========================================================================
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    local=chunk_rows(min_size=1, max_size=6, id_prefix="c"),
    extra=chunk_rows(min_size=1, max_size=6, id_prefix="x"),
)
def test_merge_is_union_by_id(local, extra) -> None:
    """Feature: portable-index-bundles, Property 6: Merge is a union by id.

    Seed a store with local chunks, then import a bundle that partially overlaps
    (some shared ids + some new ids). The final id set equals
    union(local, bundle) with no duplicates. Validates 4.1, 4.2, 5.3.
    """
    # Build a bundle from an overlapping mix: half of local (shared ids) + extra.
    shared = local[: max(1, len(local) // 2)]
    bundle_rows = shared + extra
    # Dedup bundle ids (extra prefix differs from local so only 'shared' overlap).
    seen, dedup = set(), []
    for r in bundle_rows:
        if r[0] not in seen:
            seen.add(r[0])
            dedup.append(r)
    bundle_rows = dedup

    out = _next_bundle_path()
    _write_bundle(out, bundle_rows)

    store = _seed_store(local)
    local_ids = store.get_all_ids()

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, force_dim=DIM)
        import_bundle(out)

    bundle_ids = {cid for cid, *_ in bundle_rows}
    assert store.get_all_ids() == local_ids | bundle_ids
    # No duplicates: dict keys are unique by construction; assert count matches.
    assert len(store.rows) == len(local_ids | bundle_ids)


# ===========================================================================
# Property 7 — text-only + vision enrichment yields the union
# ===========================================================================
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(n_text=st.integers(min_value=1, max_value=5), n_vision=st.integers(min_value=1, max_value=5))
def test_text_only_plus_vision_enrichment(n_text, n_vision) -> None:
    """Feature: portable-index-bundles, Property 7: Text-only + vision enrichment yields the union.

    A source is present locally as text-only. Import a bundle carrying those
    same text chunks plus new chart/figure chunks for the same source: the text
    ids are skipped (already present) and the vision ids are added; the final set
    is the union. Validates 5.1, 5.2, 5.3.
    """
    sp = "report.pdf"
    text_rows = [_chunk(f"t{i}", sp, content_type="text") for i in range(n_text)]
    vision_rows = [
        _chunk(f"v{i}", sp, content_type="chart" if i % 2 == 0 else "figure")
        for i in range(n_vision)
    ]

    # Bundle = same text chunks + new vision chunks (same source_path).
    bundle_rows = text_rows + vision_rows
    out = _next_bundle_path()
    _write_bundle(out, bundle_rows)

    # Local store has ONLY the text chunks.
    store = _seed_store(text_rows)

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, force_dim=DIM)
        report = import_bundle(out)

    text_ids = {cid for cid, *_ in text_rows}
    vision_ids = {cid for cid, *_ in vision_rows}

    assert report["added"] == len(vision_ids)
    assert report["skipped_existing"] == len(text_ids)
    assert store.get_all_ids() == text_ids | vision_ids


# ===========================================================================
# Property 8 — embedding mismatch is refused with no writes
# ===========================================================================
def test_embed_model_mismatch_refused(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 8: Embedding mismatch is refused with no writes.

    A bundle whose header embed_model differs from the installation's is refused
    (ImportError_) and the store is unchanged. Validates 6.1, 6.2, 6.3.
    """
    rows = [_chunk("m0", "a.pdf"), _chunk("m1", "a.pdf")]
    out = tmp_path / "wrong_model.jsonl"
    _write_bundle(out, rows, embed_model="some-other-model")

    store = FakeStore()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, force_dim=DIM)
        with pytest.raises(ImportError_):
            import_bundle(out)

    assert store.get_all_ids() == set()


def test_embed_dim_mismatch_refused(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 8: Embedding mismatch is refused with no writes.

    A bundle whose header embed_dim differs from the installation dimension is
    refused with no writes. The installation dim comes from a vector already in
    the store (a different dim than the bundle's). Validates 6.1, 6.2, 6.3.
    """
    # Bundle has DIM-length embeddings; installation store holds an 8-d vector,
    # so _installation_embed_dim() reads 8 != DIM and the gate refuses.
    rows = [_chunk("d0", "a.pdf"), _chunk("d1", "a.pdf")]
    out = tmp_path / "wrong_dim.jsonl"
    _write_bundle(out, rows)  # header dim == DIM (4)

    store = FakeStore()
    store.rows["existing"] = (
        "seed",
        {"source_path": "z.pdf", "content_type": "text"},
        [0.1] * 8,  # 8-d installation vector
    )

    with pytest.MonkeyPatch.context() as mp:
        # Do NOT force_dim; let the gate read the store's 8-d vector.
        _install(mp, import_store=store)
        with pytest.raises(ImportError_):
            import_bundle(out)

    # Only the pre-existing seed remains; nothing from the bundle was added.
    assert store.get_all_ids() == {"existing"}


# ===========================================================================
# Property 11 — replace-source is scoped
# ===========================================================================
def test_replace_source_is_scoped(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 11: Replace-source is scoped.

    Seed a store with sourceA and sourceB chunks. Import a bundle containing only
    sourceA chunks (new ids) with replace_sources=True: sourceA's old chunks are
    removed and replaced by the bundle's, while sourceB is untouched.
    Validates 7.1, 7.3.
    """
    a_old = [_chunk("a_old0", "A.pdf"), _chunk("a_old1", "A.pdf")]
    b_rows = [_chunk("b0", "B.pdf"), _chunk("b1", "B.pdf")]
    store = _seed_store(a_old + b_rows)

    a_new = [_chunk("a_new0", "A.pdf"), _chunk("a_new1", "A.pdf")]
    out = tmp_path / "replace_a.jsonl"
    _write_bundle(out, a_new)

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, force_dim=DIM)
        report = import_bundle(out, replace_sources=True)

    ids = store.get_all_ids()
    # Old A chunks gone, new A chunks present, B untouched.
    assert {"a_old0", "a_old1"} & ids == set()
    assert {"a_new0", "a_new1"} <= ids
    assert {"b0", "b1"} <= ids
    assert report["replaced"] == ["A.pdf"]


# ===========================================================================
# Property 13 — lock blocks import while indexing
# ===========================================================================
def test_lock_blocks_import(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 13: Lock blocks import while indexing.

    When an index job is running, import refuses (ImportError_ mentioning
    indexing) and the store is unchanged. Validates 11.1.
    """
    rows = [_chunk("l0", "a.pdf")]
    out = tmp_path / "locked.jsonl"
    _write_bundle(out, rows)

    store = FakeStore()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, import_store=store, indexing=True, force_dim=DIM)
        with pytest.raises(ImportError_) as excinfo:
            import_bundle(out)

    assert "indexing" in str(excinfo.value).lower()
    assert store.get_all_ids() == set()


# ===========================================================================
# Property 14 — archive stays consistent after import
# ===========================================================================
def test_consistency_after_import(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 14: Archive stays consistent after import.

    After an import that adds chunks, the manifest records the imported sources
    (record_imported called with the right source_path and count) and the BM25
    rebuild hook is invoked. Validates 12.2, 12.3, 12.4.
    """
    rows = [
        _chunk("k0", "a.pdf"),
        _chunk("k1", "a.pdf"),
        _chunk("k2", "b.pdf"),
    ]
    out = tmp_path / "consistency.jsonl"
    _write_bundle(out, rows)

    store = FakeStore()
    with pytest.MonkeyPatch.context() as mp:
        manifest, rebuild = _install(mp, import_store=store, force_dim=DIM)
        report = import_bundle(out)

    assert report["added"] == 3
    # Rebuild hook invoked exactly once (something was added).
    assert rebuild.calls == 1
    # Manifest recorded the right per-source counts.
    recorded = dict(manifest.imported)
    assert recorded == {"a.pdf": 2, "b.pdf": 1}


def test_no_rebuild_or_manifest_when_nothing_added(tmp_path: Path) -> None:
    """Feature: portable-index-bundles, Property 14: Archive stays consistent after import.

    When an import adds nothing (all ids already present), the rebuild hook is
    NOT invoked and the manifest records nothing — the consistency side effects
    are gated on an actual change. Validates 12.2, 12.3, 12.4.
    """
    rows = [_chunk("k0", "a.pdf"), _chunk("k1", "a.pdf")]
    out = tmp_path / "already_present.jsonl"
    _write_bundle(out, rows)

    store = _seed_store(rows)  # everything already present
    with pytest.MonkeyPatch.context() as mp:
        manifest, rebuild = _install(mp, import_store=store, force_dim=DIM)
        report = import_bundle(out)

    assert report["added"] == 0
    assert report["skipped_existing"] == 2
    assert rebuild.calls == 0
    assert manifest.imported == []
