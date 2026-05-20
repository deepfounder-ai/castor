"""Routine & Goal manager skill — create, edit, pause and delete Routines
(recurring cron jobs) and Goals (long-running one-off autonomous tasks) from chat.

Extends the low-level schedule_task / list_cron / remove_cron core tools with:
  - Conversational UX via INSTRUCTION (guides the agent to draft quality tasks)
  - routine_update: patch name / task / schedule without delete + recreate
  - routine_pause: toggle enable/disable without deleting
  - id_or_name resolution on all mutating routine tools (numeric ID or partial name)
  - goal_create / goal_list / goal_view / goal_pause / goal_resume / goal_abort
"""

DESCRIPTION = (
    "Create, edit, pause and delete Routines (scheduled cron jobs) "
    "and Goals (long-running autonomous tasks) from chat"
)

INSTRUCTION = """When helping with routines and goals, use the right type for the job.

━━━ CHOOSING: Routine vs Goal ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ROUTINE  — recurring, short (<5 min), scheduled (cron-style).
             «Fetch the weather every morning at 08:00»
             «Remind me to drink water every 2 hours»

  GOAL     — one-off, complex, multi-step, may run for hours.
             «Research competitors and write a market analysis report»
             «Scrape the last 100 GitHub issues and categorize them»

━━━ CREATING A ROUTINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Always do this before calling routine_create:
1. Call routine_list to show what already exists (brief output is fine).
2. Clarify WHAT the routine should do and WHEN.  Two questions max.
3. Draft the task instruction yourself.  Write it as a complete, self-contained
   directive for an autonomous sub-agent with shell, http_request, memory_save,
   and telegram_notify_owner available — but NO memory of this conversation.
   Every detail must be in the instruction text.

   Good example:
     «Fetch https://wttr.in/Moscow?format=j1 via http_request, extract
     today's max temperature and weather description, then call
     telegram_notify_owner with text "Погода: {desc}, макс {temp}°C"»

   Bad example:
     «Check the weather and send it to me»  ← too vague, will fail at runtime

4. Show the draft + proposed schedule and ask:
   «Подходит такая формулировка?» — wait for confirmation before saving.
5. Only after confirmation — call routine_create.

SCHEDULE FORMATS (confirm with user before saving):
  every 30m          — every 30 minutes
  every 2h           — every 2 hours
  daily 09:00        — every day at 09:00 (local time)
  mon 08:30          — every Monday at 08:30
  weekdays 09:00     — Mon–Fri at 09:00
  weekends 10:00     — Sat–Sun at 10:00
  every 2 days 10:00 — every other day at 10:00
  in 2h              — one-off, fires 2 hours from now

EDITING: routine_update patches name / task / schedule without recreating.
  A new dry-run executes automatically when task text changes (skip with skip_dry_run=true).
PAUSING: routine_pause toggles enabled/disabled — routine is kept intact.
DELETING: routine_delete removes permanently (also deletes its chat thread).

━━━ CREATING A GOAL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Clarify exactly what deliverables the user expects (file? summary? data?).
2. Draft a complete, self-contained task description — the orchestrator and
   subagents receive ONLY this text plus their system prompts.
3. Show the draft + any budgets and ask for confirmation before creating.
4. Call goal_create with the confirmed task.  The worker daemon picks it up
   asynchronously — creation is immediate, execution starts shortly after.
5. The user can track progress with goal_view(goal_id).

GOALS support optional acceptance criteria (done_conditions) — JSON objects
that must be satisfied before the goal is marked done:
  {"kind": "files_exist",       "spec": {"paths": ["report.md"]}}
  {"kind": "min_count",         "spec": {"glob": "skill_notes_entries*", "min": 5}}
  {"kind": "regex_in_file",     "spec": {"path": "output.txt", "pattern": "Summary:"}}
  {"kind": "http_200",          "spec": {"url": "https://example.com/api/health"}}
  {"kind": "shell_returns_zero","spec": {"cmd": "python tests/test_output.py"}}

LIFECYCLE: pending → running → done | failed | paused | aborted
  goal_pause  — ask the worker to pause at its next checkpoint
  goal_resume — put a paused goal back in the queue
  goal_abort  — permanently cancel (terminal, no auto-resume)
"""

