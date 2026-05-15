# Long-Running Agent Architecture Implementation Plan

> **For agentic workers:** This is a multi-phase ARCHITECTURAL plan, not a single-feature implementation plan. Each phase below is large enough to need its own detailed `writing-plans` pass before execution. Read the whole document, then execute one phase at a time.

**Goal:** Turn Castor from an interactive copilot (5-30 round turns, minutes) into a backend-resident autonomous agent that can run hours-long, multi-stage tasks (LinkedIn scraping, research projects, long migrations) — surviving WebSocket disconnects, process restarts, partial failures, and context-window pressure.

**Architecture:** "Goal → Plan → Subagent dispatch" — inspired by Claude Code's `/goal` mode. A dedicated `castor-worker` process pulls goals from a durable queue, runs an orchestrator LLM that maintains a TodoWrite-style plan, and dispatches subagents with fresh contexts for heavy subtasks. State is checkpointed every N rounds so a kill-9 followed by a restart resumes mid-task.

**Tech Stack:** SQLite (durable queue + checkpoints), existing FastEmbed/Qdrant (memory), Playwright (browser per goal), Python 3.11 asyncio (concurrency within worker), launchd/systemd (worker lifecycle).

**Time estimate:** 6-8 weeks full-time, 6 phases, each independently shippable.

---

## Why the current system can't do this

Documented for context; readers familiar with the limitations can skip.

| Subsystem | Current limit | Why it breaks hours-long tasks |
|---|---|---|
| `spawn_task` | 15 rounds/worker × 3 workers = 45 max | Hard ceiling; in-memory queue; thread-shared module state |
| `agent_loop.run_loop()` | Single long turn, no checkpoints | Round 28 of 30 crashes → restart from round 1 |
| Context window | 24k tokens, compact at 80% | Compaction loses fidelity; after 4-5 compactions key facts are gone |
| Loop detection | 2 identical `tool+args` → `_force_finish` | Legitimate retry patterns (refresh page, poll DOM) get killed |
| WebSocket transport | Live connection required for turn | Close tab / lose internet → turn dies (Web UI). Resume requires user click |
| Browser state | Module-level globals shared across all callers | Two `spawn_task` workers compete for the same `_page` |
| Memory model | Flat: messages + Qdrant raw chunks | No structured "task state" / "facts found this run" |
| Cost gates | Per-turn round/cost caps via `MAX_TURNS=0` | Either unbounded (current) or arbitrary number — no semantic budget |

The current architecture optimizes the **median case** (3-5 round chat turn). For hours-long tasks the median becomes the tail, and every weakness compounds.

---

## Design decisions (locked-in)

| Decision | Choice | Rationale |
|---|---|---|
| Worker process model | One `castor-worker` daemon per host, concurrency=N goals (default 1, configurable). Started by launchd/systemd, restarts on crash. | Subagents are stack frames, not separate processes. Keeps debugging tractable; LinkedIn-style work is browser-bound and serial anyway. Multi-host distribution is a v2 concern. |
| Plan shape | Linear `subtasks: list[Subtask]` with `status: pending/in_progress/completed/skipped/failed`. Same data model as Claude Code's TodoWrite. | Sufficient for the target use case. DAG/recursive can be added later if a real workload needs it. |
| Subagent dispatch | Function call in the same process. Returns a single result string. Has restricted tool whitelist per subagent type. | Mirrors Claude Code's Task tool. Cheap, observable, easy to checkpoint at dispatch boundaries. |
| `spawn_task` fate | Keep as fire-and-forget short-task tool (<5 min, in-memory). New durable runtime is exposed as `goal_create` / `dispatch_subagent`. | Two tools with clear semantics. No breaking change for existing skills. |
| Checkpoint frequency | After every subtask boundary AND every 3 rounds inside a subtask. Tunable via `EDITABLE_SETTINGS["checkpoint_round_interval"]`. | Subtask boundaries are natural commit points. Mid-subtask cap of 3 rounds protects against long subtasks. |
| Browser state | Per-goal Playwright `BrowserContext` with persistent `user_data_dir`. Subagents within a goal share the context. Different goals get different contexts. | Lets subtask 1 log in to LinkedIn once and subtasks 2..N reuse the session. No cross-goal interference. |
| Plan editor | Read-only in UI for v1 (live list, can pause/resume goal). Editing the plan is v2. | Avoids "edit during run" race conditions in v1. User can pause, plan re-runs to generate a new version. |

---

## High-level architecture

```
                                    ┌──────────────────────────────┐
                                    │  user → web/CLI/telegram    │
                                    │  "scrape 200 LinkedIn       │
                                    │   leads for drayage in TX"  │
                                    └────────────┬─────────────────┘
                                                 │ goal_create(...)
                                                 ▼
                                    ┌──────────────────────────────┐
                                    │  goals table (SQLite)        │
                                    │  status=pending, plan=null   │
                                    └────────────┬─────────────────┘
                                                 │
                                                 │ poll
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  castor-worker (daemon, restarts on crash)                                │
│                                                                            │
│   ┌───────────────────────────────────────────────────────────────────┐   │
│   │  Goal Runner                                                       │   │
│   │    1. SELECT goal WHERE status=running OR pending FOR UPDATE       │   │
│   │    2. Load checkpoint (last messages + plan + facts)               │   │
│   │    3. Run Orchestrator until done / paused / aborted               │   │
│   │    4. Update goal.status, save final result                        │   │
│   └────────────────────────────┬──────────────────────────────────────┘   │
│                                │                                            │
│                                ▼                                            │
│   ┌───────────────────────────────────────────────────────────────────┐   │
│   │  Orchestrator (the main LLM)                                       │   │
│   │    - Tools: goal_plan_set, subtask_update, dispatch_subagent,      │   │
│   │             memory_save, http_request, basic_tools                 │   │
│   │    - Maintains plan; picks next pending subtask                    │   │
│   │    - For each subtask:                                              │   │
│   │         decision: inline (do it itself) OR dispatch_subagent(...)  │   │
│   │    - Checkpoints state every 3 rounds                              │   │
│   └────────────────────────────┬──────────────────────────────────────┘   │
│                                │ dispatch_subagent(type, prompt, ...)       │
│                                ▼                                            │
│   ┌───────────────────────────────────────────────────────────────────┐   │
│   │  Subagent (fresh LLM context, restricted tools)                    │   │
│   │    - Types: research / browser / code / scraper                    │   │
│   │    - Reads only what orchestrator passes in via `prompt`           │   │
│   │    - Returns ONE result string                                      │   │
│   │    - Result + first 200 chars of reasoning trace persisted         │   │
│   └────────────────────────────────────────────────────────────────────┘   │
│                                                                            │
│   Browser pool: { goal_id → Playwright BrowserContext }                    │
│   Memory: existing Qdrant + new `goal_facts` (structured key/value)        │
└────────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
                          web UI / CLI sees:
                          goal.status, goal.plan, goal.events, goal.result
```

