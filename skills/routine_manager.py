"""Routine manager skill — create, edit, pause, and delete Routines from chat.

Extends the low-level schedule_task / list_cron / remove_cron core tools with:
  - Conversational UX via INSTRUCTION (guides the agent to draft quality tasks)
  - routine_update: patch name / task / schedule without delete + recreate
  - routine_pause: toggle enable/disable without deleting
  - id_or_name resolution on all mutating tools (numeric ID or partial name)
"""

DESCRIPTION = "Create, edit, pause and delete Routines (cron jobs) from chat"

INSTRUCTION = """When helping with routines, follow this workflow.

CREATING a routine — always do this before calling routine_create:
1. Call routine_list to show what already exists (brief output is fine).
2. Clarify WHAT the routine should do and WHEN.  Two questions max.
3. Draft the task instruction yourself.  Write it as a complete, self-contained
   directive for an autonomous sub-agent with shell, http_request, memory_save,
   and telegram_notify_owner available — but NO memory of this conversation.
   Every detail must be in the instruction text.

   Good example:
     «Fetch https://wttr.in/Moscow?format=j1 via http_request, extract
     today's max temperature (tmp_max in the first "feels_like" object) and
     weather description (weatherDesc[0].value), then call
     telegram_notify_owner with text "Погода: {desc}, макс {temp}°C"»

   Bad example:
     «Check the weather and send it to me»  ← too vague, will fail at runtime

4. Show the draft + proposed schedule to the user and ask:
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
"""

TOOLS = [
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
]

# ── Internal helpers ─────────────────────────────────────────────────────────

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


# ── Tool implementations ─────────────────────────────────────────────────────


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


# ── Dispatcher ───────────────────────────────────────────────────────────────


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
    return f"Unknown tool: {name}"