TOOLS = [
    # ── Routines ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "routine_list",
            "description": (
                "List all user-created routines with id, name, schedule, "
                "next run time, enabled/paused status, and last run result. "
                "Always call this first before creating or editing a routine."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_create",
            "description": (
                "Create a new routine (cron job). Runs a pre-save dry-run that "
                "executes the task once with real side-effects to verify it works. "
                "Draft and confirm the task instruction with the user BEFORE calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short human-readable name, e.g. 'Morning weather'.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "Complete, self-contained task directive — must say what "
                            "to fetch, how to process it, and what action to take."
                        ),
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "When to run: 'every 30m', 'daily 09:00', 'mon 08:30', "
                            "'weekdays 09:00', 'every 2h', 'in 1h' (one-off)."
                        ),
                    },
                    "skip_dry_run": {
                        "type": "boolean",
                        "description": (
                            "Skip the pre-save dry-run. Only use when a previous "
                            "attempt failed and offered offer_skip=true."
                        ),
                    },
                },
                "required": ["name", "task", "schedule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_update",
            "description": (
                "Edit an existing routine — patch its name, task instruction, "
                "or schedule without deleting and recreating. "
                "Provide at least one of new_name / task / schedule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id_or_name": {
                        "type": "string",
                        "description": "Routine id (number) or name (partial match OK).",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "Replacement display name (optional).",
                    },
                    "task": {
                        "type": "string",
                        "description": "Replacement task instruction (optional).",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "New schedule, e.g. 'daily 10:00' (optional).",
                    },
                    "skip_dry_run": {
                        "type": "boolean",
                        "description": "Skip dry-run when task text is updated.",
                    },
                },
                "required": ["id_or_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_pause",
            "description": (
                "Pause (disable) or resume (enable) a routine without deleting it. "
                "Calling this toggles the current state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id_or_name": {
                        "type": "string",
                        "description": "Routine id (number) or name (partial match OK).",
                    },
                },
                "required": ["id_or_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_delete",
            "description": (
                "Permanently delete a routine and its associated chat thread. "
                "Use routine_pause if you only want to suspend it temporarily."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id_or_name": {
                        "type": "string",
                        "description": "Routine id (number) or name (partial match OK).",
                    },
                },
                "required": ["id_or_name"],
            },
        },
    },
    # ── Goals ────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "goal_list",
            "description": (
                "List recent goals with id, status (pending/running/done/failed/"
                "paused/aborted), first 60 chars of task, cost, and timestamps. "
                "Call this before creating a goal to avoid duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": (
                            "Filter by status: pending, running, done, failed, "
                            "paused, aborted. Omit to show all recent goals."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of goals to return (default 20).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_create",
            "description": (
                "Enqueue a new long-running Goal for the background worker. "
                "Unlike routines, goals run once and support multi-step planning, "
                "subagents, checkpoints, and structured deliverables. "
                "Confirm the task description with the user BEFORE calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Complete, self-contained task directive the orchestrator "
                            "will execute. Must describe what to research/build/fetch, "
                            "how to validate success, and what outputs to produce."
                        ),
                    },
                    "budget_usd": {
                        "type": "number",
                        "description": (
                            "Optional spending cap in USD. Goal is aborted if "
                            "LLM costs exceed this amount."
                        ),
                    },
                    "budget_seconds": {
                        "type": "integer",
                        "description": (
                            "Optional wall-clock time cap in seconds (e.g. 3600 = 1 hour). "
                            "Goal is aborted if it takes longer."
                        ),
                    },
                    "done_conditions": {
                        "type": "array",
                        "description": (
                            "Optional acceptance criteria. Each item: "
                            "{\"kind\": \"files_exist\", \"spec\": {\"paths\": [\"out.md\"]}}. "
                            "Kinds: files_exist, min_count, regex_in_file, http_200, shell_returns_zero. "
                            "All fields go inside 'spec'."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_view",
            "description": (
                "Show detailed status of a goal: full task, status, subtask plan "
                "with per-step progress, result or error, cost, and outputs. "
                "Use this to track an in-progress or completed goal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal id (starts with 'g_'), returned by goal_create.",
                    },
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_pause",
            "description": (
                "Ask the worker to pause a pending or running goal. "
                "The goal is checkpointed and can be resumed later with goal_resume. "
                "Prefer this over goal_abort when you want to pause temporarily."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal id (starts with 'g_').",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for pausing (for audit log).",
                    },
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_resume",
            "description": (
                "Put a paused goal back in the worker queue. "
                "The worker will pick it up from its last checkpoint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal id (starts with 'g_').",
                    },
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_abort",
            "description": (
                "Permanently cancel a goal. This is a terminal state — "
                "the goal will NOT auto-resume. Use goal_pause for temporary suspension."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Goal id (starts with 'g_').",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for aborting (for audit log).",
                    },
                },
                "required": ["goal_id"],
            },
        },
    },
]