Key invariant: **the orchestrator's context never contains a subagent's raw reasoning**. Only the result string + a 1-sentence summary. This is what keeps the orchestrator's context window small even over hours of work — same trick Claude Code uses with the Task tool.

---

## Phase 1: Durable Goal Runtime (foundation)

**Goal:** Boot a `castor-worker` process that can run the EXISTING `agent.run()` against a goal pulled from SQLite, checkpoint state, and resume after kill-9.

No new agent logic in this phase. Just the runtime substrate. If this works, every subsequent phase is "add intelligence on top of a reliable execution platform."

**Estimated:** 1-2 weeks. Highest risk phase — get the lifecycle right before anything else.

### 1.1 Schema

New migration `011_goals_subtasks_checkpoints.sql`:

```sql
-- goals: top-level user request, one row per "/goal X" command
CREATE TABLE goals (
    id              TEXT PRIMARY KEY,           -- 'g_<random>'
    thread_id       TEXT,                       -- the chat thread that created it
    source          TEXT NOT NULL,              -- 'web' | 'cli' | 'telegram' | 'scheduler' | 'api'
    user_input      TEXT NOT NULL,              -- original user request
    status          TEXT NOT NULL,              -- 'pending' | 'running' | 'paused' | 'done' | 'failed' | 'aborted'
    plan            TEXT,                       -- JSON: {subtasks: [...], current_index: N}
    result          TEXT,                       -- final reply to user when done
    error           TEXT,                       -- error message if status=failed
    budget_usd      REAL,                       -- hard cap, NULL = unbounded
    budget_seconds  INTEGER,                    -- hard cap wall-clock, NULL = unbounded
    cost_usd        REAL DEFAULT 0,             -- running total
    started_at      REAL,                       -- unix ts, set when worker picks it up
    finished_at     REAL,
    created_at      REAL NOT NULL,
    -- worker lease (so we can detect dead workers and re-queue)
    worker_id       TEXT,                       -- 'host_<hostname>_<pid>'
    lease_expires_at REAL,                      -- unix ts; worker must heartbeat before this
    -- meta
    meta            TEXT                        -- JSON: telegram_chat_id, user_id, etc.
);

CREATE INDEX idx_goals_status_lease ON goals (status, lease_expires_at);
CREATE INDEX idx_goals_thread ON goals (thread_id);

-- checkpoints: orchestrator state snapshots, enables mid-goal resume
CREATE TABLE goal_checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    round_num       INTEGER NOT NULL,           -- orchestrator round at checkpoint
    subtask_index   INTEGER,                    -- which subtask was in flight (-1 = planning)
    messages_blob   BLOB NOT NULL,              -- gzipped JSON of orchestrator messages[]
    plan_snapshot   TEXT NOT NULL,              -- JSON: same shape as goals.plan
    facts_snapshot  TEXT,                       -- JSON: snapshot of goal_facts at this point
    timestamp       REAL NOT NULL,
    -- only the latest N checkpoints per goal are kept
    UNIQUE (goal_id, round_num)
);

CREATE INDEX idx_checkpoints_goal_round ON goal_checkpoints (goal_id, round_num DESC);

-- goal_events: append-only event log for observability + debugging
CREATE TABLE goal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    timestamp   REAL NOT NULL,
    event_type  TEXT NOT NULL,                  -- 'goal_started' | 'plan_set' | 'subtask_started' |
                                                 -- 'subtask_completed' | 'subagent_dispatched' |
                                                 -- 'checkpoint_saved' | 'worker_lost' | 'resumed' |
                                                 -- 'budget_warning' | 'aborted' | 'error'
    payload     TEXT                            -- JSON, schema per event_type
);

CREATE INDEX idx_events_goal_time ON goal_events (goal_id, timestamp);
```

**Design notes:**

- `lease_expires_at` is the heartbeat protocol. Worker writes `now() + 60` on each round. If a worker dies, another worker (or a startup hook) sees `lease_expires_at < now()` and can take over the goal.
- `messages_blob` is gzipped JSON — orchestrator messages can be megabytes after compaction. SQLite handles BLOBs fine but raw JSON is wasteful.
- `goal_events` is append-only. Never updated. Easy to tail for live UI.

### 1.2 Worker lifecycle

New module `worker.py`:

