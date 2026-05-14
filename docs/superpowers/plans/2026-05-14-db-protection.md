# DB Protection System Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent user data loss from SQLite corruption by adding rolling hot backups, startup integrity check with auto-restore, and graceful WAL flush on shutdown.

**Architecture:** All backup/restore logic lives in `db.py` (single responsibility, follows existing pattern). `server.py` lifespan wires in startup check + backup scheduler + clean shutdown. CLI path catches KeyboardInterrupt and flushes WAL before exit. No new files — keeps the module surface small.

**Tech Stack:** Python stdlib `sqlite3` (`.backup()` API), `threading`, `shutil`, `pathlib`. No new dependencies.

---

## File Map

| File | What changes |
|---|---|
| `db.py` | Add 6 functions: `take_backup`, `_prune_backups`, `latest_backup`, `check_and_restore`, `graceful_shutdown`, `start_backup_scheduler`. Integrate `check_and_restore` into `_get_conn`. |
| `server.py` | Lifespan startup: call `db.check_and_restore()` + `db.start_backup_scheduler()`. Lifespan shutdown: call `db.graceful_shutdown()`. Signal handler: call `db.graceful_shutdown()` before re-raising. |
| `cli.py` | Wrap main loop's `KeyboardInterrupt` with `db.graceful_shutdown()` call. |
| `tests/test_db_protection.py` | New test file — 12 tests covering all new functions. |
| `pyproject.toml` | Add `test_db_protection` to covered modules (nothing to add — `db.py` already covered). |

---

## Task 1: Core backup functions in `db.py`

**Files:**
- Modify: `db.py` (add after the existing imports block, before `_get_conn`)
- Create: `tests/test_db_protection.py`

### Step 1: Write failing tests first

- [ ] Create `tests/test_db_protection.py`:

```python
"""Tests for db.py backup/restore/graceful-shutdown functions."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


# ── take_backup ─────────────────────────────────────────────────────────────

def test_take_backup_creates_file(qwe_temp_data_dir):
    import db
    db._get_conn()  # ensure DB exists
    result = db.take_backup("test")
    assert result is not None
    assert result.exists()
    assert result.name.endswith("_test.db")


def test_take_backup_is_readable(qwe_temp_data_dir):
    import db
    db._get_conn()
    path = db.take_backup("readcheck")
    assert path is not None
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert any(r[0] == "kv" for r in rows)


def test_take_backup_prunes_old(qwe_temp_data_dir):
    import db
    db._get_conn()
    # Create MAX_BACKUPS+2 backups — oldest should be pruned
    original_max = db.MAX_BACKUPS
    db.MAX_BACKUPS = 3
    try:
        paths = [db.take_backup(f"b{i}") for i in range(5)]
        backup_dir = Path(db.config.DATA_DIR) / "db_backups"
        remaining = list(backup_dir.glob("castor_*.db"))
        assert len(remaining) <= 3
    finally:
        db.MAX_BACKUPS = original_max


# ── latest_backup ────────────────────────────────────────────────────────────

def test_latest_backup_none_when_no_backups(qwe_temp_data_dir):
    import db
    assert db.latest_backup() is None


def test_latest_backup_returns_most_recent(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.take_backup("first")
    time.sleep(0.01)
    second = db.take_backup("second")
    assert db.latest_backup() == second


# ── check_and_restore ────────────────────────────────────────────────────────

def test_check_and_restore_ok_on_fresh_db(qwe_temp_data_dir):
    import db
    db._get_conn()
    assert db.check_and_restore() is True


def test_check_and_restore_ok_when_no_db_file(qwe_temp_data_dir):
    import db
    import config
    p = Path(config.DB_PATH)
    if p.exists():
        p.unlink()
    assert db.check_and_restore() is True


def test_check_and_restore_detects_corruption(qwe_temp_data_dir):
    import db
    import config
    db._get_conn()
    p = Path(config.DB_PATH)
    # Write garbage into the file to corrupt it
    p.write_bytes(b"THIS IS NOT A SQLITE DATABASE" * 100)
    assert db.check_and_restore() is False  # no backup available → False


def test_check_and_restore_restores_from_backup(qwe_temp_data_dir):
    import db
    import config
    db._get_conn()
    db.kv_set("canary", "alive")
    db.take_backup("before_corrupt")

    # Reset ALL module-level state so check_and_restore() fires again
    db._local.conn = None
    db._migrated = False
    db._integrity_checked = False  # critical: without this the guard skips the check

    # Corrupt the database
    Path(config.DB_PATH).write_bytes(b"CORRUPTED" * 200)

    result = db.check_and_restore()
    assert result is True
    # After restore, DB should be readable and have the canary value
    db._local.conn = None
    db._migrated = False
    assert db.kv_get("canary") == "alive"


# ── graceful_shutdown ────────────────────────────────────────────────────────

def test_graceful_shutdown_does_not_raise(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.graceful_shutdown()  # must not raise


def test_graceful_shutdown_closes_connection(qwe_temp_data_dir):
    import db
    db._get_conn()
    db.graceful_shutdown()
    # After shutdown, _local.conn should be None
    assert getattr(db._local, "conn", None) is None


# ── start_backup_scheduler ───────────────────────────────────────────────────

def test_start_backup_scheduler_is_idempotent(qwe_temp_data_dir, monkeypatch):
    import db
    import threading
    # Reset global so we can test idempotency cleanly regardless of test order
    monkeypatch.setattr(db, "_backup_thread_started", False)
    db._get_conn()
    db.start_backup_scheduler()
    db.start_backup_scheduler()  # second call must not raise or spawn extra threads
    backup_threads = [t for t in threading.enumerate() if t.name == "db-backup"]
    assert len(backup_threads) == 1
```

