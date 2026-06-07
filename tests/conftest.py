"""Shared pytest fixtures for the castor test suite.

Historical note: several legacy test files used to inject mock modules into
``sys.modules`` at import time (``sys.modules["memory"] = FakeModule()``).
pytest collects every test file before running, so those mocks leaked to
every sibling test. The fix was to replace module-level mutation with
``monkeypatch`` fixtures that auto-revert after each test. This conftest
provides the common fixtures used across the suite.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

# Make repo root importable for every test file (single source of truth —
# legacy files used to each do this themselves).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Self-healing tempdir cleanup ───────────────────────────────────────────
#
# ``qwe_temp_data_dir`` uses ``shutil.rmtree(ignore_errors=True)`` in its
# teardown, so a partially-locked Qdrant directory or a Ctrl+C / OOM during
# the test silently leaks the tempdir. Over hundreds of dev iterations this
# accumulates GB of orphan ``castor_pytest_*`` dirs under ``$TMPDIR``. One
# dev tree hit 8157 dirs / 24 GB before the leak was caught (2026-06-04).
#
# Self-heal at session start: walk every ``castor_pytest_*`` older than the
# threshold and best-effort delete. This runs ONCE per pytest invocation so
# the cost is bounded.  Threshold tuned so a parallel pytest session (e.g.
# pytest-xdist) can't accidentally nuke a sibling worker's live tempdir.

_LEAK_AGE_THRESHOLD_SEC = 3600  # 1 hour
_LEAK_CLEANUP_RAN = False
_SESSION_TEMPDIRS: list[Path] = []


def _sweep_leaked_tempdirs() -> None:
    """Delete ``castor_pytest_*`` dirs older than the threshold. Idempotent
    within a session, safe across parallel test runs (only touches old dirs).
    Never raises — best-effort cleanup must never break the suite.
    """
    global _LEAK_CLEANUP_RAN
    if _LEAK_CLEANUP_RAN:
        return
    _LEAK_CLEANUP_RAN = True
    base = Path(tempfile.gettempdir())
    cutoff = time.time() - _LEAK_AGE_THRESHOLD_SEC
    swept = 0
    try:
        for entry in base.iterdir():
            if not entry.is_dir() or not entry.name.startswith("castor_pytest_"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    swept += 1
            except OSError:
                # Permission denied / vanished mid-iter — skip.
                continue
    except OSError:
        return
    if swept:
        print(f"\n[conftest] swept {swept} leaked castor_pytest_* tempdir(s)")


_sweep_leaked_tempdirs()


def pytest_sessionfinish(session, exitstatus):
    """End-of-session cleanup of every ``castor_pytest_*`` the fixture
    created during this run. Only touches dirs registered in
    ``_SESSION_TEMPDIRS`` so a parallel pytest-xdist worker's live
    tempdir is never touched. Two-pass rmtree per dir for Qdrant lock
    contention. Runs even when tests crashed — pytest invokes session-
    finish hooks regardless of test outcome.
    """
    swept = 0
    for path in _SESSION_TEMPDIRS:
        if not path.exists():
            continue
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            time.sleep(0.05)
            shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            swept += 1
    if swept:
        print(f"\n[conftest] end-of-session swept {swept} castor_pytest_* tempdir(s)")


@pytest.fixture
def qwe_temp_data_dir(monkeypatch):
    """Point CASTOR_DATA_DIR at a fresh tempdir and reload config/db.

    Yields the Path of the tempdir. Original env + module state are restored
    automatically by ``monkeypatch``; the tempdir itself is removed on exit.
    """
    import importlib

    tmp_root = Path(tempfile.mkdtemp(prefix="castor_pytest_"))
    _SESSION_TEMPDIRS.append(tmp_root)
    monkeypatch.setenv("CASTOR_DATA_DIR", str(tmp_root))

    # Prevent config._migrate_data() from copying the developer's real castor.db
    # into the test sandbox. Both migration helpers check for a marker file and
    # skip when it's present. Writing them before any module reload means the
    # helpers are no-ops for every test that uses this fixture.
    (tmp_root / ".migrated_v2").write_text("test skip\n")
    (tmp_root / ".migrated_from_qwe_qwe").write_text("test skip\n")

    # Close any stale DB connection before reload
    if "db" in sys.modules:
        try:
            _local = getattr(sys.modules["db"], "_local", None)
            conn = getattr(_local, "conn", None) if _local else None
            if conn is not None:
                conn.close()
            if _local is not None:
                _local.conn = None
            sys.modules["db"]._migrated = False
            sys.modules["db"]._integrity_checked = False
            sys.modules["db"]._backup_thread_started = False
        except Exception:
            pass

    # Close any stale Qdrant client before reload — same issue class
    # as the DB connection above. Qdrant in disk mode keeps a sqlite
    # file lock; a stale singleton bound to the developer's production
    # ``~/.castor/memory`` will refuse writes (readonly database) or
    # hold the lock from a test that needed to mutate. Required for
    # ``memory.reindex_from_markdown`` integration tests.
    if "memory" in sys.modules:
        try:
            sys.modules["memory"]._close_qdrant()
        except Exception:
            pass

    # Reload in dependency order.
    #
    # ``goal_runner`` / ``orchestrator`` / ``subagent`` are reloaded because
    # they keep module-level references to ``db`` (e.g. ``import db`` at
    # top). When ``db`` is reloaded above, those references still point at
    # the OLD module — so a test that exercises ``goal_runner.run`` and
    # then asserts via ``db.get_goal(...)`` was reading from two
    # different DB instances. Pollution surfaced on CI Python 3.12 as
    # ``test_skill_import`` hitting "no such table: skill_imports" right
    # after a test_provider_error_classification test ran.
    for mod_name in ("config", "db", "soul", "presets",
                     "goal_validators", "subagent", "orchestrator", "goal_runner",
                     "memory_store", "memory"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)

    try:
        yield tmp_root
    finally:
        # Close the test's DB connection before nuking the dir
        try:
            db_mod = sys.modules.get("db")
            if db_mod is not None:
                _local = getattr(db_mod, "_local", None)
                conn = getattr(_local, "conn", None) if _local else None
                if conn is not None:
                    conn.close()
                if _local is not None:
                    _local.conn = None
                db_mod._migrated = False
                db_mod._integrity_checked = False
                db_mod._backup_thread_started = False
        except Exception:
            pass
        # Close + reset Qdrant client BEFORE rmtree so the sqlite-style
        # lock on the disk-mode collection is released. Without this, the
        # first rmtree pass leaves orphan WAL/lock files which causes the
        # "8157 leaked tempdirs / 24 GB" leak we hit in dev.
        if "memory" in sys.modules:
            try:
                sys.modules["memory"]._close_qdrant()
            except Exception:
                pass
        # Two-pass rmtree: first pass with ignore_errors handles the easy
        # case. If the dir still exists (lock contention, vanishing FDs),
        # retry once after a tiny pause so background fsync / WAL close
        # has a chance to land. Final ``ignore_errors`` keeps the suite
        # from dying on a partial cleanup — but the session-scoped
        # ``_sweep_leaked_tempdirs`` pass at the top of this module
        # catches anything that gets through.
        shutil.rmtree(tmp_root, ignore_errors=True)
        if tmp_root.exists():
            time.sleep(0.05)
            shutil.rmtree(tmp_root, ignore_errors=True)
        for mod_name in ("config", "db", "soul", "presets",
                         "goal_validators", "subagent", "orchestrator", "goal_runner",
                         "memory_store", "memory"):
            if mod_name in sys.modules:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass


@pytest.fixture
def mock_llm(monkeypatch):
    """Patch providers.get_client() to return a deterministic fake client.

    The fake client exposes ``chat.completions.create(**kw)`` which returns a
    streaming-compatible response with text ``"ok"`` and no tool calls.  Tests
    that need a specific reply can override via ``mock_llm.reply = "..."``.
    """

    class _Holder:
        reply = "ok"

    holder = _Holder()

    class _FakeDelta:
        def __init__(self, content="", finish=None):
            self.content = content
            self.tool_calls = None
            self.role = "assistant"
            self.reasoning_content = None
            self.reasoning = None

    class _FakeChunk:
        def __init__(self, content="", finish=None):
            self.choices = [
                types.SimpleNamespace(
                    delta=_FakeDelta(content),
                    finish_reason=finish,
                    message=_FakeDelta(content),
                )
            ]
            self.usage = None
            self.id = "fake"
            self.model = "fake-model"

    class _FakeCompletions:
        def __init__(self, holder):
            self._holder = holder

        def create(self, **kw):
            # Always return a streaming generator (run_loop always passes stream=True)
            def _gen():
                yield _FakeChunk(content=self._holder.reply, finish=None)
                yield _FakeChunk(content="", finish="stop")

            return _gen()

    class _FakeClient:
        def __init__(self, holder):
            self.chat = types.SimpleNamespace(completions=_FakeCompletions(holder))

    client = _FakeClient(holder)

    import providers
    monkeypatch.setattr(providers, "get_client", lambda: client, raising=False)
    monkeypatch.setattr(providers, "get_model", lambda: "fake-model", raising=False)
    return holder