```python
"""castor-worker — durable goal runner.

Boots as a separate process. Pulls one goal at a time (configurable concurrency).
Uses SQLite row-level lock + lease expiration for crash-safe goal pickup.
"""

import asyncio
import os
import signal
import socket
import time
import uuid

import config
import db
import logger

_log = logger.get("worker")

WORKER_ID = f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
LEASE_DURATION_SEC = 60        # worker must heartbeat within this
HEARTBEAT_INTERVAL_SEC = 20    # how often we refresh the lease
POLL_INTERVAL_SEC = 5          # how often we look for new goals

_shutdown_event = asyncio.Event()


async def main():
    """Worker entry point. Pulls goals, runs them, repeats."""
    _log.info(f"worker started: {WORKER_ID}")
    _install_signal_handlers()

    # Startup: release any zombie leases this worker_id held in a previous life.
    db.release_worker_leases(WORKER_ID)

    concurrency = int(config.get("worker_concurrency") or 1)
    sem = asyncio.Semaphore(concurrency)
    active_tasks: set[asyncio.Task] = set()

    while not _shutdown_event.is_set():
        # Reap done tasks
        active_tasks = {t for t in active_tasks if not t.done()}

        if len(active_tasks) < concurrency:
            goal_id = db.claim_next_goal(WORKER_ID, LEASE_DURATION_SEC)
            if goal_id:
                _log.info(f"claimed goal {goal_id}")
                async with sem:
                    task = asyncio.create_task(_run_goal(goal_id))
                    active_tasks.add(task)
                continue  # try to claim another immediately

        await asyncio.sleep(POLL_INTERVAL_SEC)

    # Shutdown: wait for active tasks to checkpoint and exit
    _log.info(f"shutdown signal received; waiting for {len(active_tasks)} active task(s)")
    await asyncio.gather(*active_tasks, return_exceptions=True)
    _log.info("worker stopped cleanly")


async def _run_goal(goal_id: str):
    """Run one goal until it reaches a terminal state. Heartbeats throughout."""
    heartbeat = asyncio.create_task(_heartbeat_loop(goal_id))
    try:
        await goal_runner.run(goal_id, shutdown_event=_shutdown_event)
    except Exception as e:
        _log.exception(f"goal {goal_id} crashed: {e}")
        db.mark_goal_failed(goal_id, error=str(e))
    finally:
        heartbeat.cancel()


async def _heartbeat_loop(goal_id: str):
    while True:
        try:
            db.heartbeat_goal(goal_id, WORKER_ID, LEASE_DURATION_SEC)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
        except Exception:
            _log.exception(f"heartbeat failed for {goal_id}")
            await asyncio.sleep(5)


def _install_signal_handlers():
    """SIGTERM / SIGINT trigger graceful shutdown (no kill-9 of subtask)."""
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown_event.set)


if __name__ == "__main__":
    asyncio.run(main())
```

### 1.3 db.py additions

```python
def claim_next_goal(worker_id: str, lease_sec: int) -> str | None:
    """Atomically claim the next runnable goal. Returns goal_id or None.

    A goal is claimable if:
      - status='pending' (never started), OR
      - status='running' AND lease_expires_at < now() (worker died)

    Uses a single UPDATE...RETURNING to avoid TOCTOU.
    """
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        """UPDATE goals
           SET status='running',
               worker_id = ?,
               lease_expires_at = ?,
               started_at = COALESCE(started_at, ?)
           WHERE id = (
               SELECT id FROM goals
               WHERE status = 'pending'
                  OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
               ORDER BY created_at
               LIMIT 1
           )
           RETURNING id, started_at IS NOT NULL AS was_running""",
        (worker_id, now + lease_sec, now, now),
    )
    row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    goal_id, was_running = row
    if was_running:
        # We took over a dead worker's goal. Log it.
        log_goal_event(goal_id, "worker_lost", {"new_worker_id": worker_id})
        log_goal_event(goal_id, "resumed", {"reason": "previous_worker_lease_expired"})
    return goal_id


def heartbeat_goal(goal_id: str, worker_id: str, lease_sec: int):
    """Refresh the lease. Must be called > 1×/lease_sec to keep the goal."""
    conn = _get_conn()
    conn.execute(
        "UPDATE goals SET lease_expires_at=? WHERE id=? AND worker_id=?",
        (time.time() + lease_sec, goal_id, worker_id),
    )
    conn.commit()


def save_checkpoint(goal_id: str, round_num: int, subtask_index: int,
                    messages: list[dict], plan: dict, facts: dict):
    """Persist orchestrator state. Keeps only the last 5 checkpoints per goal."""
    import gzip, json
    conn = _get_conn()
    blob = gzip.compress(json.dumps(messages).encode("utf-8"))
    conn.execute(
        """INSERT OR REPLACE INTO goal_checkpoints
           (goal_id, round_num, subtask_index, messages_blob, plan_snapshot, facts_snapshot, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (goal_id, round_num, subtask_index, blob,
         json.dumps(plan), json.dumps(facts), time.time()),
    )
    # Prune: keep only the latest 5 checkpoints per goal
    conn.execute(
        """DELETE FROM goal_checkpoints
           WHERE goal_id=? AND id NOT IN (
               SELECT id FROM goal_checkpoints
               WHERE goal_id=?
               ORDER BY round_num DESC LIMIT 5
           )""",
        (goal_id, goal_id),
    )
    conn.commit()


def load_latest_checkpoint(goal_id: str) -> dict | None:
    """Returns {round_num, subtask_index, messages, plan, facts} or None if no checkpoint."""
    import gzip, json
    conn = _get_conn()
    row = conn.execute(
        """SELECT round_num, subtask_index, messages_blob, plan_snapshot, facts_snapshot
           FROM goal_checkpoints WHERE goal_id=?
           ORDER BY round_num DESC LIMIT 1""",
        (goal_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "round_num": row[0],
        "subtask_index": row[1],
        "messages": json.loads(gzip.decompress(row[2]).decode("utf-8")),
        "plan": json.loads(row[3]),
        "facts": json.loads(row[4]) if row[4] else {},
    }


def log_goal_event(goal_id: str, event_type: str, payload: dict | None = None):
    """Append-only event log. Never fails on bad payload."""
    import json
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO goal_events (goal_id, timestamp, event_type, payload) VALUES (?, ?, ?, ?)",
            (goal_id, time.time(), event_type, json.dumps(payload or {})),
        )
        conn.commit()
    except Exception:
        _log.exception(f"failed to log event {event_type} for {goal_id}")
```

### 1.4 Minimal `goal_runner.py`

Phase 1 ports existing `agent.run()` — no orchestrator logic yet.