- [ ] Run to confirm all fail:

```bash
pytest tests/test_db_protection.py -v 2>&1 | tail -20
```

Expected: all 12 FAILED with `AttributeError: module 'db' has no attribute 'take_backup'`

---

## Task 2: Implement backup functions in `db.py`

**Files:**
- Modify: `db.py` — add constants + 6 functions after line 16 (after `_migrate_lock` declaration)

- [ ] Add imports (`shutil`) and constants to `db.py` top of file — after existing imports:

```python
import shutil
```

- [ ] Add constants + 6 functions after `_migrate_lock = threading.Lock()` (line 16):

```python
# --- DB protection: backups, integrity check, graceful shutdown -------------

MAX_BACKUPS = 24          # rolling window — 24 hourly = 1 day of history
BACKUP_INTERVAL_SEC = 3600  # how often the background thread fires

_backup_thread_started = False
_backup_thread_lock = threading.Lock()
_integrity_checked = False
_integrity_lock = threading.Lock()


def take_backup(tag: str = "") -> "Path | None":
    """Hot backup using SQLite's online backup API.

    Safe to call while the database is open and being written — SQLite
    serialises the page reads automatically. Creates a file named
    castor_<unix_ts>[_<tag>].db in ~/.castor/db_backups/, then prunes
    old backups so only MAX_BACKUPS files remain.
    """
    try:
        backup_dir = Path(config.DATA_DIR) / "db_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        suffix = f"_{tag}" if tag else ""
        backup_path = backup_dir / f"castor_{ts}{suffix}.db"
        src = sqlite3.connect(str(config.DB_PATH))
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst, pages=-1)  # pages=-1 = copy everything atomically
        dst.close()
        src.close()
        _prune_backups(backup_dir)
        _log.info(f"db backup created: {backup_path.name}")
        return backup_path
    except Exception as e:
        _log.warning(f"db backup failed: {e}")
        return None


def _prune_backups(backup_dir: Path) -> None:
    """Keep only the MAX_BACKUPS most recent backup files."""
    try:
        backups = sorted(
            backup_dir.glob("castor_*.db"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in backups[:-MAX_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def latest_backup() -> "Path | None":
    """Return the most recent backup Path, or None if no backups exist."""
    try:
        backup_dir = Path(config.DATA_DIR) / "db_backups"
        backups = sorted(
            backup_dir.glob("castor_*.db"),
            key=lambda p: p.stat().st_mtime,
        )
        return backups[-1] if backups else None
    except Exception:
        return None


def check_and_restore() -> bool:
    """Integrity check on startup. Auto-restores from backup if malformed.

    Returns True if the DB is healthy (original or restored from backup).
    Returns False if malformed AND no backup was available — caller should
    let _get_conn() create a fresh database.

    Call this BEFORE _get_conn() to catch corruption before sqlite3.connect()
    tries to open a broken file (which raises sqlite3.DatabaseError and
    prevents any db access for the whole process lifetime).
    """
    db_path = Path(config.DB_PATH)
    if not db_path.exists():
        return True  # fresh install — nothing to check

    try:
        probe = sqlite3.connect(str(db_path), check_same_thread=False, timeout=3)
        result = probe.execute("PRAGMA integrity_check(1)").fetchone()
        probe.close()
        if result and result[0] == "ok":
            return True
    except sqlite3.DatabaseError as e:
        _log.error(f"database integrity check failed: {e}")
    except Exception as e:
        _log.warning(f"database probe error: {e}")
        return True  # non-corruption error (permissions etc.) — don't wipe DB

    # Database is malformed — attempt restore from latest backup
    backup = latest_backup()
    if backup:
        _log.warning(f"corrupt database — restoring from backup: {backup.name}")
        try:
            corrupted = db_path.with_name(f"castor.db.corrupted.{int(time.time())}")
            shutil.copy2(str(db_path), str(corrupted))
            shutil.copy2(str(backup), str(db_path))
            _log.info(f"database restored from {backup.name} "
                      f"(corrupted copy saved as {corrupted.name})")
            return True
        except Exception as e:
            _log.error(f"restore from backup failed: {e}")

    # No backup available — rename corrupted DB and let startup create a fresh one
    _log.error("database corrupted and no backup available — starting fresh")
    try:
        corrupted = db_path.with_name(f"castor.db.corrupted.{int(time.time())}")
        db_path.rename(corrupted)
        _log.warning(f"corrupted file saved as {corrupted.name}")
    except OSError:
        pass
    return False


def graceful_shutdown() -> None:
    """Flush WAL and close the connection for this thread cleanly.

    Call on SIGTERM / SIGINT before the process exits. Without this,
    killing the process mid-write leaves WAL pages unflushed; the next
    startup has to recover them — and if the WAL is also partially
    written, recovery can fail and corrupt the database.
    """
    try:
        conn = getattr(_local, "conn", None)
        if conn:
            row = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            # row = (busy, log_frames, checkpointed_frames)
            # log incomplete WAL checkpoints so ops can diagnose
            if row and row[0]:  # busy > 0 means some frames couldn't be checkpointed
                _log.warning(
                    f"wal_checkpoint(FULL): busy={row[0]} log={row[1]} checkpointed={row[2]} "
                    f"— other connections may still hold the WAL open"
                )
            conn.close()
            _local.conn = None
            _log.info("database flushed and closed (graceful shutdown)")
    except Exception as e:
        _log.warning(f"graceful db shutdown error (non-fatal): {e}")


def start_backup_scheduler() -> None:
    """Start a daemon thread that takes a hot backup every BACKUP_INTERVAL_SEC.

    Also takes an immediate 'startup' backup so there's always at least one
    backup from the last clean start.

    Idempotent — safe to call multiple times; only one thread is ever started.
    """
    global _backup_thread_started
    if _backup_thread_started:
        return
    with _backup_thread_lock:
        if _backup_thread_started:
            return
        _backup_thread_started = True

    # Immediate startup backup (runs synchronously before the thread starts)
    take_backup("startup")

    def _loop() -> None:
        while True:
            time.sleep(BACKUP_INTERVAL_SEC)
            try:
                take_backup()
            except Exception as e:
                _log.warning(f"scheduled backup error: {e}")

    t = threading.Thread(target=_loop, name="db-backup", daemon=True)
    t.start()
    _log.info(f"db backup scheduler started "
              f"(interval={BACKUP_INTERVAL_SEC}s, max={MAX_BACKUPS} backups)")
```

