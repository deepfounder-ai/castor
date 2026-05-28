"""Daily anti-pattern coach over ``agent_runs`` + ``goals``.

Inspired by Microsoft's `AI Engineer Coach`_ VS Code extension: scan the
last N days of session data for known degenerate patterns, write a
concise markdown summary to ``memory`` so recall surfaces it on the
next turn.

Local-first, opt-in (``setting:coach_enabled``, default 0). Scheduled
via :func:`scheduler._register_coach` as ``__coach_daily__``; routes
through ``scheduler._execute_task``'s fast path (no LLM, no
``agent.run``) — same cost-leak class we fixed for
``__synthesis_continuous__`` in v0.23.2.

Rule set is intentionally small (6 detectors) and additive. Each rule
returns a :class:`Finding` or ``None``. The report function aggregates
findings into a markdown block:

  ## Castor coach — 2026-05-27 (7-day window)

  - **mega_session**: 3 sessions ran >30 min (max 142 min).
  - **cost_outlier**: 1 run cost >$5 (g_0937821f — $5.11).
  - ...

Detection logic is dead simple SQL over ``agent_runs`` /
``goal_events`` / ``goals``. No LLM call — coach output is deterministic
and reproducible.

.. _AI Engineer Coach: https://github.com/microsoft/AI-Engineering-Coach
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import config
import db
import logger

_log = logger.get("coach")

# ─────────────────────────────────────────────────────────────────────────────
# Rule definitions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One detected anti-pattern. ``severity`` is a 0-100 ordinal — used
    for sorting in the report (highest first); not a precise score."""
    rule_id: str
    severity: int            # 0-100, sort order in the report
    summary: str             # one-line headline
    detail: str = ""         # optional second line with the offending IDs
    recommendation: str = ""  # actionable suggestion


# ── Rule 1: mega session ─────────────────────────────────────────────────────
# A single agent_run that took >MEGA_SESSION_MIN minutes is a flag — either
# the model got stuck in a loop, or the user is on a hours-long live debug
# without checkpointing. Goal subagents (source='subagent_*') are excluded
# because they routinely run 5-15 min by design.

MEGA_SESSION_MIN = 30


def _rule_mega_session(since_ts: float) -> Finding | None:
    conn = db._get_conn()
    rows = conn.execute(
        """SELECT id, duration_ms, source, model
           FROM agent_runs
           WHERE started_at >= ? AND duration_ms IS NOT NULL
                 AND duration_ms > ?
                 AND source NOT LIKE 'subagent_%'
           ORDER BY duration_ms DESC""",
        (since_ts, MEGA_SESSION_MIN * 60 * 1000),
    ).fetchall()
    if not rows:
        return None
    max_min = rows[0][1] // 60000
    return Finding(
        rule_id="mega_session",
        severity=60,
        summary=f"{len(rows)} session(s) ran >{MEGA_SESSION_MIN}min (longest {max_min}min).",
        detail=f"agent_runs ids: {', '.join(str(r[0]) for r in rows[:5])}",
        recommendation=(
            "Long single-shot runs are usually loops. Set "
            "``setting:max_tool_rounds`` or break the work into smaller "
            "subtasks via dispatch_subagent."
        ),
    )


# ── Rule 2: cost outlier ─────────────────────────────────────────────────────
# Any single agent_run costing >$COST_OUTLIER is worth surfacing. The
# $5 LinkedIn goal in v0.23.1 is a real case; the $9.86 synthesis-as-
# routine fire in pre-v0.23.2 is another.

COST_OUTLIER_USD = 1.00


def _rule_cost_outlier(since_ts: float) -> Finding | None:
    conn = db._get_conn()
    rows = conn.execute(
        """SELECT id, source, cost_usd, goal_id, thread_id
           FROM agent_runs
           WHERE started_at >= ? AND cost_usd IS NOT NULL
                 AND cost_usd >= ?
           ORDER BY cost_usd DESC""",
        (since_ts, COST_OUTLIER_USD),
    ).fetchall()
    if not rows:
        return None
    total = sum(r[2] for r in rows)
    top = rows[0]
    label = (
        f"goal {top[3]}" if top[3]
        else f"thread {top[4]}" if top[4]
        else f"source {top[1]}"
    )
    return Finding(
        rule_id="cost_outlier",
        severity=70,
        summary=(
            f"{len(rows)} run(s) cost >=${COST_OUTLIER_USD:.2f} "
            f"(top: ${top[2]:.2f} on {label}, total ${total:.2f})."
        ),
        detail=f"agent_runs ids: {', '.join(str(r[0]) for r in rows[:5])}",
        recommendation=(
            "Set ``budget_usd`` on goals or routine-level caps via "
            "``setting:system_task_budget_usd``. A run >$1 usually means "
            "context-window saturation or an agent loop."
        ),
    )


