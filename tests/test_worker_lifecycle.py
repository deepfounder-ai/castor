"""Worker process integration tests.

These tests spawn the worker as a real subprocess so we exercise the full
lifecycle: signal handling, lease heartbeat, claim_next_goal across process
boundaries. Pytest's `qwe_temp_data_dir` fixture isn't usable directly
(the subprocess has its own module state) — we set CASTOR_DATA_DIR via env
and the subprocess picks it up at import time.

The agent.run() inside the subprocess hits a real LLM provider, so these
tests need it mocked. We do that by replacing providers.get_client with a
deterministic fake via a small "shim module" that's imported before agent
in the subprocess. The shim is auto-imported by pointing PYTHONSTARTUP at it
when launching the worker.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


_SHIM_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "worker_shim.py"


def _make_shim_dir(tmp_root: Path) -> Path:
    """Drop a sitecustomize.py that monkey-patches agent.run in the worker.

    sitecustomize is auto-imported by Python's site init when on PYTHONPATH.
    We point at the static fixture file so there's no f-string / heredoc
    escaping to worry about; the scripted reply is controlled via env var.
    """
    shim_dir = tmp_root / "_test_site"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(_SHIM_FIXTURE.read_text())
    return shim_dir


@pytest.fixture
def worker_env(tmp_path):
    """Set up an isolated CASTOR_DATA_DIR with the shim wired in via sitecustomize."""
    data_dir = tmp_path / "castor_data"
    data_dir.mkdir()
    # Mark migration as already done so the subprocess doesn't try copying real data
    (data_dir / ".migrated_v2").write_text("test")
    (data_dir / ".migrated_from_qwe_qwe").write_text("test")

    shim_dir = _make_shim_dir(tmp_path)
    # Scripted reply for the fake agent.run inside the worker subprocess.
    # The fixture reads it via os.environ.

    env = os.environ.copy()
    env["CASTOR_DATA_DIR"] = str(data_dir)
    env["CASTOR_DB_PATH"] = str(data_dir / "test.db")
    # shim_dir first → sitecustomize.py is auto-imported by Python before
    # anything else (gives us a chance to monkey-patch agent.run).
    env["PYTHONPATH"] = (
        str(shim_dir) + os.pathsep + str(REPO_ROOT) +
        (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")
    )
    # Tell the shim what to make agent.run return.
    env["CASTOR_TEST_FAKE_REPLY"] = "done"
    # Suppress noisy backup/synth threads if they have env hooks
    env["CASTOR_DISABLE_BACKGROUND"] = "1"

    # Bootstrap the DB schema in this dir BEFORE the subprocess starts —
    # easier than racing against the subprocess to create_goal.
    # Run a one-shot Python that imports config + db with this env in effect.
    bootstrap = subprocess.run(
        [sys.executable, "-c", "import config, db; db._get_conn(); print('OK')"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert bootstrap.returncode == 0, f"bootstrap failed:\n{bootstrap.stderr}"

    yield env, data_dir


def _create_goal_in(env, data_dir, user_input: str = "say done") -> str:
    """Insert a goal directly via a child Python so we use the same DB env."""
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import db, sys; "
            f"sys.stdout.write(db.create_goal(user_input={user_input!r}, source='cli'))",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"create_goal failed:\n{proc.stderr}"
    return proc.stdout.strip()


def _get_goal_in(env, goal_id: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import db, json, sys; "
            f"sys.stdout.write(json.dumps(db.get_goal({goal_id!r})))",
        ],
        env=env, capture_output=True, text=True, timeout=30,
    )
    import json
    assert proc.returncode == 0, f"get_goal failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_worker_once_picks_up_pending_goal_and_marks_done(worker_env):
    """End-to-end happy path: create goal → worker --once → goal marked done."""
    env, data_dir = worker_env
    gid = _create_goal_in(env, data_dir, "do thing")

    # Run worker in --once mode (claim one, run it, exit).
    proc = subprocess.run(
        [sys.executable, "-m", "worker", "--once"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"worker --once failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    g = _get_goal_in(env, gid)
    assert g["status"] == "done", (
        f"goal status={g['status']} (expected 'done')\n"
        f"worker stderr:\n{proc.stderr}"
    )
    assert g["result"] == "done"  # matches the shim's scripted_reply
    assert g["worker_id"] is None  # lease released on completion
