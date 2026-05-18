"""Per-thread persistence of `tool_search` activations.

The previous behaviour cleared `_active_extra_tools` on every agent turn,
which meant the tools array sent to the LLM kept changing as it
re-discovered the same skills via `tool_search`. That defeated Anthropic
prompt caching (the tools list is part of the cached prefix) and burned
extra agent rounds re-running tool_search.

New behaviour: activations persist in `kv` keyed by `thread_active_tools_<tid>`.
`_load_active_tools_for_thread` restores them at the top of each turn,
`_do_tool_search` persists new additions, and `_reset_active_tools_for_thread`
wipes them when the user explicitly resets.
"""
from __future__ import annotations

import json

import db
import threads
import tools


# ── Persistence round-trip ───────────────────────────────────────────────────


def test_load_active_tools_starts_empty(qwe_temp_data_dir):
    """A fresh thread has no activated extras."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == set()


def test_persist_active_tools_writes_to_kv(qwe_temp_data_dir):
    """After tool_search adds to _active_extra_tools and we persist,
    the kv key carries a sorted JSON list."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    tools._load_active_tools_for_thread(tid)
    tools._active_extra_tools.update({"browser_open", "browser_snapshot"})
    tools._persist_active_tools(tid)

    raw = db.kv_get(tools._THREAD_ACTIVE_TOOLS_KEY + tid)
    assert raw is not None
    arr = json.loads(raw)
    assert arr == ["browser_open", "browser_snapshot"]