```python
"""Phase 1: bare-bones goal runner. Runs existing agent.run() with checkpointing.

Real orchestrator logic lands in Phase 2.
"""

import asyncio
import agent
import db
import logger
from turn_context import TurnContext

_log = logger.get("goal_runner")

CHECKPOINT_EVERY_N_ROUNDS = 3


async def run(goal_id: str, shutdown_event: asyncio.Event):
    """Run one goal to completion. Resumes from checkpoint if present."""
    goal = db.get_goal(goal_id)
    if not goal:
        _log.warning(f"goal {goal_id} not found, skipping")
        return

    checkpoint = db.load_latest_checkpoint(goal_id)
    if checkpoint:
        _log.info(f"resuming {goal_id} from round {checkpoint['round_num']}")
        db.log_goal_event(goal_id, "resumed",
                          {"from_round": checkpoint["round_num"]})
        messages = checkpoint["messages"]
        start_round = checkpoint["round_num"] + 1
    else:
        _log.info(f"starting {goal_id} fresh")
        db.log_goal_event(goal_id, "goal_started", {"input": goal["user_input"][:200]})
        messages = None  # let agent.run build the messages
        start_round = 0

    # Build a TurnContext that:
    #   1. Persists each round's messages to checkpoint table
    #   2. Listens to shutdown_event so SIGTERM stops the loop cleanly
    abort_event = _build_abort_event(shutdown_event)
    ctx = TurnContext(
        source=goal["source"],
        thread_id=goal["thread_id"],
        abort_event=abort_event,
        on_round_complete=_make_checkpoint_callback(goal_id, start_round),
    )

    # In phase 1, run the EXISTING agent.run() unmodified. It already produces
    # a final reply; we just capture it.
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, agent.run, goal["user_input"], ctx
        )
        db.mark_goal_done(goal_id, result=result)
        db.log_goal_event(goal_id, "goal_completed", {"reply_len": len(result)})
    except asyncio.CancelledError:
        # Graceful shutdown — checkpoint was saved on the last round, we just exit
        _log.info(f"goal {goal_id} cancelled (graceful shutdown)")
        db.mark_goal_paused(goal_id, reason="worker_shutdown")
        db.log_goal_event(goal_id, "paused", {"reason": "worker_shutdown"})
        raise
    except Exception as e:
        _log.exception(f"goal {goal_id} failed: {e}")
        db.mark_goal_failed(goal_id, error=str(e))
        db.log_goal_event(goal_id, "error", {"error": str(e)})


def _make_checkpoint_callback(goal_id: str, start_round: int):
    """Returns a callback that saves a checkpoint every N rounds."""
    def _cb(round_num: int, messages: list[dict]):
        global_round = start_round + round_num
        if global_round % CHECKPOINT_EVERY_N_ROUNDS != 0:
            return
        try:
            db.save_checkpoint(goal_id, global_round, subtask_index=-1,
                              messages=messages, plan={}, facts={})
            db.log_goal_event(goal_id, "checkpoint_saved", {"round": global_round})
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")
    return _cb


def _build_abort_event(shutdown_event: asyncio.Event):
    """Convert asyncio.Event to threading.Event for the synchronous agent loop."""
    import threading
    evt = threading.Event()
    async def _watch():
        await shutdown_event.wait()
        evt.set()
    asyncio.create_task(_watch())
    return evt
```

### 1.5 agent_loop changes for Phase 1

The existing `run_loop()` needs ONE addition: emit `on_round_complete` after each round so checkpoints can be saved.

```python
# agent_loop.py, after each round's messages.append calls in the main loop:
if ctx and ctx.on_round_complete:
    ctx.on_round_complete(_turn_num, list(messages))
```

That's it for Phase 1. Existing agent logic is unchanged.

### 1.6 server.py integration

Add `POST /api/goals` endpoint that creates a goal row + returns the goal_id. The web UI can poll `GET /api/goals/{id}` for status.

In Phase 1 the existing chat WebSocket continues to work unchanged — the new goal API is parallel.

### 1.7 Process lifecycle (macOS launchd)

Ship `scripts/com.castor.worker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>com.castor.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/.venv/bin/python</string>
        <string>-m</string>
        <string>worker</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>~/.castor/logs/worker.out.log</string>
    <key>StandardErrorPath</key><string>~/.castor/logs/worker.err.log</string>
</dict>
</plist>
```

Linux: equivalent systemd unit. Windows: scheduled task.

Boot doctor (`cli.py:doctor()`) gets a new check: "Is the worker process running?" and a "Launch worker" subcommand.

### 1.8 Tests for Phase 1

The HARD tests — these must all pass before Phase 2 starts:

```python
# tests/test_worker_lifecycle.py

def test_goal_survives_kill_9():
    """The acceptance test for Phase 1.

    1. Spawn a worker subprocess.
    2. POST /api/goals with a request that needs 6 rounds (mock LLM with deterministic responses).
    3. Wait for round 3 checkpoint.
    4. SIGKILL the worker.
    5. Spawn a new worker subprocess.
    6. Assert the new worker picks up the same goal, resumes from round 3+, finishes.
    """

def test_lease_expiry_triggers_takeover():
    """Worker A claims goal, then is killed without releasing.
    Worker B's next poll sees lease_expires_at < now() and takes over.
    """

def test_two_workers_dont_double_claim():
    """Race condition test: spawn 10 workers concurrently, create 1 goal,
    exactly one worker gets it, others see None from claim_next_goal."""

def test_graceful_shutdown_checkpoints():
    """SIGTERM causes worker to checkpoint current state and mark goal=paused,
    not failed. New worker can resume."""

def test_checkpoint_pruning():
    """After 10 rounds with checkpoint every 3, exactly 5 latest checkpoints remain."""
```

### 1.9 Phase 1 acceptance criteria

- [ ] `python -m worker` boots, polls, exits gracefully on SIGTERM
- [ ] launchd plist installs and worker restarts on crash
- [ ] `POST /api/goals` enqueues, worker picks up within 5s
- [ ] Goals survive `kill -9 worker` (resume from last checkpoint)
- [ ] Goals survive `restart whole castor stack`
- [ ] `tests/test_worker_lifecycle.py` — all 5 tests pass
- [ ] Existing 819 tests still pass

### 1.10 Risks for Phase 1

| Risk | Mitigation |
|---|---|
| SQLite contention between worker + server | WAL mode (already on); `BEGIN IMMEDIATE` in claim_next_goal to avoid SQLITE_BUSY |
| Worker checkpoints faster than disk can keep up | Gzip compress; cap checkpoint size at 1 MB (truncate oldest messages if needed) |
| Two `castor-worker` instances accidentally running | Worker registers via `~/.castor/worker.pid` file with `fcntl.lockf`. Doctor warns. |
| User restarts castor → in-flight chat turn becomes a "ghost" goal | Goals created from the chat UI (not `/goal` explicitly) skip the durable path in Phase 1. Only explicit goals go through worker. |

---

## Phase 2: Orchestrator + Subagent Dispatch

**Goal:** Replace the bare `agent.run()` in `goal_runner` with a real orchestrator LLM that maintains a plan, picks subtasks, and dispatches subagents. **This is where the agent learns to handle hours-long tasks.**

