"""Goal runner — execute one durable goal to completion (or pause/fail).

This is Phase 1 of the long-running agent runtime. It deliberately stays thin:
it loads/saves checkpoints and runs the EXISTING ``agent.run()`` against the
goal's user_input. Phase 2 will replace the agent.run() call with a real
orchestrator that maintains a plan and dispatches subagents.

Lifecycle of one goal:

    1. Worker calls run(goal_id, shutdown_event)
    2. Load latest checkpoint (None on first claim)
    3. Build a TurnContext wired with:
         - abort_event linked to the worker's shutdown_event
         - on_round_complete callback that saves a checkpoint every
           CHECKPOINT_EVERY_N_ROUNDS rounds
         - goal_id so tools can scope state (Phase 3+)
    4. Call agent.run(user_input, ctx=...) inside a thread pool
       (agent.run is synchronous; we don't want to block the worker loop)
    5. On success → mark_goal_done; on shutdown → mark_goal_paused;
       on exception → mark_goal_failed
"""
from __future__ import annotations

import asyncio
import threading

import agent
import config
import db
import logger
from turn_context import TurnContext

_log = logger.get("goal_runner")


def _checkpoint_interval() -> int:
    """Rounds between checkpoints. Configurable via EDITABLE_SETTINGS."""
    try:
        v = config.get("checkpoint_round_interval")
        return max(1, int(v)) if v else 3
    except (TypeError, ValueError):
        return 3


async def run(goal_id: str, shutdown_event: asyncio.Event) -> None:
    """Run one goal until terminal status.

    Never raises — all errors are caught and recorded on the goal row so the
    worker poll loop can keep going.
    """
    goal = db.get_goal(goal_id)
    if not goal:
        _log.warning(f"goal {goal_id} not found, skipping")
        return

    if goal["status"] in db.GOAL_TERMINAL_STATUSES:
        _log.info(f"goal {goal_id} already in terminal status {goal['status']}, skipping")
        return

    checkpoint = db.load_latest_checkpoint(goal_id)
    start_round = (checkpoint["round_num"] + 1) if checkpoint else 0
    if checkpoint:
        _log.info(f"resuming {goal_id} from round {checkpoint['round_num']}")
    else:
        _log.info(f"starting {goal_id} fresh")
        db.log_goal_event(goal_id, "goal_started",
                          {"input_preview": goal["user_input"][:200]})

    # Bridge asyncio shutdown_event → threading.Event so the sync agent loop
    # (which runs in an executor) can poll it and exit cleanly.
    abort_event = _bridge_shutdown_to_threading(shutdown_event)

    ctx = TurnContext(
        source=goal["source"],
        abort_event=abort_event,
        goal_id=goal_id,
        on_round_complete=_make_checkpoint_callback(goal_id, start_round),
    )

    try:
        # agent.run is synchronous — run it in the default executor so this
        # coroutine doesn't block the worker's poll loop / heartbeat task.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: agent.run(
                user_input=goal["user_input"],
                thread_id=goal["thread_id"],
                source=goal["source"],
                ctx=ctx,
                save_user_msg=False,  # goal already persists user_input on goals row
            ),
        )
    except asyncio.CancelledError:
        # Cooperative cancellation — checkpoint preserved by on_round_complete.
        _log.info(f"goal {goal_id} cancelled during run; marking paused")
        db.mark_goal_paused(goal_id, reason="worker_cancelled")
        raise
    except Exception as e:
        _log.exception(f"goal {goal_id} crashed: {e}")
        db.mark_goal_failed(goal_id, error=f"{type(e).__name__}: {e}")
        return

    # Did the shutdown_event fire while the agent was running? If yes the
    # agent.run() may have returned early after abort — treat as paused.
    if shutdown_event.is_set():
        db.mark_goal_paused(goal_id, reason="worker_shutdown")
        return

    reply = getattr(result, "reply", "") or ""
    db.mark_goal_done(goal_id, result=reply)


def _make_checkpoint_callback(goal_id: str, start_round: int):
    """Build the on_round_complete callback that persists every N rounds.

    The callback runs inside the agent_loop thread (the executor where
    agent.run is running). SQLite is thread-safe with per-thread connections
    (db._local.conn), so the write happens on its own connection.
    """
    interval = _checkpoint_interval()

    def _cb(round_num: int, messages: list[dict]) -> None:
        global_round = start_round + round_num
        if global_round <= 0 or (global_round % interval) != 0:
            return
        try:
            db.save_checkpoint(
                goal_id,
                global_round,
                subtask_index=-1,  # no plan yet in Phase 1
                messages=messages,
                plan={},
                facts={},
            )
            db.log_goal_event(goal_id, "checkpoint_saved",
                              {"round": global_round, "messages": len(messages)})
        except Exception:
            _log.exception(f"checkpoint failed for {goal_id} round {global_round}")

    return _cb


def _bridge_shutdown_to_threading(shutdown_event: asyncio.Event) -> threading.Event:
    """Return a threading.Event that is set whenever ``shutdown_event`` is set.

    agent.run / agent_loop only know about threading.Event for aborts (it's
    polled from blocking tool calls like shell, http_request). The worker
    operates in asyncio land. This bridge fires once and stays set.
    """
    evt = threading.Event()

    async def _watcher() -> None:
        try:
            await shutdown_event.wait()
        except asyncio.CancelledError:
            return
        evt.set()

    # Fire-and-forget watcher; cancelled when the parent task ends.
    asyncio.create_task(_watcher())
    return evt