def test_load_after_persist_round_trip(qwe_temp_data_dir):
    """Persist → fresh process → load brings the set back."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    tools._active_extra_tools = {"notes_add", "browser_open"}
    tools._persist_active_tools(tid)

    # Simulate a brand-new turn: clear the global, then load.
    tools._active_extra_tools = set()
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == {"notes_add", "browser_open"}


def test_load_for_thread_isolates_thread_state(qwe_temp_data_dir):
    """Two threads have independent activations."""
    t1 = threads.create("t1", source="cli")
    t2 = threads.create("t2", source="cli")
    tid1, tid2 = t1["id"], t2["id"]

    tools._load_active_tools_for_thread(tid1)
    tools._active_extra_tools.update({"browser_open"})
    tools._persist_active_tools(tid1)

    tools._load_active_tools_for_thread(tid2)
    tools._active_extra_tools.update({"notes_add"})
    tools._persist_active_tools(tid2)

    tools._load_active_tools_for_thread(tid1)
    assert tools._active_extra_tools == {"browser_open"}

    tools._load_active_tools_for_thread(tid2)
    assert tools._active_extra_tools == {"notes_add"}


def test_load_with_corrupt_kv_falls_back_to_empty(qwe_temp_data_dir):
    """If the kv blob is malformed JSON, treat as empty (don't crash)."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    db.kv_set(tools._THREAD_ACTIVE_TOOLS_KEY + tid, "{not-valid-json")
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == set()


def test_load_with_non_list_kv_falls_back_to_empty(qwe_temp_data_dir):
    """JSON that doesn't parse to a list → empty set, no crash."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    db.kv_set(tools._THREAD_ACTIVE_TOOLS_KEY + tid, '{"unexpected": "object"}')
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == set()


def test_load_without_thread_id_uses_active_thread(qwe_temp_data_dir):
    """Calling without an explicit tid resolves the active thread."""
    t = threads.create("test", source="cli")
    threads.switch(t["id"])
    tools._active_extra_tools = {"browser_open"}
    tools._persist_active_tools()  # no tid → uses active

    tools._active_extra_tools = set()
    tools._load_active_tools_for_thread()  # no tid → uses active
    assert tools._active_extra_tools == {"browser_open"}


# ── _reset_active_tools (compat shim) ────────────────────────────────────────


def test_reset_active_tools_now_loads_instead_of_clearing(qwe_temp_data_dir):
    """Legacy callers using _reset_active_tools() (e.g. agent.run) now
    restore persisted state rather than wiping. The activations from the
    PRIOR turn must survive the boundary."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    tools._load_active_tools_for_thread(tid)
    tools._active_extra_tools.update({"notes_add"})
    tools._persist_active_tools(tid)

    # Simulate the cross-turn boundary the legacy chat path takes.
    threads.switch(tid)
    tools._active_extra_tools = set()
    tools._reset_active_tools()  # the legacy entry point
    assert tools._active_extra_tools == {"notes_add"}


# ── Explicit reset path ──────────────────────────────────────────────────────


def test_reset_for_thread_clears_persistence(qwe_temp_data_dir):
    """_reset_active_tools_for_thread (the explicit user-driven reset)
    clears both the in-memory set AND the kv key."""
    t = threads.create("test", source="cli")
    tid = t["id"]
    tools._active_extra_tools = {"browser_open"}
    tools._persist_active_tools(tid)
    assert db.kv_get(tools._THREAD_ACTIVE_TOOLS_KEY + tid) is not None

    tools._reset_active_tools_for_thread(tid)
    assert tools._active_extra_tools == set()
    assert db.kv_get(tools._THREAD_ACTIVE_TOOLS_KEY + tid) is None

    # And the NEXT load is clean.
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == set()


# ── tool_search itself persists ──────────────────────────────────────────────


def test_tool_search_persists_activations(qwe_temp_data_dir):
    """When the agent calls tool_search and finds matches, the activated
    set must land in DB immediately — so the NEXT turn (which calls
    _load_active_tools_for_thread) restores them."""
    t = threads.create("test", source="cli")
    threads.switch(t["id"])
    tid = t["id"]
    tools._load_active_tools_for_thread(tid)
    # Sanity: starts empty
    assert tools._active_extra_tools == set()

    # Search for "browser" — matches in _TOOL_SEARCH_INDEX.
    result = tools._do_tool_search("browser")
    assert "Activated" in result or "ALREADY ACTIVE" in result
    assert len(tools._active_extra_tools) > 0
    persisted_names = set(tools._active_extra_tools)

    # Check kv was updated
    raw = db.kv_get(tools._THREAD_ACTIVE_TOOLS_KEY + tid)
    assert raw is not None
    assert set(json.loads(raw)) == persisted_names

    # Simulate a new turn — wipe global, reload from DB.
    tools._active_extra_tools = set()
    tools._load_active_tools_for_thread(tid)
    assert tools._active_extra_tools == persisted_names


def test_tool_search_short_circuits_on_already_active(qwe_temp_data_dir):
    """Calling tool_search twice for the same keyword in the same thread
    returns ALREADY ACTIVE the second time — the persisted state is
    inspected before re-activating."""
    t = threads.create("test", source="cli")
    threads.switch(t["id"])
    tools._load_active_tools_for_thread(t["id"])

    tools._do_tool_search("notes")
    second = tools._do_tool_search("notes")
    assert "ALREADY ACTIVE" in second


def test_tools_list_stable_across_simulated_turns(qwe_temp_data_dir):
    """The load-call → search → persist cycle yields the SAME tools list
    on the next turn — which is the core property that lets Anthropic
    prompt-cache hit on subsequent requests within a thread."""
    t = threads.create("test", source="cli")
    threads.switch(t["id"])
    tid = t["id"]

    # Turn 1: agent searches for browser tools
    tools._load_active_tools_for_thread(tid)
    tools._do_tool_search("browser")
    turn1_tools = {t["function"]["name"] for t in tools.get_all_tools()}

    # Turn 2 boundary: simulate the new agent.run starting up
    tools._active_extra_tools = set()
    tools._load_active_tools_for_thread(tid)
    turn2_tools = {t["function"]["name"] for t in tools.get_all_tools()}

    # Same tools list → prompt cache prefix stays stable
    assert turn1_tools == turn2_tools
    # And it contains browser_open (the most common keyword match)
    assert "browser_open" in turn1_tools