# ── Rule 3: capitulating goals ───────────────────────────────────────────────
# Goal marked ``done`` but plan has subtasks with status='failed' or no
# done_conditions / no goal-level criteria. Catches the "I delivered 50%
# and called it done" pattern we saw in goal3 (g_0937821f088f4580).


def _rule_capitulating_goals(since_ts: float) -> Finding | None:
    import json as _json
    conn = db._get_conn()
    rows = conn.execute(
        """SELECT id, plan, done_conditions
           FROM goals
           WHERE status='done' AND started_at >= ?""",
        (since_ts,),
    ).fetchall()
    bad: list[str] = []
    for goal_id, plan_str, dc_str in rows:
        try:
            plan = _json.loads(plan_str) if plan_str else {}
            subtasks = plan.get("subtasks") or []
        except Exception:
            subtasks = []
        failed = sum(1 for s in subtasks if s.get("status") == "failed")
        has_dc = bool(dc_str and dc_str != "null" and dc_str != "[]")
        if failed > 0 or (not subtasks and not has_dc):
            bad.append(goal_id)
    if not bad:
        return None
    return Finding(
        rule_id="capitulating_goals",
        severity=80,
        summary=(
            f"{len(bad)} goal(s) closed as 'done' with failed subtasks or "
            f"no acceptance criteria."
        ),
        detail=f"goal ids: {', '.join(bad[:5])}",
        recommendation=(
            "Set goal-level ``done_conditions`` at submission so the "
            "acceptance gate enforces the user's stated quantities. "
            "Without them the orchestrator can self-declare partial done."
        ),
    )


# ── Rule 4: shell-heavy without read_file ────────────────────────────────────
# If a thread fired >SHELL_HEAVY ``shell`` tool calls with zero
# ``read_file`` calls in the same window, the model is probably shell-
# poking the filesystem instead of using the more efficient read paths
# (the exact pattern that made our LinkedIn goal3 burn 60+ shell rounds
# inspecting prior workspace contents).

SHELL_HEAVY = 20


def _rule_shell_heavy(since_ts: float) -> Finding | None:
    conn = db._get_conn()
    # tool counts aren't stored — derive from result_preview / there's no
    # tool-call breakdown table. Fall back to a proxy: agent_runs with
    # very-low output_tokens but elevated input_tokens are likely loop-
    # poking. Skip if pricing not tracked.
    rows = conn.execute(
        """SELECT id, input_tokens, output_tokens, cost_usd, source
           FROM agent_runs
           WHERE started_at >= ?
                 AND input_tokens > 50000
                 AND output_tokens > 0
                 AND CAST(input_tokens AS REAL) / output_tokens > 50""",
        (since_ts,),
    ).fetchall()
    if len(rows) < 3:
        return None
    return Finding(
        rule_id="shell_heavy",
        severity=55,
        summary=(
            f"{len(rows)} run(s) had input-to-output token ratio >50:1 — "
            f"likely shell/file inspection loops with little new content."
        ),
        detail=f"agent_runs ids: {', '.join(str(r[0]) for r in rows[:5])}",
        recommendation=(
            "When the agent reads the same files repeatedly, prefer "
            "``fact_save`` for intermediate findings and ``memory_search`` "
            "over raw ``shell ls/cat`` chains."
        ),
    )


# ── Rule 5: orphan synthesis cost ────────────────────────────────────────────
# Catches a regression of the v0.23.2 fix. If ``__synthesis_continuous__``
# (or ``__synthesis__``) is showing nonzero cost in the window, the
# fast-path dispatch broke again and these jobs are going through
# agent.run.

SYNTHESIS_DAILY_BUDGET = 0.10


def _rule_synthesis_overspend(since_ts: float) -> Finding | None:
    conn = db._get_conn()
    row = conn.execute(
        """SELECT SUM(cost_usd) FROM agent_runs r
           JOIN scheduled_tasks t ON t.id = r.cron_id
           WHERE r.started_at >= ?
                 AND t.name IN ('__synthesis__', '__synthesis_continuous__')
                 AND r.cost_usd IS NOT NULL""",
        (since_ts,),
    ).fetchone()
    total = float(row[0] or 0.0)
    days = max(1.0, (time.time() - since_ts) / 86400.0)
    per_day = total / days
    if per_day < SYNTHESIS_DAILY_BUDGET:
        return None
    return Finding(
        rule_id="synthesis_overspend",
        severity=85,
        summary=(
            f"Synthesis crons spent ${total:.2f} over {days:.1f} day(s) "
            f"(~${per_day:.2f}/day, threshold ${SYNTHESIS_DAILY_BUDGET:.2f})."
        ),
        recommendation=(
            "v0.23.2 made ``__synthesis_continuous__`` skip the routine "
            "agent.run path. If this rule fires, ``scheduler._is_routine`` "
            "may have regressed — check that it returns False for the "
            "system task names."
        ),
    )


