"""Hygiene around aborted turns: dedup 'Stopped.' replies + auto-dismiss
prior aborts when a new turn starts.

Background: during long Goals-runtime testing sessions with frequent server
restarts, the chat thread accumulated 8 back-to-back '⏹ Stopped.' assistant
messages and 9 aborted agent_runs flagged as resumable. Both rooted in the
same problem — a single user-visible "I pressed Ctrl-C" event fires the
abort_event via multiple paths (WS disconnect + lifespan teardown), and the
abort/resume system has no de-duplication.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
#  Dedup: '⏹ Stopped.' assistant message
# ─────────────────────────────────────────────────────────────────────────────


def test_is_duplicate_stop_reply_true_when_prev_is_stop(qwe_temp_data_dir):
    """If the last assistant message is '⏹ Stopped.', another stop reply
    is treated as a duplicate and should NOT be persisted."""
    import db
    import agent
    import threads as threads_mod

    t = threads_mod.create("test thread")
    db.save_message("user", "hi", thread_id=t["id"])
    db.save_message("assistant", "⏹ Stopped.", thread_id=t["id"])

    assert agent._is_duplicate_stop_reply("⏹ Stopped.", t["id"]) is True


def test_is_duplicate_stop_reply_false_for_first_stop(qwe_temp_data_dir):
    """The FIRST stop reply in a thread is NOT a duplicate — it must be saved
    so the user sees the abort happened."""
    import db
    import agent
    import threads as threads_mod

    t = threads_mod.create("test thread")
    db.save_message("user", "do something", thread_id=t["id"])
    # No prior assistant message yet.
    assert agent._is_duplicate_stop_reply("⏹ Stopped.", t["id"]) is False


def test_is_duplicate_stop_reply_false_when_prev_is_normal(qwe_temp_data_dir):
    """A stop reply after a normal assistant message is NOT a duplicate."""
    import db
    import agent
    import threads as threads_mod

    t = threads_mod.create("test thread")
    db.save_message("user", "hi", thread_id=t["id"])
    db.save_message("assistant", "Sure, here's the result: 42.", thread_id=t["id"])
    assert agent._is_duplicate_stop_reply("⏹ Stopped.", t["id"]) is False


def test_is_duplicate_stop_reply_false_for_non_stop_reply(qwe_temp_data_dir):
    """Non-stop replies are never treated as duplicates of anything."""
    import agent
    # Even if there was a prior stop, a normal reply should always save
    assert agent._is_duplicate_stop_reply("Here's your answer.", "any_thread") is False


def test_is_duplicate_stop_reply_no_thread_returns_false(qwe_temp_data_dir):
    """CLI / one-shot runs have no thread — no dedup possible, save it."""
    import agent
    assert agent._is_duplicate_stop_reply("⏹ Stopped.", None) is False
    assert agent._is_duplicate_stop_reply("⏹ Stopped.", "") is False


# ─────────────────────────────────────────────────────────────────────────────
#  Auto-dismiss prior aborts when new turn starts
# ─────────────────────────────────────────────────────────────────────────────


def test_insert_agent_run_dismisses_prior_aborts(qwe_temp_data_dir):
    """Starting a fresh turn on a thread auto-dismisses prior aborted runs.

    Set up 3 aborted runs by direct DB insert (bypassing the auto-dismiss
    logic) so we can verify a single new insert_agent_run reaps all of
    them in one go.
    """
    import db
    import time as _t

    conn = db._get_conn()
    now = _t.time()
    aborted_ids = []
    for i in range(3):
        cur = conn.execute(
            "INSERT INTO agent_runs (thread_id, source, started_at, status) "
            "VALUES (?, 'web', ?, 'aborted')",
            ("t_test", now - 100 + i),
        )
        aborted_ids.append(int(cur.lastrowid))
    conn.commit()

    # Sanity: all three are aborted + not dismissed
    cnt = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE thread_id='t_test' "
        "AND status='aborted' AND dismissed_at IS NULL"
    ).fetchone()[0]
    assert cnt == 3

    # Start a fresh turn — should auto-dismiss the 3 prior aborts
    new_run = db.insert_agent_run(
        thread_id="t_test", source="web", started_at=now,
        status="running",
    )

    cnt_after = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE thread_id='t_test' "
        "AND status='aborted' AND dismissed_at IS NULL"
    ).fetchone()[0]
    assert cnt_after == 0
    # The 3 prior runs still exist, just dismissed
    cnt_total_aborted = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE thread_id='t_test' "
        "AND status='aborted'"
    ).fetchone()[0]
    assert cnt_total_aborted == 3
    # New run was inserted and is NOT dismissed
    new_row = conn.execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (new_run,)
    ).fetchone()
    assert new_row[0] is None


def test_insert_agent_run_keeps_only_one_undismissed_after_loop(qwe_temp_data_dir):
    """The real-world scenario: 5 server restarts each create a new aborted
    run. With auto-dismiss, only the MOST RECENT aborted run is undismissed
    at any time — the resume banner shows 1 (or 0 after start), not 5."""
    import db
    import time as _t

    # Simulate 5 server-restart cycles: each starts a run, then aborts it.
    for _ in range(5):
        rid = db.insert_agent_run(
            thread_id="t_loop", source="web", started_at=_t.time(),
            status="running",
        )
        db.finalize_agent_run(rid, finished_at=_t.time(), duration_ms=100,
                              status="aborted")

    # Count undismissed aborted runs — should be just 1 (the most recent),
    # because each `insert_agent_run` after the first dismissed prior aborts.
    conn = db._get_conn()
    cnt = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE thread_id='t_loop' "
        "AND status='aborted' AND dismissed_at IS NULL"
    ).fetchone()[0]
    assert cnt == 1, (
        "expected 1 undismissed abort (most recent) after 5 server-restart "
        f"cycles, got {cnt}"
    )


def test_insert_agent_run_does_not_dismiss_other_threads(qwe_temp_data_dir):
    """Auto-dismiss is per-thread — aborts on a DIFFERENT thread stay alive."""
    import db
    import time as _t

    # Aborted run on thread A
    rid_a = db.insert_agent_run(
        thread_id="t_a", source="web", started_at=_t.time(), status="running",
    )
    db.finalize_agent_run(rid_a, finished_at=_t.time(), duration_ms=100,
                          status="aborted")
    # Start fresh on thread B
    db.insert_agent_run(
        thread_id="t_b", source="web", started_at=_t.time(), status="running",
    )
    # A's aborted run is untouched
    conn = db._get_conn()
    row = conn.execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid_a,)
    ).fetchone()
    assert row[0] is None


def test_resume_run_does_not_dismiss(qwe_temp_data_dir):
    """A run with resumed_from_run_id set is a RESUME — we'd be dismissing
    the very row we're resuming from. Skip dismissal in that case."""
    import db
    import time as _t

    rid_original = db.insert_agent_run(
        thread_id="t_x", source="web", started_at=_t.time(), status="running",
    )
    db.finalize_agent_run(rid_original, finished_at=_t.time(), duration_ms=100,
                          status="aborted")

    # Now start a resume run pointing at it
    db.insert_agent_run(
        thread_id="t_x", source="web", started_at=_t.time(),
        status="running", resumed_from_run_id=rid_original,
    )

    # The original is NOT dismissed because this insert was a resume
    conn = db._get_conn()
    row = conn.execute(
        "SELECT dismissed_at FROM agent_runs WHERE id=?", (rid_original,)
    ).fetchone()
    assert row[0] is None


def test_insert_agent_run_returns_correct_id(qwe_temp_data_dir):
    """Auto-dismiss logic must not break the contract that insert_agent_run
    returns the id of the row it inserted (not e.g. the last dismissed id)."""
    import db
    import time as _t

    rid_old = db.insert_agent_run(
        thread_id="t_z", source="web", started_at=_t.time(), status="running",
    )
    db.finalize_agent_run(rid_old, finished_at=_t.time(), duration_ms=100,
                          status="aborted")

    rid_new = db.insert_agent_run(
        thread_id="t_z", source="web", started_at=_t.time(), status="running",
    )
    assert rid_new > rid_old
    # Verify by reading back
    conn = db._get_conn()
    row = conn.execute(
        "SELECT id, status FROM agent_runs WHERE id=?", (rid_new,)
    ).fetchone()
    assert row[0] == rid_new
    assert row[1] == "running"
