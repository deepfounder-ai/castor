"""Per-goal workspace isolation.

Each goal runs in ``~/.castor/workspace/goals/<goal_id>/`` so the
orchestrator doesn't inherit hundreds of stale files from prior goals.

Failure mode this prevents: g_bd9d9285ad8b4548 burnt $2.16 in 9.9 min
flailing on the SHARED workspace's leftover linkedin_invites_log.txt /
screenshot_*.png from g_0937821f088f4580 — 60+ shell rounds with zero
write_file / dispatch_subagent, then the acceptance gate exhausted with
all 3 subtasks still ``pending``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def fresh_castor(qwe_temp_data_dir, monkeypatch):
    """Reset module-level WORKSPACE constants so tests see the temp dir."""
    import importlib
    import config
    import tools
    import goal_validators
    importlib.reload(config)
    importlib.reload(tools)
    importlib.reload(goal_validators)
    return config


# ─────────────────────────────────────────────────────────────────────────────
# config.goal_workspace helper
# ─────────────────────────────────────────────────────────────────────────────


def test_goal_workspace_creates_dir(fresh_castor):
    import config
    p = config.goal_workspace("g_unit_test_1")
    assert p.exists() and p.is_dir()
    assert p == config.WORKSPACE_DIR / "goals" / "g_unit_test_1"


def test_goal_workspace_idempotent(fresh_castor):
    import config
    p1 = config.goal_workspace("g_unit_test_2")
    p1.joinpath("marker.txt").write_text("preserved")
    p2 = config.goal_workspace("g_unit_test_2")
    assert p1 == p2
    assert p2.joinpath("marker.txt").read_text() == "preserved"


def test_goal_workspace_rejects_empty(fresh_castor):
    import config
    with pytest.raises(ValueError):
        config.goal_workspace("")


# ─────────────────────────────────────────────────────────────────────────────
# tools._resolve_path under per-goal ctx
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_path_relative_anchored_at_goal_workspace(fresh_castor, monkeypatch):
    """A relative ``leads.csv`` lands in the goal's workspace, not the shared one."""
    import config
    import tools
    from turn_context import TurnContext

    goal_ws = config.goal_workspace("g_res1")
    ctx = TurnContext(goal_id="g_res1", workspace_root=str(goal_ws))
    monkeypatch.setattr(tools, "_get_turn_ctx", lambda: ctx)

    resolved = tools._resolve_path("leads.csv", for_write=True)
    # Goal subtree, not the shared root
    assert str(resolved).startswith(str(goal_ws.resolve()))
    assert str(resolved).endswith("/leads.csv")


def test_resolve_path_rewrites_shared_workspace_into_goal(fresh_castor, monkeypatch):
    """The orchestrator's habit of writing ``~/.castor/workspace/foo.csv``
    gets silently rewritten into the goal's workspace — otherwise model
    behaviour bypasses isolation."""
    import config
    import tools
    from turn_context import TurnContext

    goal_ws = config.goal_workspace("g_res2")
    ctx = TurnContext(goal_id="g_res2", workspace_root=str(goal_ws))
    monkeypatch.setattr(tools, "_get_turn_ctx", lambda: ctx)

    # Absolute path targeting the SHARED root
    shared_target = str(config.WORKSPACE_DIR / "outside.txt")
    resolved = tools._resolve_path(shared_target, for_write=True)
    assert str(resolved).startswith(str(goal_ws.resolve()))
    assert str(resolved).endswith("/outside.txt")


def test_resolve_path_no_ctx_uses_shared_workspace(fresh_castor, monkeypatch):
    """CLI / Telegram / scheduler runs (no goal ctx) keep the flat shared dir."""
    import config
    import tools

    monkeypatch.setattr(tools, "_get_turn_ctx", lambda: None)

    resolved = tools._resolve_path("notes.md", for_write=True)
    # Lands in the shared workspace, NOT under workspace/goals/
    assert str(resolved).startswith(str(config.WORKSPACE_DIR.resolve()))
    assert "/goals/" not in str(resolved)


def test_resolve_path_doesnt_double_prefix_goal_subtree(fresh_castor, monkeypatch):
    """If the model passes an already-goal-scoped path, don't re-prefix it."""
    import config
    import tools
    from turn_context import TurnContext

    goal_ws = config.goal_workspace("g_res3")
    ctx = TurnContext(goal_id="g_res3", workspace_root=str(goal_ws))
    monkeypatch.setattr(tools, "_get_turn_ctx", lambda: ctx)

    target = str(goal_ws / "deliverable.md")
    resolved = tools._resolve_path(target, for_write=True)
    # NOT ".../goals/g_res3/goals/g_res3/deliverable.md"
    assert str(resolved) == str((goal_ws / "deliverable.md").resolve())