# ── Internal helpers — routines ───────────────────────────────────────────────

_SYSTEM_TASK_NAMES = {"__heartbeat__", "__synthesis__", "__synthesis_continuous__"}


def _is_system_task(name: str) -> bool:
    return name in _SYSTEM_TASK_NAMES or name.startswith("__")


def _resolve_id_or_name(id_or_name: str):
    """Return ``(task_id, error_str)``.  error_str is empty string on success."""
    import db
    conn = db._get_conn()
    val = str(id_or_name).strip()

    if val.isdigit():
        tid = int(val)
        row = conn.execute(
            "SELECT id, name FROM scheduled_tasks WHERE id=?", (tid,)
        ).fetchone()
        if not row:
            return None, f"Routine #{tid} not found."
        if _is_system_task(row[1]):
            return None, f"#{tid} is a system task and cannot be modified here."
        return tid, ""

    # Partial name match — exclude system tasks
    rows = conn.execute(
        "SELECT id, name FROM scheduled_tasks WHERE name LIKE ?",
        (f"%{val}%",),
    ).fetchall()
    rows = [(r[0], r[1]) for r in rows if not _is_system_task(r[1])]
    if not rows:
        return None, (
            f"No routine matching '{val}'. "
            "Call routine_list to see all routines."
        )
    if len(rows) > 1:
        names = ", ".join(f"#{r[0]} \"{r[1]}\"" for r in rows)
        return None, (
            f"Multiple matches: {names}. "
            "Be more specific or use the numeric id."
        )
    return rows[0][0], ""


# ── Internal helpers — goals ──────────────────────────────────────────────────

def _resolve_goal_id(goal_id: str):
    """Return ``(goal_dict, error_str)``.  error_str is empty string on success."""
    import db
    val = str(goal_id).strip()
    if not val.startswith("g_"):
        return None, f"Invalid goal id '{val}'. Goal ids start with 'g_'."
    g = db.get_goal(val)
    if not g:
        return None, f"Goal '{val}' not found."
    return g, ""


# ── Tool implementations — routines ──────────────────────────────────────────


def _routine_list() -> str:
    import db
    import scheduler
    from datetime import datetime
    try:
        scheduler._ensure_table()
    except Exception:
        pass
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT id, name, schedule, next_run, last_run, enabled, "
        "       last_status, last_error, run_count "
        "FROM scheduled_tasks ORDER BY next_run"
    ).fetchall()
    if not rows:
        return "No routines yet."

    try:
        tz = scheduler._tz()
    except Exception:
        import datetime as _dt
        tz = _dt.timezone.utc

    lines = []
    for (rid, name, schedule, next_run, last_run, enabled,
         last_status, last_error, run_count) in rows:
        if _is_system_task(name):
            continue
        state = "on" if enabled else "PAUSED"
        try:
            next_dt = datetime.fromtimestamp(next_run, tz).strftime("%m-%d %H:%M")
        except Exception:
            next_dt = "?"
        last_info = ""
        if last_status == "ok":
            last_info = " [ok]"
        elif last_status == "err":
            err_short = (last_error or "err").split("\n")[0][:40]
            last_info = f" [ERR: {err_short}]"
        lines.append(
            f"#{rid} [{state}] \"{name}\" — {schedule} — next: {next_dt}"
            f" — runs: {run_count or 0}{last_info}"
        )

    if not lines:
        return "No user routines yet."
    return "\n".join(lines)