**Estimated:** 1-2 weeks.

### 2.1 Plan schema

The orchestrator's plan is JSON, stored in `goals.plan`. Shape:

```jsonc
{
  "version": 1,
  "subtasks": [
    {
      "id": "st_1",                              // stable across plan updates
      "title": "Search LinkedIn for drayage carriers in TX",
      "description": "Use browser to search...",  // for subagent prompt
      "status": "completed",                      // pending | in_progress | completed | skipped | failed
      "started_at": 1747424...,
      "finished_at": 1747424...,
      "result_summary": "Found 47 results across 3 pages",
      "result_full": "[truncated to 4 KB if very long]",
      "dispatched_subagent": "browser",          // or null if inline
      "attempts": 1
    },
    {
      "id": "st_2",
      "title": "Extract contact info for top 50 results",
      "description": "...",
      "status": "in_progress",
      "depends_on": ["st_1"]                     // optional, ignored for now (linear v1)
    }
  ],
  "current_index": 1,
  "created_at": ...,
  "updated_at": ...
}
```

### 2.2 Orchestrator system prompt (`prompts/orchestrator.md`)

The orchestrator gets a NEW system prompt distinct from the chat agent's soul. Key rules:

```markdown
You are an autonomous backend agent running a goal that may take hours.

WORKFLOW:
1. On first run: call `goal_plan_set` with a list of subtasks. Each subtask is one focused unit
   of work (search X, scrape Y, extract Z).
2. On each round: look at the plan, pick the first `pending` subtask, decide:
     a. INLINE: do it yourself this turn if it's a 1-2 step thing (write a file, save a memory)
     b. DISPATCH: call `dispatch_subagent(type, prompt, ...)` for anything that needs:
          - multiple browser actions
          - context the orchestrator shouldn't carry (search results, long pages)
          - >5 tool calls
3. After each subtask: call `subtask_update(id, status, result_summary)` to mark progress.
4. When all subtasks are `completed`: write a final summary message and STOP.

NEVER:
- Hold raw browser pages in your messages — dispatch a subagent instead.
- Update the plan from inside a subtask. Plan changes go via `goal_plan_set` only at orchestrator level.
- Re-do completed subtasks unless explicitly asked.

SUBAGENT TYPES:
- `research`: web search, summarize, return findings as text
- `browser`: navigate, click, scrape; can run for many rounds; returns extracted data
- `code`: read/write files, run shell, fix bugs
- `scraper`: extract structured data from a list of URLs (calls browser internally)

The subagent's full reasoning is discarded after it returns — only the result string is kept.
```

### 2.3 New tools

In `tools.py`:

```python
{
    "type": "function",
    "function": {
        "name": "goal_plan_set",
        "description": "Set or replace the goal's plan. Call this ONCE at the start of a new goal, "
                       "or to revise the plan when something major changes (e.g. a subtask failed in a way "
                       "that requires re-planning).",
        "parameters": {
            "type": "object",
            "properties": {
                "subtasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title", "description"],
                    },
                },
            },
            "required": ["subtasks"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "subtask_update",
        "description": "Update a subtask's status. Call after each subtask completes (inline or via subagent).",
        "parameters": {
            "type": "object",
            "properties": {
                "subtask_id": {"type": "string"},
                "status": {"type": "string", "enum": ["completed", "failed", "skipped"]},
                "result_summary": {"type": "string", "description": "One sentence."},
            },
            "required": ["subtask_id", "status"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": "Dispatch a focused subagent with a FRESH context window. The subagent does the work "
                       "and returns ONE result string. Use for anything multi-step (browser scraping, complex "
                       "research, long file edits). Returns the subagent's result.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["research", "browser", "code", "scraper"]},
                "prompt": {"type": "string", "description": "Self-contained task description. The subagent has NO context except this."},
                "subtask_id": {"type": "string", "description": "Which plan subtask this subagent is working on."},
                "max_rounds": {"type": "integer", "description": "Hard cap on subagent rounds (default 20)."},
                "shared_context": {
                    "type": "object",
                    "description": "Optional key facts to pass in (e.g. login URL, search keywords). NOT free-form text.",
                },
            },
            "required": ["type", "prompt", "subtask_id"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "fact_save",
        "description": "Save a structured fact to goal-scoped memory. Use for things subagents discovered "
                       "that future subtasks will need (URLs, IDs, credentials, intermediate counts).",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "snake_case, descriptive"},
                "value": {"type": "string"},
                "source_subtask_id": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "fact_get",
        "description": "Retrieve facts saved in this goal. Pass keys=null to list all keys.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
},
```

### 2.4 Subagent runtime (`subagent.py`)

