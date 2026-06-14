"""Entity de-duplication — collapse same-named graph nodes into one.

Legacy synthesis spawned a NEW entity node every run instead of updating the
existing one (a fuzzy search(limit=1) + exact-text guard missed the real node
when a near-name out-ranked it), so the knowledge graph filled with up to
14x 'Drayage' / 'LinkedIn' / … nodes. These pin the merge + dedupe behaviour.
"""
from __future__ import annotations

import memory


def test_merge_entity_group_unions_relations_and_sums_obs():
    grp = [
        {"name": "Drayage", "type": "concept", "description": "short",
         "relations": [{"to": "Port", "rel": "at"}], "observation_count": 3},
        {"name": "drayage", "type": "service", "description": "a longer description",
         "relations": [{"to": "Port", "rel": "at"}, {"to": "Truck", "rel": "uses"}],
         "observation_count": 2},
    ]
    m = memory.merge_entity_group(grp)
    assert m["name"] == "Drayage"                  # first non-empty display casing
    assert m["observation_count"] == 5             # summed
    assert m["description"] == "a longer description"  # longest wins
    # relations unioned, deduped by (to, rel)
    assert {(r["to"], r["rel"]) for r in m["relations"]} == {("Port", "at"), ("Truck", "uses")}


def test_entity_key_casefold_and_trim():
    assert memory._entity_key("  Drayage ") == memory._entity_key("drayage")
    assert memory._entity_key(None) == ""


def test_dedupe_entities_collapses_and_recreates(monkeypatch):
    store = [
        {"id": "a", "name": "Drayage", "type": "concept", "description": "x",
         "relations": [{"to": "Port", "rel": "at"}], "observation_count": 1},
        {"id": "b", "name": "drayage", "type": "concept", "description": "xx",
         "relations": [{"to": "Truck", "rel": "uses"}], "observation_count": 1},
        {"id": "c", "name": "LinkedIn", "type": "concept", "description": "",
         "relations": [], "observation_count": 1},
    ]
    deleted: list[str] = []
    created: list[dict] = []
    monkeypatch.setattr(memory, "get_all_entities", lambda limit=10000: list(store))
    monkeypatch.setattr(memory, "delete", lambda pid: deleted.append(pid) or True)
    monkeypatch.setattr(memory, "_save_single",
                        lambda **kw: created.append(kw) or "new-id")

    res = memory.dedupe_entities()
    assert res == {"groups": 1, "removed": 1}      # only the Drayage pair merged
    assert set(deleted) == {"a", "b"}              # both copies deleted
    assert len(created) == 1                        # one canonical node recreated
    meta = created[0]["meta"]
    assert created[0]["text"] == "Drayage"
    assert meta["observation_count"] == 2
    assert {(r["to"], r["rel"]) for r in meta["relations"]} == {("Port", "at"), ("Truck", "uses")}


def test_dedupe_entities_noop_when_clean(monkeypatch):
    store = [
        {"id": "a", "name": "Drayage", "type": "concept", "description": "x",
         "relations": [], "observation_count": 1},
        {"id": "c", "name": "LinkedIn", "type": "concept", "description": "",
         "relations": [], "observation_count": 1},
    ]
    monkeypatch.setattr(memory, "get_all_entities", lambda limit=10000: list(store))
    monkeypatch.setattr(memory, "delete", lambda pid: (_ for _ in ()).throw(AssertionError("should not delete")))
    monkeypatch.setattr(memory, "_save_single", lambda **kw: (_ for _ in ()).throw(AssertionError("should not recreate")))
    assert memory.dedupe_entities() == {"groups": 0, "removed": 0}


def test_find_entities_by_name_matches_casefold(monkeypatch):
    store = [
        {"id": "a", "name": "Drayage"}, {"id": "b", "name": "drayage"},
        {"id": "c", "name": "LinkedIn"},
    ]
    monkeypatch.setattr(memory, "get_all_entities", lambda limit=10000: list(store))
    got = memory.find_entities_by_name("  DRAYAGE ")
    assert {e["id"] for e in got} == {"a", "b"}
    assert memory.find_entities_by_name("") == []