def _routine_create(args: dict) -> str:
    import scheduler
    name = args.get("name", "").strip()
    task = args.get("task", "").strip()
    schedule = args.get("schedule", "").strip()
    skip_dry_run = bool(args.get("skip_dry_run", False))

    if not name:
        return "Error: name is required."
    if not task:
        return "Error: task description is required."
    if not schedule:
        return "Error: schedule is required (e.g. 'daily 09:00' or 'every 2h')."

    result = scheduler.add(name, task, schedule, skip_dry_run=skip_dry_run)

    if "error" in result:
        msg = f"Could not create routine: {result['error']}"
        if result.get("hint"):
            msg += f"\nHint: {result['hint']}"
        if result.get("output"):
            msg += f"\nDry-run output: {result['output'][:300]}"
        if result.get("offer_skip"):
            msg += "\nYou can retry with skip_dry_run=true to save anyway."
        return msg

    repeat_label = "repeating" if result.get("repeat") else "one-off"
    reply = (
        f"Routine '{name}' created — "
        f"next run: {result.get('next_run', '?')} ({repeat_label})."
    )
    if result.get("dry_run") == "passed":
        preview = (result.get("preview") or "").strip()
        if preview:
            reply += f"\nDry-run passed. Preview:\n{preview[:300]}"
        else:
            reply += "\nDry-run passed."
    return reply


def _routine_update(args: dict) -> str:
    import db
    import scheduler
    id_or_name = str(args.get("id_or_name", "")).strip()
    new_name = (args.get("new_name") or "").strip() or None
    task = (args.get("task") or "").strip() or None
    schedule = (args.get("schedule") or "").strip() or None
    skip_dry_run = bool(args.get("skip_dry_run", False))

    if not id_or_name:
        return "Error: id_or_name is required."
    if not any([new_name, task, schedule]):
        return "Error: provide at least one of new_name, task, or schedule."

    tid, err = _resolve_id_or_name(id_or_name)
    if err:
        return f"Error: {err}"

    conn = db._get_conn()
    row = conn.execute(
        "SELECT name, task, schedule FROM scheduled_tasks WHERE id=?", (tid,)
    ).fetchone()
    if not row:
        return f"Error: routine #{tid} not found."
    cur_name, cur_task, cur_schedule = row

    # If task text changed, validate with a dry-run first
    if task and not skip_dry_run:
        try:
            import config as _cfg
            _dry_exec = (scheduler._dry_run_executor
                         if _cfg.get("routine_dry_run_mock") else None)
            dry_result = scheduler._execute_task(task, max_rounds=8,
                                                 tool_executor=_dry_exec)
            validation = scheduler._validate_dry_run(dry_result, task)
            if not validation["ok"]:
                msg = f"Dry-run failed for updated task: {validation['reason']}"
                if validation.get("hint"):
                    msg += f"\nHint: {validation['hint']}"
                if dry_result:
                    msg += f"\nOutput: {dry_result[:300]}"
                msg += "\nRetry with skip_dry_run=true to save anyway."
                return msg
        except Exception as e:
            return f"Error during dry-run: {e}"

    updates = {}
    if new_name:
        updates["name"] = new_name
    if task:
        updates["task"] = task
    if schedule:
        next_run, repeat = scheduler._parse_schedule(schedule)
        if next_run is None:
            return (
                f"Error: can't parse schedule '{schedule}'. "
                "Try 'daily 09:00', 'every 2h', or 'mon 08:30'."
            )
        updates["schedule"] = schedule
        updates["next_run"] = next_run
        updates["repeat"] = 1 if repeat else 0

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [tid]
    conn.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE id=?", tuple(values))
    conn.commit()

    parts = []
    if new_name:
        parts.append(f"name → '{new_name}'")
    if task:
        parts.append(f"task updated ({len(task)} chars)")
    if schedule:
        parts.append(f"schedule → '{schedule}'")

    return f"Routine #{tid} '{cur_name}' updated: {', '.join(parts)}."


def _routine_pause(args: dict) -> str:
    import scheduler
    id_or_name = str(args.get("id_or_name", "")).strip()
    if not id_or_name:
        return "Error: id_or_name is required."

    tid, err = _resolve_id_or_name(id_or_name)
    if err:
        return f"Error: {err}"

    result = scheduler.set_enabled(tid, enabled=None)  # None = toggle
    if "error" in result:
        return f"Error: {result['error']}"

    state = "resumed (enabled)" if result["enabled"] else "paused (disabled)"
    return f"Routine #{tid} {state}."