- [ ] Integrate `check_and_restore()` into `_get_conn()` — add the integrity check block at the top:

```python
def _get_conn() -> sqlite3.Connection:
    global _migrated, _integrity_checked
    # Run integrity check + auto-restore exactly once per process, before the
    # first sqlite3.connect() call. If the file is corrupted, check_and_restore()
    # renames it aside and returns False; sqlite3.connect() then creates a fresh DB.
    if not _integrity_checked:
        with _integrity_lock:
            if not _integrity_checked:
                check_and_restore()
                _integrity_checked = True
    conn = getattr(_local, "conn", None)
    # ... rest of existing code unchanged
```

- [ ] Run the tests:

```bash
pytest tests/test_db_protection.py -v 2>&1 | tail -20
```

Expected: all 12 PASS

- [ ] Run full test suite to confirm no regressions:

```bash
pytest tests/ -q --ignore=tests/test_serial_port_skill.py --ignore=tests/test_ws_attachments.py 2>&1 | tail -8
```

Expected: 800+ passed

- [ ] Commit:

```bash
git add db.py tests/test_db_protection.py
git commit -m "feat(db): rolling backups, startup integrity check, graceful shutdown

- take_backup() uses SQLite online backup API (hot, safe during writes)
- check_and_restore() runs before first connection — auto-restores from
  latest backup if DB is malformed, renames corrupted file aside
- graceful_shutdown() does PRAGMA wal_checkpoint(FULL) before close
- start_backup_scheduler() takes hourly backups, keeps last 24
- Integrated check_and_restore into _get_conn() (runs once per process)"
```