# ─────────────────────────────────────────────────────────────────────────────
# goal_validators with workspace_root override
# ─────────────────────────────────────────────────────────────────────────────


def test_validator_files_exist_in_goal_workspace(fresh_castor):
    import config
    import goal_validators

    goal_ws = config.goal_workspace("g_val1")
    goal_ws.joinpath("output.csv").write_text("a,b,c")

    crit = {"kind": "files_exist", "spec": {"paths": ["output.csv"]}}
    passed, _ = goal_validators.run_validator(crit, workspace_root=goal_ws)
    assert passed is True

    # Same criterion against the SHARED workspace (no override): FAIL —
    # output.csv doesn't exist there.
    passed, remediation = goal_validators.run_validator(crit)
    assert passed is False
    assert "output.csv" in remediation


def test_validator_regex_in_file_per_goal(fresh_castor):
    import config
    import goal_validators

    goal_ws = config.goal_workspace("g_val2")
    goal_ws.joinpath("report.md").write_text("## Findings\n- 100 invites sent")

    crit = {
        "kind": "regex_in_file",
        "spec": {
            "path": "report.md",
            "pattern": r"100 invites sent",
        },
    }
    passed, _ = goal_validators.run_validator(crit, workspace_root=goal_ws)
    assert passed is True


def test_validator_shared_path_rewritten_to_goal_workspace(fresh_castor):
    """A done_condition with the shared-workspace path resolves to the
    goal's workspace when workspace_root is set — symmetric with the
    writer's rewrite in tools._resolve_path."""
    import config
    import goal_validators

    goal_ws = config.goal_workspace("g_val3")
    goal_ws.joinpath("invoice.pdf").write_text("dummy")

    crit = {
        "kind": "files_exist",
        "spec": {
            "paths": [str(config.WORKSPACE_DIR / "invoice.pdf")],
        },
    }
    passed, _ = goal_validators.run_validator(crit, workspace_root=goal_ws)
    assert passed is True


def test_validator_min_count_glob_in_goal_workspace(fresh_castor):
    import config
    import goal_validators

    goal_ws = config.goal_workspace("g_val4")
    for i in range(5):
        goal_ws.joinpath(f"item_{i}.json").write_text("{}")

    crit = {"kind": "min_count", "spec": {"glob": "item_*.json", "min": 3}}
    passed, _ = goal_validators.run_validator(crit, workspace_root=goal_ws)
    assert passed is True


def test_validator_shell_runs_in_goal_workspace(fresh_castor):
    """The shell validator's cwd must be the goal workspace, not shared —
    otherwise `wc -l *.csv` counts the wrong directory."""
    import config
    import goal_validators

    goal_ws = config.goal_workspace("g_val5")
    goal_ws.joinpath("leads.csv").write_text("a\nb\nc\nd\ne\n")  # 5 lines

    crit = {
        "kind": "shell_returns_zero",
        "spec": {
            "cmd": "test $(wc -l < leads.csv) -ge 5",
            "timeout": 5,
        },
    }
    passed, remediation = goal_validators.run_validator(crit, workspace_root=goal_ws)
    assert passed is True, remediation


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: previous goal's files invisible to the next goal
# ─────────────────────────────────────────────────────────────────────────────


def test_two_goals_dont_see_each_others_files(fresh_castor, monkeypatch):
    """The whole point: g_alpha writes leads.csv → g_beta starts, can't
    see leads.csv via either the validator or a relative-path read."""
    import config
    import goal_validators
    import tools
    from turn_context import TurnContext

    # Goal alpha writes its deliverable
    alpha_ws = config.goal_workspace("g_alpha")
    ctx_alpha = TurnContext(goal_id="g_alpha", workspace_root=str(alpha_ws))
    monkeypatch.setattr(tools, "_get_turn_ctx", lambda: ctx_alpha)
    alpha_path = tools._resolve_path("leads.csv", for_write=True)
    alpha_path.write_text("alpha lead 1\nalpha lead 2\n")
    assert alpha_path.exists()

    # Goal beta starts — its own workspace is empty
    beta_ws = config.goal_workspace("g_beta")
    crit = {"kind": "files_exist", "spec": {"paths": ["leads.csv"]}}
    passed, remediation = goal_validators.run_validator(crit, workspace_root=beta_ws)
    assert passed is False, "beta MUST NOT see alpha's leads.csv"
    assert "leads.csv" in remediation

    # And alpha's validator still sees its own file
    passed_alpha, _ = goal_validators.run_validator(crit, workspace_root=alpha_ws)
    assert passed_alpha is True
