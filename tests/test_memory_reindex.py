"""Recovery path: ``memory.reindex_from_markdown``.

When the Qdrant collection gets wiped (corrupt rebuild, manual
``/api/knowledge/graph/clear``, or any future migration that drops the
collection without re-indexing) the canonical markdown layer survives
under ``~/.castor/memories/atoms/`` and orphaned atoms become invisible
to ``memory.search`` / the knowledge-graph view.

The reindex walks every markdown atom, re-embeds it, and upserts under
the SAME point_id so entity relations stay valid. These tests pin:

- A fresh install with no markdown atoms returns a zero-stat dict.
- A markdown-only state (atoms on disk, Qdrant empty) writes every
  atom, the knowledge graph endpoint subsequently sees them.
- ``skip_existing=True`` skips atoms already in Qdrant on a repeat run.
- A malformed atom file is logged + counted toward ``errors`` without
  killing the sweep.
- ``upsert_with_id`` preserves the supplied point_id (not a fresh UUID).
"""
from __future__ import annotations

import uuid as _uuid

import pytest


@pytest.fixture(scope="module", autouse=True)
def _require_embed_model():
    """Skip the whole module when the FastEmbed dense model can't load.

    These tests exercise the real reindex → embed → Qdrant path, so they need
    a working embedding model. On CI the model is fetched from HuggingFace on
    first use; when that fetch flakes (network/offline) every reindex test
    fails with "Could not load model ... from any source" and blocks
    unrelated dependency PRs. Probe once per module and skip gracefully
    instead of failing — the tests still run wherever the model is available
    (local dev, warm CI cache).
    """
    import memory
    try:
        memory._embed("probe")
    except Exception as e:  # noqa: BLE001 - any load failure → skip, never fail
        pytest.skip(f"FastEmbed dense model unavailable ({type(e).__name__}); "
                    "reindex tests need real embeddings")


def test_reindex_empty_corpus_returns_zero_stats(qwe_temp_data_dir):
    import memory
    stats = memory.reindex_from_markdown()
    assert stats == {"scanned": 0, "written": 0, "skipped": 0, "errors": 0}


def test_reindex_writes_markdown_only_atoms_into_qdrant(qwe_temp_data_dir):
    """Seed markdown atoms WITHOUT touching Qdrant, then reindex —
    every atom must appear via memory.search / get_all_entities."""
    import memory
    import memory_store
    # Write 3 entity atoms directly through memory_store (bypasses Qdrant).
    ids = []
    for name in ("alpha topic", "beta person", "gamma project"):
        pid = str(_uuid.uuid4())
        memory_store.write(
            point_id=pid,
            text=name,
            tag="entity",
            meta={"entity_type": "concept",
                  "description": f"about {name}",
                  "relations": [], "observation_count": 1},
        )
        ids.append(pid)

    # Sanity: markdown has them
    assert len(memory_store.iter_all()) == 3

    # Qdrant is empty
    pre_entities = memory.get_all_entities(limit=200)
    assert pre_entities == []

    # Run reindex
    stats = memory.reindex_from_markdown()
    assert stats["scanned"] == 3
    assert stats["written"] == 3
    assert stats["errors"] == 0

    # Qdrant now has them
    post_entities = memory.get_all_entities(limit=200)
    assert len(post_entities) == 3
    names = {e["name"] for e in post_entities}
    assert names == {"alpha topic", "beta person", "gamma project"}


def test_reindex_skip_existing_is_idempotent(qwe_temp_data_dir):
    """Second run with skip_existing=True is a no-op — same scanned
    count, zero written, all skipped."""
    import memory
    import memory_store
    memory_store.write(
        point_id=str(_uuid.uuid4()),
        text="topic1", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )
    memory.reindex_from_markdown()
    second = memory.reindex_from_markdown(skip_existing=True)
    assert second["scanned"] == 1
    assert second["written"] == 0
    assert second["skipped"] == 1
    assert second["errors"] == 0