---

## Task 3: Wire into `server.py` lifespan + signal handler

**Files:**
- Modify: `server.py` — lifespan startup block + lifespan shutdown block + `_signal_handler`

- [ ] In `server.py` lifespan startup (around line 392), add DB check + backup scheduler **before** any other startup code (DB must be healthy before `db.kv_get()` on line 394):

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── DB integrity check + backup scheduler (must be first) ──
    try:
        db.start_backup_scheduler()
    except Exception as e:
        _log.warning(f"db backup scheduler startup: {e}")
    # Startup — load timezone before anything else
    tz_val = db.kv_get("tz_offset") or db.kv_get("timezone")
    # ... rest unchanged
```

- [ ] In `server.py` lifespan shutdown block (around line 448, after `yield`), add graceful DB shutdown **last** (after all other services stop):

```python
    # Shutdown
    try:
        telegram_bot.stop()
    except Exception:
        pass
    try:
        mcp_client.stop_all()
    except Exception:
        pass
    # Flush WAL and close DB cleanly — must be last
    try:
        db.graceful_shutdown()
    except Exception:
        pass
    _log.info("web server stopped")
```

- [ ] In `server.py` signal handler (lines 8–13), add graceful shutdown before the log:

```python
def _signal_handler(signum, frame):
    import traceback
    import logger as _lg
    _l = _lg.get("server")
    _l.error(f"SIGNAL {signum} received!")
    _l.error("".join(traceback.format_stack(frame)))
    # Flush WAL so the DB is not left with an open write transaction
    try:
        import db as _db
        _db.graceful_shutdown()
    except Exception:
        pass
```

- [ ] Add wiring tests to `tests/test_db_protection.py`:

```python
# ── server.py wiring ─────────────────────────────────────────────────────────

