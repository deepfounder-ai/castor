"""Pin the v0.23.4 hardening that closes the migration race surfaced by
test_routine_runs::test_detect_missed_caps_at_ten on CI Linux runners
(architecture review H-2.1).

Two pytest fixtures both reload ``db`` so ``_migrate_lock`` is a fresh
object on each reload. A stale connection from a prior test (or a
daemon worker still wired to the old module) can race the current test's
``_apply_migrations`` on the same DB file. The first runner commits a
CREATE TABLE, the second runner hits ``sqlite3.OperationalError: table
goals already exists``.

The schema_version gate prevents this on the happy path. ``_apply_one``
now defends against the racy case by treating ``already exists`` the
same way it already treats ``duplicate column name``: forward progress
is safe, the object exists in the shape the migration wanted, log at
debug and continue.
"""
from __future__ import annotations

import sqlite3


def test_apply_one_skips_table_already_exists(monkeypatch, tmp_path):
    """A migration whose CREATE TABLE collides with an existing object
    no longer raises — it logs and continues with the next statement.
    """
    import db

    tmp = tmp_path
    db_path = tmp / "x.db"
    conn = sqlite3.connect(db_path)
    # Pre-create the table the migration would try to create.
    conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.commit()

    mig = tmp / "099_collision.sql"
    mig.write_text(
        "BEGIN;\n"
        "CREATE TABLE foo (id INTEGER PRIMARY KEY, payload TEXT);\n"
        "CREATE INDEX foo_payload_idx ON foo (payload);\n"
        "COMMIT;\n"
    )

    # Should not raise — the CREATE TABLE is harmlessly skipped, the
    # CREATE INDEX runs, the migration completes.
    db._apply_one(conn, mig)

    # Index actually got created.
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='foo_payload_idx'"
    ).fetchone()
    assert row is not None


def test_apply_one_skips_duplicate_column(monkeypatch, tmp_path):
    """Existing contract preserved — duplicate column name is still
    silently skipped (back-compat with scheduler._ensure_table that
    added columns ad-hoc before the migration ran).
    """
    import db

    tmp = tmp_path
    db_path = tmp / "x.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE bar (id INTEGER PRIMARY KEY, extant TEXT)")
    conn.commit()

    mig = tmp / "099_alter.sql"
    mig.write_text(
        "BEGIN;\n"
        "ALTER TABLE bar ADD COLUMN extant TEXT;\n"
        "ALTER TABLE bar ADD COLUMN brand_new INTEGER;\n"
        "COMMIT;\n"
    )

    db._apply_one(conn, mig)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(bar)").fetchall()]
    assert "brand_new" in cols


def test_apply_one_still_raises_real_errors(monkeypatch, tmp_path):
    """Defensive — unrelated OperationalErrors (e.g. invalid SQL,
    constraint violations) still propagate so the migration runner
    aborts and rolls back as before.
    """
    import db
    import pytest

    tmp = tmp_path
    db_path = tmp / "x.db"
    conn = sqlite3.connect(db_path)

    mig = tmp / "099_broken.sql"
    mig.write_text(
        "BEGIN;\n"
        "INSERT INTO nonexistent VALUES (1);\n"
        "COMMIT;\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        db._apply_one(conn, mig)


def test_apply_one_skips_already_existing_index(monkeypatch, tmp_path):
    """CREATE INDEX collision is silently skipped too — the "already
    exists" detector is keyword-based, not table-specific.
    """
    import db

    tmp = tmp_path
    db_path = tmp / "x.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE baz (id INTEGER PRIMARY KEY, k TEXT)")
    conn.execute("CREATE INDEX baz_k_idx ON baz (k)")
    conn.commit()

    mig = tmp / "099_idx.sql"
    mig.write_text(
        "BEGIN;\n"
        "CREATE INDEX baz_k_idx ON baz (k);\n"
        "CREATE TABLE baz_extra (id INTEGER PRIMARY KEY);\n"
        "COMMIT;\n"
    )

    db._apply_one(conn, mig)

    # The follow-up CREATE TABLE still landed even though the prior
    # CREATE INDEX raised "already exists".
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='baz_extra'"
    ).fetchone()
    assert row is not None