```python
"""Subagent dispatch — fresh LLM context, restricted tools, returns one string.

Mirrors Claude Code's Task tool semantics: the subagent's reasoning trace
is discarded; only the result string flows back to the orchestrator.
"""

import logger
import providers
import agent_loop
from turn_context import TurnContext

_log = logger.get("subagent")

# Tool whitelist per subagent type. Each subagent gets ONLY the tools it needs;
# this both reduces hallucinated tool calls and prevents an orchestrator-level
# tool (like goal_plan_set) from being misused inside a subagent.
SUBAGENT_TOOLS = {
    "research": ["http_request", "memory_save", "memory_search", "browser_open", "browser_snapshot"],
    "browser":  ["browser_open", "browser_snapshot", "browser_click", "browser_fill",
                 "browser_eval", "browser_wait_for", "browser_accessibility",
                 "browser_screenshot", "browser_back", "browser_press_key"],
    "code":     ["read_file", "write_file", "shell", "memory_search"],
    "scraper":  ["browser_open", "browser_snapshot", "browser_eval", "memory_save"],
}

SUBAGENT_SYSTEM_PROMPTS = {
    "research": "...",   # see prompts/subagent_research.md
    "browser":  "...",
    "code":     "...",
    "scraper":  "...",
}


def run_subagent(
    *,
    goal_id: str,
    subtask_id: str,
    subagent_type: str,
    prompt: str,
    shared_context: dict | None = None,
    max_rounds: int = 20,
    browser_session_id: str | None = None,
    parent_ctx: TurnContext,
) -> str:
    """Run a subagent to completion. Returns the result string.

    The subagent uses:
      - Fresh `messages = [{role: system, ...}, {role: user, content: prompt}]`
      - Restricted tool whitelist (`SUBAGENT_TOOLS[type]`)
      - Same model as orchestrator (config.LLM_MODEL)
      - Browser session shared with this goal (so login persists)
      - Same abort_event as parent (orchestrator) — if parent aborts, subagent stops
    """
    if subagent_type not in SUBAGENT_TOOLS:
        return f"Error: unknown subagent type '{subagent_type}'"

    _log.info(f"[goal={goal_id} subtask={subtask_id}] dispatching {subagent_type} subagent")

    # Build the subagent's prompt
    system = SUBAGENT_SYSTEM_PROMPTS[subagent_type]
    user_message = _format_subagent_prompt(prompt, shared_context)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]

    # Restricted tools — pull from the full tools list and filter
    import tools as tools_module
    full_tools = tools_module.TOOLS  # OpenAI-format function schemas
    allowed = set(SUBAGENT_TOOLS[subagent_type])
    sub_tools = [t for t in full_tools if t["function"]["name"] in allowed]

    # Build sub-TurnContext that:
    #   - Logs events to goal_events with subagent_type prefix
    #   - Inherits parent's abort_event (cancel propagation)
    #   - Has the goal's browser_session_id so all browser tools use the right context
    sub_ctx = TurnContext(
        source=f"subagent_{subagent_type}",
        thread_id=parent_ctx.thread_id,
        abort_event=parent_ctx.abort_event,
        goal_id=goal_id,
        browser_session_id=browser_session_id or goal_id,
    )

    # Persist start event
    db.log_goal_event(goal_id, "subagent_dispatched",
                      {"subtask_id": subtask_id, "type": subagent_type, "max_rounds": max_rounds})

    # Run a constrained version of run_loop with this subagent's settings
    result = agent_loop.run_loop(
        messages=messages,
        tools=sub_tools,
        max_rounds=max_rounds,           # NEW: per-subagent hard cap
        model=config.get("llm_model"),
        ctx=sub_ctx,
        # ... other run_loop params
    )

    # The orchestrator never sees the subagent's raw messages — only this result.
    final_text = result.final_content or "[subagent produced no text result]"

    # Truncate hard: if subagent returned a 50 KB scrape, summarize before passing back
    if len(final_text) > 8000:
        final_text = _summarize_subagent_result(final_text)

    db.log_goal_event(goal_id, "subagent_completed",
                      {"subtask_id": subtask_id, "type": subagent_type,
                       "rounds": result.rounds, "result_len": len(final_text)})

    return final_text
```

### 2.5 Orchestrator changes in `goal_runner.run()`

```python
async def run(goal_id: str, shutdown_event: asyncio.Event):
    """Phase 2: real orchestrator loop."""
    goal = db.get_goal(goal_id)
    checkpoint = db.load_latest_checkpoint(goal_id)

    if checkpoint:
        messages = checkpoint["messages"]
        plan = checkpoint["plan"]
        facts = checkpoint["facts"]
    else:
        # Fresh start: only the system prompt + user input
        with open("prompts/orchestrator.md") as f:
            system = f.read()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal["user_input"]},
        ]
        plan = {"subtasks": [], "current_index": 0}
        facts = {}

    # Build the orchestrator's tool set
    import tools
    orch_tools = tools.get_orchestrator_tools()  # full set + goal_plan_set, subtask_update,
                                                   # dispatch_subagent, fact_save, fact_get

    # Run the orchestrator main loop
    while True:
        if shutdown_event.is_set():
            db.mark_goal_paused(goal_id, "shutdown")
            break

        # Build a TurnContext that exposes goal_id + facts to all tools
        ctx = TurnContext(
            source=goal["source"],
            thread_id=goal["thread_id"],
            abort_event=_build_abort_event(shutdown_event),
            goal_id=goal_id,
            on_round_complete=_make_checkpoint_callback(goal_id, plan, facts),
        )

        # ONE round of the orchestrator. Not a full agent.run().
        # We need fine-grained control so we can checkpoint between rounds AND
        # so we can detect when the orchestrator wants to dispatch a subagent.
        round_result = agent_loop.run_one_round(
            messages=messages, tools=orch_tools, ctx=ctx,
        )

        messages = round_result.messages_after

        # Did the orchestrator call dispatch_subagent? Run it inline.
        for tool_call in round_result.tool_calls:
            if tool_call.name == "dispatch_subagent":
                subagent_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    subagent.run_subagent,
                    goal_id=goal_id,
                    subtask_id=tool_call.args["subtask_id"],
                    subagent_type=tool_call.args["type"],
                    prompt=tool_call.args["prompt"],
                    shared_context=tool_call.args.get("shared_context", {}),
                    max_rounds=tool_call.args.get("max_rounds", 20),
                    parent_ctx=ctx,
                )
                # Push subagent result back into orchestrator's messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": subagent_result,
                })

        # All subtasks done?
        if all(s["status"] in ("completed", "skipped", "failed") for s in plan["subtasks"]):
            if plan["subtasks"]:  # not empty
                db.mark_goal_done(goal_id, result=round_result.final_content)
                break

        # Budget gate
        if _budget_exceeded(goal_id, goal):
            db.mark_goal_paused(goal_id, "budget_exceeded")
            break
```

### 2.6 Acceptance: Phase 2

The integration test that defines Phase 2 done:

```python
def test_three_subtask_goal_completes_end_to_end():
    """
    Goal: "Find 3 LinkedIn URLs for drayage companies in TX, save them as facts."

    Mock LLM with:
      - Orchestrator round 1: calls goal_plan_set([search, extract, save])
      - Orchestrator round 2: calls dispatch_subagent(browser, "search LinkedIn...")
      - Subagent: 4 rounds of browser actions, returns 3 URLs
      - Orchestrator round 3: calls fact_save x3, subtask_update(search, completed)
      - Orchestrator round 4: writes final summary, stops.

    Assert:
      - goals.status == 'done'
      - plan has 3 completed subtasks
      - goal_facts has 3 saved URLs
      - goal_events has subagent_dispatched + subagent_completed
      - orchestrator's final messages list does NOT contain the subagent's raw HTML
    """
```