# ── Rule 6: no skills used ───────────────────────────────────────────────────
# Window with 0 agent_runs whose result_preview hints at a skill tool.
# We don't track tool calls per run, so the heuristic is loose — but a
# week with zero ``create_skill`` / ``tool_search`` interactions on a
# active user is worth a nudge.

NO_SKILLS_RUN_THRESHOLD = 30  # only fire if there's enough activity


def _rule_no_skills(since_ts: float) -> Finding | None:
    conn = db._get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE started_at >= ? AND source IN ('web','cli','telegram')",
        (since_ts,),
    ).fetchone()[0]
    if total < NO_SKILLS_RUN_THRESHOLD:
        return None
    # Crude proxy: scan result_preview for skill-tool markers. Real
    # implementation would need a tool-call ledger; this is a PoC.
    skill_hits = conn.execute(
        """SELECT COUNT(*) FROM agent_runs
           WHERE started_at >= ?
                 AND (result_preview LIKE '%tool_search%'
                      OR result_preview LIKE '%create_skill%'
                      OR result_preview LIKE '%canvas_%'
                      OR result_preview LIKE '%/cron%')""",
        (since_ts,),
    ).fetchone()[0]
    if skill_hits > 0:
        return None
    return Finding(
        rule_id="no_skills_used",
        severity=30,
        summary=(
            f"{total} session(s) this window, zero skill/cron tool use — "
            f"you might be missing leverage."
        ),
        recommendation=(
            "Try ``tool_search('schedule')`` to surface routines, or "
            "``create_skill`` to bake a repeated workflow into a one-call "
            "tool."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Report assembly
# ─────────────────────────────────────────────────────────────────────────────


_RULES = (
    _rule_mega_session,
    _rule_cost_outlier,
    _rule_capitulating_goals,
    _rule_shell_heavy,
    _rule_synthesis_overspend,
    _rule_no_skills,
)


def run_pass(*, lookback_days: int | None = None) -> str:
    """Execute every rule against the last ``lookback_days`` of data.

    Returns the markdown report (also saved to ``memory`` with tag="coach").
    Never raises — broken individual rules are logged and skipped.
    """
    if lookback_days is None:
        try:
            lookback_days = int(config.get("coach_lookback_days") or 7)
        except (TypeError, ValueError):
            lookback_days = 7

    since_ts = time.time() - lookback_days * 86400
    today = datetime.utcfromtimestamp(time.time()).strftime("%Y-%m-%d")

    findings: list[Finding] = []
    for rule in _RULES:
        try:
            f = rule(since_ts)
        except Exception:
            _log.exception(f"coach rule {rule.__name__} crashed — skipped")
            continue
        if f is not None:
            findings.append(f)

    findings.sort(key=lambda f: -f.severity)

    if not findings:
        report = (
            f"## Castor coach — {today} ({lookback_days}-day window)\n\n"
            f"No anti-patterns detected. {len(_RULES)} rules ran clean."
        )
    else:
        lines = [
            f"## Castor coach — {today} ({lookback_days}-day window)",
            "",
            f"{len(findings)} finding(s):",
            "",
        ]
        for f in findings:
            lines.append(f"- **{f.rule_id}**: {f.summary}")
            if f.detail:
                lines.append(f"  - {f.detail}")
            if f.recommendation:
                lines.append(f"  - → {f.recommendation}")
        report = "\n".join(lines)

    # Persist to memory so recall surfaces the report on the next turn.
    # Best-effort — never break the cron because of a memory write hiccup.
    try:
        import memory
        memory.save(report, tag="coach")
    except Exception:
        _log.exception("coach: failed to save report to memory")

    # Archive copy on disk so the user can scroll back through reports
    # without a memory search.
    try:
        out_dir = config.DATA_DIR / "uploads"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"coach-{today}.md"
        out_path.write_text(report, encoding="utf-8")
    except Exception:
        _log.exception("coach: failed to write archive copy")

    return report