def _routine_delete(args: dict) -> str:
    import scheduler
    id_or_name = str(args.get("id_or_name", "")).strip()
    if not id_or_name:
        return "Error: id_or_name is required."

    tid, err = _resolve_id_or_name(id_or_name)
    if err:
        return f"Error: {err}"

    return scheduler.remove(tid)


# ── Tool implementations — goals ──────────────────────────────────────────────

_STATUS_EMOJI = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
    "paused": "⏸",
    "aborted": "🚫",
}


def _goal_list(args: dict) -> str:
    import db
    from datetime import datetime, timezone
    status_filter = (args.get("status") or "").strip() or None
    limit = int(args.get("limit") or 20)
    goals = db.list_goals(status=status_filter, limit=limit)
    if not goals:
        msg = "No goals yet."
        if status_filter:
            msg = f"No goals with status '{status_filter}'."
        return msg

    lines = []
    for g in goals:
        emoji = _STATUS_EMOJI.get(g["status"], "?")
        snippet = (g["user_input"] or "")[:60].replace("\n", " ")
        if len(g["user_input"] or "") > 60:
            snippet += "…"
        cost = f" — ${g['cost_usd']:.4f}" if g["cost_usd"] else ""
        try:
            ts = datetime.fromtimestamp(g["created_at"], timezone.utc).strftime(
                "%m-%d %H:%M"
            )
        except Exception:
            ts = "?"
        lines.append(
            f"{emoji} {g['id']}  [{g['status']}]  {ts}{cost}\n   {snippet}"
        )
    return "\n".join(lines)


def _goal_create(args: dict) -> str:
    import db
    task = (args.get("task") or "").strip()
    if not task:
        return "Error: task is required."

    budget_usd = args.get("budget_usd")
    budget_seconds = args.get("budget_seconds")
    done_conditions = args.get("done_conditions") or None

    if budget_usd is not None:
        try:
            budget_usd = float(budget_usd)
        except (TypeError, ValueError):
            return "Error: budget_usd must be a number."
    if budget_seconds is not None:
        try:
            budget_seconds = int(budget_seconds)
        except (TypeError, ValueError):
            return "Error: budget_seconds must be an integer."

    try:
        goal_id = db.create_goal(
            user_input=task,
            source="skill",
            budget_usd=budget_usd,
            budget_seconds=budget_seconds,
            done_conditions=done_conditions,
        )
    except ValueError as e:
        return f"Error creating goal: {e}"
    except Exception as e:
        return f"Error: {e}"

    parts = [f"Goal created: {goal_id}"]
    parts.append("Status: pending — the worker will pick it up shortly.")
    if budget_usd is not None:
        parts.append(f"Budget: ${budget_usd:.2f} USD")
    if budget_seconds is not None:
        h, s = divmod(budget_seconds, 3600)
        m = s // 60
        label = f"{h}h {m}m" if h else f"{m}m"
        parts.append(f"Time limit: {label}")
    parts.append(f"Track progress: goal_view(goal_id='{goal_id}')")
    return "\n".join(parts)