def test_reindex_skip_existing_false_re_embeds_all(qwe_temp_data_dir):
    """skip_existing=False re-embeds every atom regardless — useful
    after an embedding-model upgrade."""
    import memory
    import memory_store
    memory_store.write(
        point_id=str(_uuid.uuid4()),
        text="x", tag="entity",
        meta={"entity_type": "concept", "description": ""},
    )
    memory.reindex_from_markdown()
    second = memory.reindex_from_markdown(skip_existing=False)
    assert second["scanned"] == 1
    assert second["written"] == 1
    assert second["skipped"] == 0


def test_reindex_preserves_point_id(qwe_temp_data_dir):
    """The id under which the markdown atom was written must be the
    same id Qdrant ends up holding — relations between entities
    reference each other by name / id."""
    import memory
    import memory_store
    pid = str(_uuid.uuid4())
    memory_store.write(
        point_id=pid,
        text="known_entity", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )
    memory.reindex_from_markdown()
    entities = memory.get_all_entities(limit=10)
    assert any(e["id"] == pid for e in entities), (
        f"reindex changed the point id (expected {pid}, "
        f"got {[e['id'] for e in entities]})"
    )


def test_upsert_with_id_writes_exact_id(qwe_temp_data_dir):
    import memory
    import memory_store
    pid = str(_uuid.uuid4())
    memory.upsert_with_id(
        pid, text="z", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )
    entities = memory.get_all_entities(limit=10)
    assert len(entities) == 1
    assert entities[0]["id"] == pid


def test_reindex_handles_malformed_atom(qwe_temp_data_dir, monkeypatch):
    """An atom that returns None from memory_store.read counts as an
    error but does not stop the rest of the sweep."""
    import memory
    import memory_store
    good_pid = str(_uuid.uuid4())
    memory_store.write(
        point_id=good_pid, text="ok", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )
    bad_pid = str(_uuid.uuid4())
    memory_store.write(
        point_id=bad_pid, text="ok2", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )

    # Make memory_store.read return None for ONE specific atom.
    real_read = memory_store.read

    def _broken_read(pid):
        if pid == bad_pid:
            return None
        return real_read(pid)

    monkeypatch.setattr(memory_store, "read", _broken_read)

    stats = memory.reindex_from_markdown(skip_existing=False)
    assert stats["scanned"] == 2
    assert stats["written"] == 1
    assert stats["errors"] == 1


def test_reindex_never_raises_on_pre_scroll_failure(
    qwe_temp_data_dir, monkeypatch,
):
    """If Qdrant scroll-all fails (e.g. collection missing), reindex
    proceeds without the skip-existing optimization — better to write
    everything than to crash."""
    import memory
    import memory_store
    memory_store.write(
        point_id=str(_uuid.uuid4()),
        text="x", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )

    # Force the pre-scroll to fail by swapping _get_qdrant's first scroll call.
    real_qc = memory._get_qdrant()
    original_scroll = real_qc.scroll
    calls = {"n": 0}

    def _broken_scroll(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("forced fail on first scroll")
        return original_scroll(*a, **kw)

    monkeypatch.setattr(real_qc, "scroll", _broken_scroll)
    stats = memory.reindex_from_markdown(skip_existing=True)
    assert stats["scanned"] == 1
    # skip_existing was effectively disabled — the atom gets written
    assert stats["written"] == 1


def test_reindex_endpoint_exposed(qwe_temp_data_dir):
    """``POST /api/knowledge/reindex`` returns the stats dict from the
    helper."""
    import importlib
    import memory_store
    memory_store.write(
        point_id=str(_uuid.uuid4()),
        text="api smoke", tag="entity",
        meta={"entity_type": "topic", "description": ""},
    )
    from fastapi.testclient import TestClient
    import server
    importlib.reload(server)
    with TestClient(server.app) as c:
        r = c.post("/api/knowledge/reindex")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["scanned"] == 1
        assert body["written"] == 1