---

## Phase 3: Long-Running Browser

**Goal:** Browser sessions survive across subagent dispatches and worker restarts. A goal that logs into LinkedIn in subtask 1 can scrape with that session in subtasks 2..N.

**Estimated:** 1 week.

### 3.1 Per-goal `user_data_dir`

Refactor `skills/browser.py` from module-level globals to a session manager:

```python
# skills/browser.py — Phase 3

import threading
from pathlib import Path

class BrowserSession:
    """One Playwright context, persistent across goal subagents."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.user_data_dir = Path(config.DATA_DIR) / "browser_sessions" / session_id
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = None
        self.context = None
        self.pages = []
        self.active_page = None
        self.network_log = []
        self.console_log = []
        self.lock = threading.Lock()
        self._headless = True

    def ensure_running(self):
        """Launch persistent context if not running. Thread-safe."""
        with self.lock:
            if self.context and self._is_alive():
                return
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self._headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=True,
            )
            # Pick up existing pages from persistent context (after restart)
            self.pages = self.context.pages or []
            self.active_page = self.pages[0] if self.pages else self.context.new_page()
            if self.active_page not in self.pages:
                self.pages.append(self.active_page)

    def close(self):
        with self.lock:
            try:
                if self.context: self.context.close()
            except Exception: pass
            try:
                if self.playwright: self.playwright.stop()
            except Exception: pass
            self.context = None
            self.playwright = None
            self.pages = []
            self.active_page = None


# Global session registry
_sessions: dict[str, BrowserSession] = {}
_registry_lock = threading.Lock()


def get_session(session_id: str) -> BrowserSession:
    with _registry_lock:
        if session_id not in _sessions:
            _sessions[session_id] = BrowserSession(session_id)
        return _sessions[session_id]


def execute(name: str, args: dict, ctx=None) -> str:
    """Browser tool entry point. Reads session_id from ctx."""
    session_id = (ctx.browser_session_id if ctx else None) or "default"
    session = get_session(session_id)
    return _execute_on_session(session, name, args)
```

### 3.2 Cookie/state recovery

`launch_persistent_context` automatically saves cookies, localStorage, IndexedDB to `user_data_dir`. After a worker restart:
1. `BrowserSession.ensure_running()` opens the same dir
2. Cookies + localStorage are restored
3. The previously-logged-in LinkedIn session resumes

### 3.3 Concurrency safety within a goal

Subagents within a goal share the session. If two subagents are dispatched in parallel (Phase 4 feature), they need to serialize on the session's lock. For Phase 3, orchestrator only dispatches subagents sequentially, so no lock contention.

### 3.4 Session GC

A scheduled job cleans up sessions for goals that are `done`/`failed`/`aborted` and older than `browser_session_retention_days` (default 7).

### 3.5 Tests

```python
def test_browser_session_persists_login_across_workers():
    """
    1. Start worker A.
    2. Create goal G with subtask: subagent navigates to a fake login page and submits form.
    3. After subtask 1 done, SIGKILL worker A.
    4. Start worker B.
    5. Worker B resumes G, runs subtask 2: subagent visits a 'profile' page
       that requires the login cookie set in subtask 1.
    6. Assert subtask 2 sees the logged-in state.
    """
    # Uses a local httpd serving Set-Cookie + a /profile that requires the cookie.
```

---

## Phase 4: Smarter Memory + Loop Detection

**Goal:** Make the orchestrator + subagents resilient to context-window pressure and dumb loop-detection false positives.

**Estimated:** 1 week.

### 4.1 `goal_facts` table

```sql
CREATE TABLE goal_facts (
    goal_id    TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    source_subtask_id TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (goal_id, key)
);
```

The orchestrator's `fact_save` / `fact_get` tools (defined in Phase 2) write/read here. Facts are ALWAYS available — they bypass context compaction entirely. The orchestrator's system prompt is amended each round with:

```
KNOWN FACTS (read-only, use fact_get for the latest):
- linkedin_session_cookie: [SET in subtask st_1]
- search_results_count: 47
- last_processed_url: https://...
```

(Only the keys are listed; values are pulled on demand via `fact_get` to keep the prompt small.)

### 4.2 Compaction strategy

Override the existing `agent._compact_context()` for orchestrator turns. Instead of summarizing the whole history:

1. Keep the system prompt (immutable).
2. Keep the plan (current state).
3. Keep the last 3 subtask result_summaries.
4. Keep the last 2 rounds of orchestrator messages verbatim.
5. Drop everything else.

Subagent results that were >8 KB are already summarized at dispatch time (Phase 2), so they don't pile up.

### 4.3 Result-aware loop detection

Replace `_force_finish on 2 identical tool sigs`:

```python
# agent_loop.py

# Track (tool_name, args_hash, result_hash) triples
_recent_tool_signatures = collections.deque(maxlen=4)

def _check_loop(tool_name, args, result) -> str | None:
    """Returns 'force_finish' | 'warn' | None.

    'force_finish': same call with same RESULT 3+ times (genuine loop).
    'warn': same call with same args 3+ times but DIFFERENT results (legit retry/polling).
    None: not a loop.
    """
    sig_args = (tool_name, _hash_args(args))
    sig_full = (tool_name, _hash_args(args), _hash_result(result))

    _recent_tool_signatures.append(sig_full)

    args_count = sum(1 for s in _recent_tool_signatures if s[:2] == sig_args)
    full_count = sum(1 for s in _recent_tool_signatures if s == sig_full)

    if full_count >= 3:
        return "force_finish"
    if args_count >= 3 and full_count < args_count:
        return "warn"  # tell the model "you're retrying — make sure that's intentional"
    return None
```

This kills the "agent legitimately re-reads a file 3 times after edits" false positive.

### 4.4 Subagent prompt with shared facts

When orchestrator dispatches a subagent, the subagent's user message includes a "Known facts:" preamble pulled from `goal_facts`. So a scraper subagent automatically knows the search keywords, login state, etc. without the orchestrator manually passing them in the prompt.

### 4.5 Tests