def _goal_view(args: dict) -> str:
    import db
    from datetime import datetime, timezone
    goal_id = (args.get("goal_id") or "").strip()
    if not goal_id:
        return "Error: goal_id is required."

    g, err = _resolve_goal_id(goal_id)
    if err:
        return f"Error: {err}"

    emoji = _STATUS_EMOJI.get(g["status"], "?")
    lines = [
        f"{emoji} Goal {g['id']} — {g['status'].upper()}",
        f"Task: {g['user_input']}",
    ]

    # Timestamps
    try:
        created = datetime.fromtimestamp(g["created_at"], timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        lines.append(f"Created: {created}")
    except Exception:
        pass
    if g.get("started_at"):
        try:
            started = datetime.fromtimestamp(g["started_at"], timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            lines.append(f"Started: {started}")
        except Exception:
            pass
    if g.get("finished_at"):
        try:
            finished = datetime.fromtimestamp(g["finished_at"], timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            lines.append(f"Finished: {finished}")
        except Exception:
            pass

    # Cost
    if g["cost_usd"]:
        lines.append(f"Cost so far: ${g['cost_usd']:.4f}")

    # Subtask plan
    plan = db.get_goal_plan(g["id"])
    if plan and plan.get("subtasks"):
        lines.append("\nPlan:")
        for st in plan["subtasks"]:
            st_id = st.get("id", "?")
            st_status = st.get("status", "pending")
            st_title = st.get("title", st.get("description", ""))[:70]
            st_emoji = _STATUS_EMOJI.get(st_status, "·")
            lines.append(f"  {st_emoji} [{st_status}] {st_id}: {st_title}")

    # Result / error
    if g.get("result"):
        lines.append(f"\nResult:\n{g['result'][:500]}")
    if g.get("error"):
        lines.append(f"\nError: {g['error'][:300]}")

    # Outputs
    try:
        outputs = db.get_goal_outputs(g["id"])
        if outputs:
            lines.append("\nOutputs:")
            for o in outputs:
                lines.append(f"  [{o['kind']}] {o['title']}: {o['value'][:80]}")
    except Exception:
        pass

    return "\n".join(lines)


def _goal_pause(args: dict) -> str:
    import db
    goal_id = (args.get("goal_id") or "").strip()
    reason = (args.get("reason") or "paused by user").strip()
    if not goal_id:
        return "Error: goal_id is required."

    g, err = _resolve_goal_id(goal_id)
    if err:
        return f"Error: {err}"

    if g["status"] not in ("pending", "running"):
        return (
            f"Error: goal is '{g['status']}' — can only pause pending or running goals. "
            f"Use goal_resume to unpause, or goal_view to check status."
        )

    try:
        db.mark_goal_paused(goal_id, reason=reason)
    except Exception as e:
        return f"Error pausing goal: {e}"

    return (
        f"Goal {goal_id} paused. "
        "The worker will stop at its next checkpoint. "
        "Resume with goal_resume when ready."
    )


def _goal_resume(args: dict) -> str:
    import db
    goal_id = (args.get("goal_id") or "").strip()
    if not goal_id:
        return "Error: goal_id is required."

    g, err = _resolve_goal_id(goal_id)
    if err:
        return f"Error: {err}"

    if g["status"] != "paused":
        return (
            f"Error: goal is '{g['status']}' — only paused goals can be resumed. "
            f"Use goal_view to check current status."
        )

    conn = db._get_conn()
    conn.execute(
        "UPDATE goals SET status='pending', worker_id=NULL, lease_expires_at=NULL "
        "WHERE id=? AND status='paused'",
        (goal_id,),
    )
    conn.commit()
    db.log_goal_event(goal_id, "resumed", {"reason": "resumed by user via skill"})

    return (
        f"Goal {goal_id} is back in the queue (status: pending). "
        "The worker will pick it up from the last checkpoint shortly."
    )


def _goal_abort(args: dict) -> str:
    import db
    goal_id = (args.get("goal_id") or "").strip()
    reason = (args.get("reason") or "aborted by user").strip()
    if not goal_id:
        return "Error: goal_id is required."

    g, err = _resolve_goal_id(goal_id)
    if err:
        return f"Error: {err}"

    if g["status"] in ("done", "failed", "aborted"):
        return (
            f"Goal is already in terminal state '{g['status']}' — nothing to abort."
        )

    try:
        db.mark_goal_aborted(goal_id, reason=reason)
    except Exception as e:
        return f"Error aborting goal: {e}"

    return f"Goal {goal_id} aborted. This is permanent — the goal will not auto-resume."


# ── Dispatcher ────────────────────────────────────────────────────────────────


def execute(name: str, args: dict) -> str:
    if name == "routine_list":
        return _routine_list()
    elif name == "routine_create":
        return _routine_create(args)
    elif name == "routine_update":
        return _routine_update(args)
    elif name == "routine_pause":
        return _routine_pause(args)
    elif name == "routine_delete":
        return _routine_delete(args)
    elif name == "goal_list":
        return _goal_list(args)
    elif name == "goal_create":
        return _goal_create(args)
    elif name == "goal_view":
        return _goal_view(args)
    elif name == "goal_pause":
        return _goal_pause(args)
    elif name == "goal_resume":
        return _goal_resume(args)
    elif name == "goal_abort":
        return _goal_abort(args)
    return f"Unknown tool: {name}"