def test_lifespan_starts_backup_scheduler(qwe_temp_data_dir, monkeypatch):
    """server lifespan startup must call db.start_backup_scheduler()."""
    import db
    calls = []
    monkeypatch.setattr(db, "start_backup_scheduler", lambda: calls.append(1))
    # Import server lazily to avoid module-level side effects
    import server
    import asyncio
    async def _run():
        async with server.lifespan(server.app):
            pass
    asyncio.run(_run())
    assert len(calls) >= 1, "start_backup_scheduler was not called from lifespan"


def test_lifespan_calls_graceful_shutdown(qwe_temp_data_dir, monkeypatch):
    """server lifespan shutdown block must call db.graceful_shutdown()."""
    import db
    calls = []
    monkeypatch.setattr(db, "graceful_shutdown", lambda: calls.append(1))
    import server
    import asyncio
    async def _run():
        async with server.lifespan(server.app):
            pass
    asyncio.run(_run())
    assert len(calls) >= 1, "graceful_shutdown was not called from lifespan shutdown"
```

- [ ] Run to confirm they fail (before wiring):

```bash
pytest tests/test_db_protection.py::test_lifespan_starts_backup_scheduler tests/test_db_protection.py::test_lifespan_calls_graceful_shutdown -v 2>&1 | tail -10
```

- [ ] Run integration smoke test:

```bash
python -c "from server import app; import db; print('import OK'); db.graceful_shutdown()"
```

Expected: `import OK` with no errors

- [ ] Run full test suite:

```bash
pytest tests/ -q --ignore=tests/test_serial_port_skill.py --ignore=tests/test_ws_attachments.py 2>&1 | tail -8
```

Expected: 800+ passed

- [ ] Commit:

```bash
git add server.py
git commit -m "feat(server): wire db backup scheduler and graceful shutdown into lifespan"
```

---

## Task 4: Wire graceful shutdown into CLI path

**Files:**
- Modify: `cli.py` — find the outermost `KeyboardInterrupt` handler in `main_entry` / `interactive_loop`

- [ ] Find the CLI's main KeyboardInterrupt exit point:

```bash
grep -n "KeyboardInterrupt\|main_entry\|def main" cli.py | tail -20
```

- [ ] Wrap the top-level exit with `db.graceful_shutdown()`:

```python
except (EOFError, KeyboardInterrupt):
    try:
        db.graceful_shutdown()
    except Exception:
        pass
    console.print("\n[dim]bye[/dim]")
    sys.exit(0)
```

> Add this to every `KeyboardInterrupt` / `EOFError` branch that calls `sys.exit` or falls off the end of `main_entry`.

- [ ] Run tests:

```bash
pytest tests/ -q --ignore=tests/test_serial_port_skill.py --ignore=tests/test_ws_attachments.py 2>&1 | tail -5
```

- [ ] Commit:

```bash
git add cli.py
git commit -m "feat(cli): flush WAL on Ctrl+C / EOF before process exit"
```

---

## Task 5: Push + verify

- [ ] Push all commits:

```bash
git push
```

- [ ] Verify backup dir appears on server start:

```bash
python cli.py --web &
sleep 3
ls ~/.castor/db_backups/
kill %1
```

Expected: `castor_<ts>_startup.db` file present

- [ ] Verify `castor --doctor` output includes backup status (optional — if doctor check is added separately)

---

## Acceptance Criteria

| Scenario | Expected behaviour |
|---|---|
| Normal startup | `check_and_restore()` runs, DB healthy → no action |
| Startup after hard kill | WAL checkpoint cleans up leftovers; backup from last clean start available |
| Startup with corrupted DB + backup | Auto-restore from backup, corrupted file renamed aside, process continues |
| Startup with corrupted DB + no backup | Corrupted file renamed, fresh DB created, process continues |
| Clean Ctrl+C (web or CLI) | `graceful_shutdown()` flushes WAL before exit |
| Hourly cron | `take_backup()` fires, oldest backup pruned if > 24 |