```python
def test_facts_survive_compaction():
    """Run an orchestrator for 50 rounds with aggressive compaction.
    Facts saved in round 5 are still queryable in round 50."""

def test_loop_detection_allows_retry_on_changing_result():
    """Browser refresh + browser_snapshot loop where snapshot changes each time
    is NOT treated as a loop."""

def test_loop_detection_blocks_stuck_loop():
    """Browser refresh + browser_snapshot loop where snapshot is identical 3x
    IS treated as a loop."""
```

---

## Phase 5: UI + Observability

**Goal:** User can see what the goal is doing, pause it, resume it, abort it.

**Estimated:** 1 week.

### 5.1 Goals view (`static/index.html`)

New left-nav item: **Goals**. Renders a list of goals with:
- Status chip (running / paused / done / failed)
- Cost-to-date, time-to-date
- Latest event ("subagent_browser completed: scraped 12 URLs")
- Click → detail view

### 5.2 Goal detail view

- Top: live status, budget bars, cost gauge
- Middle: **plan** with subtask checkboxes, each row shows status + result_summary
- Bottom tabs:
  - **Events**: timeline of `goal_events`
  - **Facts**: live `goal_facts` table
  - **Logs**: tail of orchestrator + subagent reasoning (truncated)
  - **Inspector**: current orchestrator messages (debug only)

### 5.3 Live updates via WebSocket

New WS events:
- `goal_event`: `{goal_id, event_type, payload, timestamp}` — pushed when worker logs an event
- `goal_status_change`: `{goal_id, status, plan?}`

Worker pushes these to a Redis-style pubsub-on-SQLite mechanism (or just polls SQLite from server.py, since we don't need sub-second latency).

### 5.4 Pause / Resume / Abort buttons

- Pause: set `goals.status='paused'`, worker on next heartbeat sees it and stops (after checkpointing).
- Resume: set back to `running`, worker poll picks it up.
- Abort: set `aborted`. Worker stops. Browser session retained for debugging.

### 5.5 Telegram integration

`/goals` command: list active goals. `/goal_pause N`, `/goal_resume N`, `/goal_abort N`. Notifications on goal completion / failure.

---

## Phase 6: Migration & cleanup

**Goal:** Wire all existing entry points into the goal API where it makes sense, deprecate now-obsolete code paths.

**Estimated:** 3-4 days.

### 6.1 `spawn_task` → fire-and-forget short tasks only

- Update the tool's description: "For short fire-and-forget tasks (<5 min). For long autonomous work, use goal_create."
- Document the trade-off in soul.py rule.
- Add `tasks._MAX_REASONABLE_DURATION_SEC = 300` warning: if a task runs longer than this, log a hint to use `goal_create`.

### 6.2 Scheduler routines → goals

Routines that are explicitly "long-running scrape" type get a flag `routine.use_goal_runtime=true`. When fired, instead of running in the scheduler thread, they create a goal and let the worker run it.

Short routines (heartbeat, daily-summary) stay on the scheduler.

### 6.3 Web chat → goals (optional)

Chat turns that hit `MAX_TURNS_FOR_CHAT` (new soft limit, default 10) get auto-promoted to goals. UI shows "This is getting long — running in background as Goal #42. I'll notify you when done."

### 6.4 CLI

`castor goal "scrape LinkedIn for ..."` — creates a goal, returns the goal_id, tails events.

### 6.5 Soul.py update

Add `Rule 17 — LONG TASKS`: when user requests something that's "scrape X", "monitor Y for 1 hour", "extract data from a list of N URLs", call `goal_create` instead of starting work in the chat turn.

### 6.6 Deprecation timeline

- v0.22.0: ship Phases 1-3, `goal_create` available, `spawn_task` unchanged.
- v0.23.0: ship Phases 4-6, scheduler routines opt-in to goal runtime.
- v0.24.0: deprecate `MAX_ROUNDS_PER_WORKER` chain — fold into goal runtime entirely.

---

## Cross-cutting: testing strategy

Three test tiers per phase:

1. **Unit tests** — pure functions (`db.claim_next_goal`, `_check_loop`, etc.). Fast.
2. **Integration tests** — full worker + mock LLM, in-process. Per phase, the acceptance test listed in that phase's section.
3. **Chaos tests** — real worker subprocess + kill-9 + restart. Run in CI on a slow lane (5 min budget). One per phase that exercises restart-safety.

Coverage floor stays at 24% (per `pyproject.toml`). New code should land at >70%.

---

## Cross-cutting: rollout & feature flag

Each phase ships behind a setting, default OFF:
- `worker_enabled` (Phase 1)
- `orchestrator_enabled` (Phase 2)
- `browser_per_goal_enabled` (Phase 3)
- `smart_loop_detection_enabled` (Phase 4)

This lets us ship-then-validate. Setting flips happen in subsequent releases once telemetry confirms stability.

---

## Open questions (resolve before starting phase 1)

1. **Worker authentication**: when worker runs as a separate process, does it share the same SQLite DB but different `_local.conn`? Yes — but we need to verify WAL handles 2 writers cleanly under load.
2. **Browser headless default for `castor-worker`**: should goals default to headless (yes — they're autonomous) or visible (only when explicitly requested)? Going with headless by default + per-goal `show_browser=true` flag.
3. **Budget enforcement granularity**: enforce at orchestrator level (round-level) or subagent level (subtask-level)? Both — orchestrator hard-stops on global budget, subagent has its own sub-budget passed in by orchestrator.
4. **What happens if the LLM provider is down for an hour?**: goal pauses with status=`paused`, reason=`provider_unreachable`. Worker keeps polling provider every 60s, resumes when LLM returns.
5. **Should goals have priorities?**: not in v1. FIFO. Add `priority INTEGER DEFAULT 0` column for future use.
6. **Cost recording per subagent**: each subagent run is a separate `agent_runs` row, linked to the goal via a new column `goal_id`. Aggregation rolls up.

---

## Summary

The minimum viable autonomous agent is:
1. Durable queue + worker (Phase 1) — survives crashes
2. Plan + subagent dispatch (Phase 2) — handles complex tasks
3. Persistent browser (Phase 3) — handles real-world scraping
4. + observability + retry semantics (Phases 4-6)

Phase 1 alone makes the existing agent ~10× more reliable for chat-length tasks. Phase 2 unlocks hours-long tasks. Phases 3-6 polish it into something users can trust unattended.

Each phase ships independently, behind a feature flag, with its own acceptance tests. **No big-bang releases.**
