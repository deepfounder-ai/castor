"""Tests for skills/routine_manager.py

Covers:
  - Skill structure (DESCRIPTION, INSTRUCTION, TOOLS, execute signature)
  - routine_list — empty state, user routines, system-task exclusion
  - routine_create — happy path, bad schedule, missing args, dry-run failure
  - routine_update — name / task / schedule / combined, unknown id_or_name,
    bad schedule string, skip_dry_run bypasses validation
  - routine_pause — toggle on/off, not-found
  - routine_delete — by numeric id, by partial name, not-found, ambiguous name
  - _resolve_id_or_name — all branches (integer id, partial name, ambiguous,
    not found, system-task guard)
  - _DEFAULT_SKILLS inclusion
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rm(qwe_temp_data_dir, monkeypatch):
    """Return a freshly-loaded routine_manager module against a clean DB.

    Reloads scheduler (so _ensure_table() runs against the temp DB) then
    imports routine_manager.  A monkeypatched scheduler.add bypasses the
    LLM dry-run by default; tests that care about dry-run override it.
    """
    # Reload scheduler against the temp DB
    for m in ("config", "db", "scheduler"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
        else:
            importlib.import_module(m)

    sched = sys.modules["scheduler"]
    sched._callbacks.clear()

    # Load routine_manager fresh
    skill_path = Path(__file__).parent.parent / "skills" / "routine_manager.py"
    spec = importlib.util.spec_from_file_location("routine_manager", skill_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Ensure DB tables exist before tests run
    import db as _db
    _db.kv_get("_rm_test_init")

    return mod


@pytest.fixture
def rm_skip_dryrun(rm, monkeypatch):
    """rm with scheduler.add hard-wired to skip_dry_run=True."""
    import scheduler as sched

    _real_add = sched.add

    def _no_dry(name, task, schedule, skip_dry_run=False):
        return _real_add(name, task, schedule, skip_dry_run=True)

    monkeypatch.setattr(sched, "add", _no_dry)
    return rm


# ---------------------------------------------------------------------------
# 1. Skill structure
# ---------------------------------------------------------------------------

class TestSkillStructure:
    def test_tools_names(self, rm):
        names = [t["function"]["name"] for t in rm.TOOLS]
        assert names == [
            "routine_list", "routine_create", "routine_update",
            "routine_pause", "routine_delete",
        ]

    def test_description_nonempty(self, rm):
        assert rm.DESCRIPTION.strip()

    def test_instruction_present_and_mentions_workflow(self, rm):
        instr = rm.INSTRUCTION
        assert instr and len(instr) > 100
        # Must mention key concepts from the workflow
        assert "routine_list" in instr
        assert "routine_create" in instr

    def test_execute_callable(self, rm):
        assert callable(rm.execute)

    def test_execute_unknown_tool(self, rm):
        result = rm.execute("no_such_tool", {})
        assert "Unknown" in result

    def test_in_default_skills(self):
        """routine_manager must be always-on — included in _DEFAULT_SKILLS."""
        import skills
        assert "routine_manager" in skills._DEFAULT_SKILLS

    def test_tools_schemas_valid(self, rm):
        """Every tool must have a non-empty description and required fields."""
        for tool in rm.TOOLS:
            fn = tool["function"]
            assert fn.get("description"), f"{fn['name']} missing description"
            params = fn.get("parameters", {})
            assert params.get("type") == "object"

    def test_mutating_tools_have_id_or_name(self, rm):
        """update / pause / delete must declare id_or_name as required."""
        mutating = {"routine_update", "routine_pause", "routine_delete"}
        for tool in rm.TOOLS:
            fn = tool["function"]
            if fn["name"] not in mutating:
                continue
            props = fn["parameters"].get("properties", {})
            required = fn["parameters"].get("required", [])
            assert "id_or_name" in props, f"{fn['name']} missing id_or_name param"
            assert "id_or_name" in required, f"{fn['name']} id_or_name not required"


# ---------------------------------------------------------------------------
# 2. routine_list
# ---------------------------------------------------------------------------

class TestRoutineList:
    def test_empty(self, rm):
        result = rm.execute("routine_list", {})
        assert "no" in result.lower() or "yet" in result.lower()

    def test_shows_created_routine(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rm.execute("routine_create", {
            "name": "Daily ping",
            "task": "echo hello",
            "schedule": "daily 09:00",
        })
        result = rm.execute("routine_list", {})
        assert "Daily ping" in result

    def test_excludes_system_tasks(self, rm):
        """__heartbeat__ and __synthesis__ must never appear in routine_list."""
        import db
        import time
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat) "
            "VALUES (?,?,?,?,?)",
            ("__heartbeat__", "heartbeat", "every 1m", time.time() + 60, 1),
        )
        conn.execute(
            "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat) "
            "VALUES (?,?,?,?,?)",
            ("__synthesis__", "synth", "daily 02:00", time.time() + 7200, 1),
        )
        conn.commit()
        result = rm.execute("routine_list", {})
        assert "__heartbeat__" not in result
        assert "__synthesis__" not in result

    def test_shows_paused_state(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rm.execute("routine_create", {
            "name": "Weekend check",
            "task": "ls",
            "schedule": "weekends 10:00",
        })
        import scheduler
        tasks = scheduler.list_tasks()
        rid = next(t["id"] for t in tasks if t["name"] == "Weekend check")
        scheduler.set_enabled(rid, enabled=False)

        result = rm.execute("routine_list", {})
        assert "PAUSED" in result

    def test_shows_run_count(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rm.execute("routine_create", {
            "name": "Counter test",
            "task": "echo 1",
            "schedule": "every 1h",
        })
        import db
        db.execute(
            "UPDATE scheduled_tasks SET run_count=7 WHERE name='Counter test'"
        )
        result = rm.execute("routine_list", {})
        assert "7" in result


# ---------------------------------------------------------------------------
# 3. routine_create
# ---------------------------------------------------------------------------

class TestRoutineCreate:
    def test_happy_path(self, rm, monkeypatch):
        import scheduler
        monkeypatch.setattr(scheduler, "add",
                            lambda n, t, s, skip_dry_run=False:
                            {"ok": True, "name": n, "next_run": "09:00:00",
                             "repeat": True})
        result = rm.execute("routine_create", {
            "name": "Morning weather",
            "task": "fetch weather and notify",
            "schedule": "daily 09:00",
        })
        assert "Morning weather" in result
        assert "Error" not in result

    def test_missing_name(self, rm):
        result = rm.execute("routine_create", {"task": "x", "schedule": "daily 09:00"})
        assert "Error" in result and "name" in result.lower()

    def test_missing_task(self, rm):
        result = rm.execute("routine_create", {"name": "x", "schedule": "daily 09:00"})
        assert "Error" in result and "task" in result.lower()

    def test_missing_schedule(self, rm):
        result = rm.execute("routine_create", {"name": "x", "task": "do it"})
        assert "Error" in result and "schedule" in result.lower()

    def test_dry_run_failure_surfaces_hint(self, rm, monkeypatch):
        import scheduler
        monkeypatch.setattr(scheduler, "add",
                            lambda n, t, s, skip_dry_run=False: {
                                "error": "Dry-run failed: command not found",
                                "hint": "Use shell with full path",
                                "output": "bash: foobar: command not found",
                                "offer_skip": True,
                                "saved": False,
                            })
        result = rm.execute("routine_create", {
            "name": "Bad routine",
            "task": "foobar --doIt",
            "schedule": "every 1h",
        })
        assert "Error" in result or "Could not" in result
        assert "hint" in result.lower() or "Hint" in result
        assert "skip_dry_run" in result

    def test_skip_dry_run_forwarded(self, rm, monkeypatch):
        """skip_dry_run=True must be passed through to scheduler.add."""
        called_with = {}
        import scheduler

        def _capture(n, t, s, skip_dry_run=False):
            called_with["skip"] = skip_dry_run
            return {"ok": True, "name": n, "next_run": "10:00:00", "repeat": False}

        monkeypatch.setattr(scheduler, "add", _capture)
        rm.execute("routine_create", {
            "name": "Skip test",
            "task": "x",
            "schedule": "in 1h",
            "skip_dry_run": True,
        })
        assert called_with.get("skip") is True

    def test_dry_run_passed_confirmation_shown(self, rm, monkeypatch):
        import scheduler
        monkeypatch.setattr(scheduler, "add",
                            lambda n, t, s, skip_dry_run=False: {
                                "ok": True, "name": n,
                                "next_run": "08:30:00", "repeat": True,
                                "dry_run": "passed",
                                "preview": "Weather: sunny, 22°C",
                            })
        result = rm.execute("routine_create", {
            "name": "Weather",
            "task": "fetch weather",
            "schedule": "daily 08:30",
        })
        assert "passed" in result.lower()
        assert "sunny" in result


# ---------------------------------------------------------------------------
# 4. routine_update
# ---------------------------------------------------------------------------

class TestRoutineUpdate:
    def _create_one(self, rm_skip_dryrun, name="Test routine"):
        rm_skip_dryrun.execute("routine_create", {
            "name": name,
            "task": "echo original",
            "schedule": "daily 09:00",
        })
        import db
        row = db._get_conn().execute(
            "SELECT id FROM scheduled_tasks WHERE name=?", (name,)
        ).fetchone()
        return row[0]

    def test_update_name(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        result = rm.execute("routine_update", {
            "id_or_name": str(rid),
            "new_name": "Renamed routine",
        })
        assert "Error" not in result
        assert "name" in result.lower()
        import db
        row = db._get_conn().execute(
            "SELECT name FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "Renamed routine"

    def test_update_schedule(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        result = rm.execute("routine_update", {
            "id_or_name": str(rid),
            "schedule": "every 2h",
        })
        assert "Error" not in result
        import db
        row = db._get_conn().execute(
            "SELECT schedule, repeat FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "every 2h"
        assert row[1] == 1  # repeat=True

    def test_update_bad_schedule(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        result = rm.execute("routine_update", {
            "id_or_name": str(rid),
            "schedule": "0 9 * * *",  # cron syntax — not supported
        })
        assert "Error" in result
        assert "parse" in result.lower() or "schedule" in result.lower()

    def test_update_task_with_skip_dry_run(self, rm_skip_dryrun, monkeypatch):
        """Updating task with skip_dry_run=True must skip LLM validation."""
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        # Inject a _execute_task that would fail if called
        import sys
        sched = sys.modules["scheduler"]
        monkeypatch.setattr(sched, "_execute_task",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError("dry-run must be skipped")))
        result = rm.execute("routine_update", {
            "id_or_name": str(rid),
            "task": "echo new task",
            "skip_dry_run": True,
        })
        assert "Error" not in result
        import db
        row = db._get_conn().execute(
            "SELECT task FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "echo new task"

    def test_update_dry_run_failure_blocks_save(self, rm_skip_dryrun, monkeypatch):
        """A bad task update (dry-run fails) must not persist the change."""
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        import sys
        sched = sys.modules["scheduler"]
        monkeypatch.setattr(sched, "_execute_task",
                            lambda *a, **kw: "command not found: foobar")
        monkeypatch.setattr(sched, "_validate_dry_run",
                            lambda result, task: {
                                "ok": False,
                                "reason": "command not found",
                                "hint": "use full path",
                            })
        result = rm.execute("routine_update", {
            "id_or_name": str(rid),
            "task": "foobar --bad",
        })
        assert "failed" in result.lower() or "Error" in result
        # Original task must be unchanged
        import db
        row = db._get_conn().execute(
            "SELECT task FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "echo original"

    def test_update_nothing_to_change(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_one(rm)
        result = rm.execute("routine_update", {"id_or_name": str(rid)})
        assert "Error" in result

    def test_update_not_found(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        result = rm.execute("routine_update", {
            "id_or_name": "9999",
            "new_name": "Ghost",
        })
        assert "Error" in result and "not found" in result.lower()

    def test_update_by_partial_name(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        self._create_one(rm, "Morning digest")
        result = rm.execute("routine_update", {
            "id_or_name": "Morning",
            "schedule": "daily 08:00",
        })
        assert "Error" not in result
        import db
        row = db._get_conn().execute(
            "SELECT schedule FROM scheduled_tasks WHERE name='Morning digest'"
        ).fetchone()
        assert row[0] == "daily 08:00"


# ---------------------------------------------------------------------------
# 5. routine_pause
# ---------------------------------------------------------------------------

class TestRoutinePause:
    def _create_enabled(self, rm_skip_dryrun, name="Pausable"):
        rm_skip_dryrun.execute("routine_create", {
            "name": name, "task": "echo x", "schedule": "daily 10:00",
        })
        import db
        row = db._get_conn().execute(
            "SELECT id FROM scheduled_tasks WHERE name=?", (name,)
        ).fetchone()
        return row[0]

    def test_pause_disables(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_enabled(rm)
        result = rm.execute("routine_pause", {"id_or_name": str(rid)})
        assert "paused" in result.lower()
        import db
        row = db._get_conn().execute(
            "SELECT enabled FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == 0

    def test_pause_then_resume(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create_enabled(rm)
        rm.execute("routine_pause", {"id_or_name": str(rid)})  # pause
        result = rm.execute("routine_pause", {"id_or_name": str(rid)})  # resume
        assert "resumed" in result.lower() or "enabled" in result.lower()
        import db
        row = db._get_conn().execute(
            "SELECT enabled FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == 1

    def test_pause_by_name(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        self._create_enabled(rm, "Name pause test")
        result = rm.execute("routine_pause", {"id_or_name": "Name pause"})
        assert "Error" not in result

    def test_pause_not_found(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        result = rm.execute("routine_pause", {"id_or_name": "9999"})
        assert "Error" in result and "not found" in result.lower()

    def test_pause_missing_arg(self, rm):
        result = rm.execute("routine_pause", {})
        assert "Error" in result


# ---------------------------------------------------------------------------
# 6. routine_delete
# ---------------------------------------------------------------------------

class TestRoutineDelete:
    def _create(self, rm_skip_dryrun, name="To delete"):
        rm_skip_dryrun.execute("routine_create", {
            "name": name, "task": "echo bye", "schedule": "daily 11:00",
        })
        import db
        row = db._get_conn().execute(
            "SELECT id FROM scheduled_tasks WHERE name=?", (name,)
        ).fetchone()
        return row[0]

    def test_delete_by_id(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create(rm)
        result = rm.execute("routine_delete", {"id_or_name": str(rid)})
        assert "removed" in result.lower() or "deleted" in result.lower() or "#" in result
        import db
        row = db._get_conn().execute(
            "SELECT id FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row is None, "Routine must be gone from DB"

    def test_delete_by_partial_name(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create(rm, "Fully named routine")
        result = rm.execute("routine_delete", {"id_or_name": "named routine"})
        assert "Error" not in result
        import db
        row = db._get_conn().execute(
            "SELECT id FROM scheduled_tasks WHERE id=?", (rid,)
        ).fetchone()
        assert row is None

    def test_delete_not_found(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        result = rm.execute("routine_delete", {"id_or_name": "9999"})
        assert "Error" in result and "not found" in result.lower()

    def test_delete_missing_arg(self, rm):
        result = rm.execute("routine_delete", {})
        assert "Error" in result

    def test_delete_removes_from_list(self, rm_skip_dryrun):
        rm = rm_skip_dryrun
        rid = self._create(rm, "List removal check")
        rm.execute("routine_delete", {"id_or_name": str(rid)})
        listing = rm.execute("routine_list", {})
        assert "List removal check" not in listing


# ---------------------------------------------------------------------------
# 7. _resolve_id_or_name
# ---------------------------------------------------------------------------

class TestResolveIdOrName:
    def _insert(self, name="Resolve test", system=False):
        import db
        import time
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat) "
            "VALUES (?,?,?,?,?)",
            (name, "x", "daily 09:00", time.time() + 3600, 1),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM scheduled_tasks WHERE name=?", (name,)
        ).fetchone()
        return row[0]

    def test_resolve_by_integer_id(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")  # ensure tables exist
        rid = self._insert("Resolve by id")
        tid, err = rm._resolve_id_or_name(str(rid))
        assert err == ""
        assert tid == rid

    def test_resolve_by_partial_name(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")
        rid = self._insert("My special routine")
        tid, err = rm._resolve_id_or_name("special")
        assert err == ""
        assert tid == rid

    def test_resolve_exact_name(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")
        rid = self._insert("Exact match routine")
        tid, err = rm._resolve_id_or_name("Exact match routine")
        assert err == ""
        assert tid == rid

    def test_resolve_not_found_by_id(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")
        tid, err = rm._resolve_id_or_name("99999")
        assert tid is None
        assert "not found" in err.lower()

    def test_resolve_not_found_by_name(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")
        tid, err = rm._resolve_id_or_name("nonexistent xyz")
        assert tid is None
        assert "not found" in err.lower() or "no routine" in err.lower()

    def test_resolve_ambiguous_name(self, rm, qwe_temp_data_dir):
        import db
        db.kv_get("_init")
        self._insert("Weather morning")
        self._insert("Weather evening")
        tid, err = rm._resolve_id_or_name("Weather")
        assert tid is None
        assert "multiple" in err.lower() or "specific" in err.lower()

    def test_resolve_system_task_blocked(self, rm, qwe_temp_data_dir):
        import db
        import time
        db.kv_get("_init")
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO scheduled_tasks (name, task, schedule, next_run, repeat) "
            "VALUES (?,?,?,?,?)",
            ("__heartbeat__", "hb", "every 1m", time.time() + 60, 1),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM scheduled_tasks WHERE name='__heartbeat__'"
        ).fetchone()
        hb_id = row[0]
        # By ID
        tid, err = rm._resolve_id_or_name(str(hb_id))
        assert tid is None
        assert "system" in err.lower()

    def test_is_system_task_dunder(self, rm):
        assert rm._is_system_task("__heartbeat__") is True
        assert rm._is_system_task("__synthesis__") is True
        assert rm._is_system_task("__custom__") is True
        assert rm._is_system_task("morning weather") is False
        assert rm._is_system_task("my_routine") is False


# ---------------------------------------------------------------------------
# 8. Tool-search index registration
# ---------------------------------------------------------------------------

class TestToolSearchIndex:
    """Check static registration constants — no module reloading needed.

    _TOOL_SEARCH_INDEX and TOOL_CATEGORIES_BY_NAME are module-level dicts
    defined at parse time; importing tools once is sufficient and safe for
    the rest of the test suite (no stale-reference side-effects).
    """

    @staticmethod
    def _tools_mod():
        import tools
        return tools

    def test_routine_keyword_returns_skill_tools(self):
        """tool_search('routine') must surface routine_manager tools."""
        idx = self._tools_mod()._TOOL_SEARCH_INDEX
        assert "routine" in idx
        for expected in ("routine_list", "routine_create",
                         "routine_update", "routine_pause", "routine_delete"):
            assert expected in idx["routine"], (
                f"'{expected}' missing from _TOOL_SEARCH_INDEX['routine']"
            )

    def test_schedule_keyword_includes_routine_tools(self):
        idx = self._tools_mod()._TOOL_SEARCH_INDEX
        sched = idx.get("schedule", [])
        assert "routine_list" in sched
        assert "routine_create" in sched

    def test_routine_tools_in_tool_categories(self):
        cats = self._tools_mod().TOOL_CATEGORIES_BY_NAME
        for name in ("routine_list", "routine_create", "routine_update",
                     "routine_pause", "routine_delete"):
            assert name in cats, f"'{name}' missing from TOOL_CATEGORIES_BY_NAME"
            assert cats[name] == "automation"
