"""castor-worker — durable goal runner.

Run as a separate process (launchd / systemd / `python -m worker`). Polls the
``goals`` table, claims runnable goals via :func:`db.claim_next_goal`, executes
them via :func:`goal_runner.run`, and heartbeats the lease throughout.

If this process dies, the goal's ``lease_expires_at`` will lapse and another
worker (or this same one after restart) will take over from the last
checkpoint. See ``docs/superpowers/plans/2026-05-15-long-running-agent-architecture.md``
for the full design.

Usage::

    python -m worker                  # foreground
    python -m worker --once           # claim one goal, run it, exit (for tests)

The worker is intentionally simple: it doesn't import server.py / FastAPI /
WebSocket code, so it can run on minimal containers and doesn't tie its
lifetime to the web UI.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
import uuid

import config
import db
import goal_runner
import logger

_log = logger.get("worker")


# Identity: hostname + pid + 6 random hex. Used for lease ownership.
WORKER_ID = f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:6]}"

# Heartbeat tunables. lease_sec must be > heartbeat_interval × 2 so a single
# missed heartbeat doesn't free the lease prematurely.
LEASE_DURATION_SEC = 60
HEARTBEAT_INTERVAL_SEC = 20


# Module-level shutdown signal so signal handlers and the main loop share it.
_shutdown_event: asyncio.Event | None = None


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """SIGTERM / SIGINT trigger graceful shutdown — never kill-9 mid-checkpoint."""
    assert _shutdown_event is not None
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler on the proactor loop.
            signal.signal(sig, lambda *_: _shutdown_event.set())  # type: ignore[arg-type]


async def _heartbeat_loop(goal_id: str, stop: asyncio.Event) -> None:
    """Refresh the lease until *stop* is set or the goal is taken over."""
    while not stop.is_set():
        try:
            held = await asyncio.to_thread(
                db.heartbeat_goal, goal_id, WORKER_ID, LEASE_DURATION_SEC,
            )
            if not held:
                _log.warning(
                    f"goal {goal_id} taken over by another worker; "
                    f"this worker will stop processing it"
                )
                stop.set()
                return
        except Exception:
            _log.exception(f"heartbeat error for {goal_id}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)
            return  # stop was set during wait
        except asyncio.TimeoutError:
            continue  # interval elapsed, heartbeat again


async def _run_goal_with_heartbeat(goal_id: str, shutdown: asyncio.Event) -> None:
    """Run one goal, keeping its lease alive with a parallel heartbeat task."""
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_loop(goal_id, stop_heartbeat))
    try:
        await goal_runner.run(goal_id, shutdown_event=shutdown)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(f"unhandled error running goal {goal_id}")
    finally:
        stop_heartbeat.set()
        try:
            await asyncio.wait_for(heartbeat, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            heartbeat.cancel()


async def _poll_loop(once: bool = False) -> None:
    """Main worker loop. Claims goals up to `worker_concurrency` at a time."""
    assert _shutdown_event is not None

    # Startup cleanup: release any leases this WORKER_ID held in a previous
    # life. Practically a no-op on first boot but harmless to run.
    released = await asyncio.to_thread(db.release_worker_leases, WORKER_ID)
    if released:
        _log.info(f"released {released} stale lease(s) from previous run")

    concurrency = int(config.get("worker_concurrency") or 1)
    poll_interval = int(config.get("worker_poll_interval_sec") or 5)
    _log.info(
        f"worker {WORKER_ID} started "
        f"(concurrency={concurrency}, poll={poll_interval}s, lease={LEASE_DURATION_SEC}s)"
    )

    active: set[asyncio.Task] = set()

    while not _shutdown_event.is_set():
        # Reap finished tasks
        for t in {t for t in active if t.done()}:
            active.discard(t)
            exc = t.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                _log.error(f"goal task ended with exception: {exc!r}")

        # Try to claim more goals up to the concurrency limit.
        while len(active) < concurrency and not _shutdown_event.is_set():
            goal_id = await asyncio.to_thread(
                db.claim_next_goal, WORKER_ID, LEASE_DURATION_SEC,
            )
            if not goal_id:
                break  # no goals available right now
            _log.info(f"claimed goal {goal_id}")
            task = asyncio.create_task(
                _run_goal_with_heartbeat(goal_id, _shutdown_event)
            )
            active.add(task)
            if once:
                # Test mode: stop accepting new goals after the first claim.
                break

        if once and not active:
            # Nothing to do in --once mode and no in-flight tasks.
            return
        if once and active:
            # Wait for the in-flight task(s) to finish, then return.
            await asyncio.gather(*active, return_exceptions=True)
            return

        # Sleep until the next poll OR shutdown.
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass  # poll interval elapsed

    # Shutdown path: wait for active goals to checkpoint and pause cleanly.
    if active:
        _log.info(f"shutdown requested; waiting for {len(active)} active goal(s)")
        await asyncio.gather(*active, return_exceptions=True)
    _log.info("worker stopped cleanly")


async def _main(once: bool = False) -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop())
    await _poll_loop(once=once)


# ── Inline mode (web server embeds the worker) ───────────────────────────────
#
# When `worker_inline` is True (default), server.py spawns the worker as an
# asyncio task inside its own lifespan instead of forcing the user to start a
# separate `python -m worker` daemon. Great UX for dev / desktop installs;
# operators running a dedicated launchd / systemd worker should set
# worker_inline=False to avoid double-claim races (which are safe but waste
# poll cycles).


async def start_inline(shutdown_event: asyncio.Event) -> None:
    """Run the worker poll loop sharing the caller's event loop.

    Different from _main(): doesn't install signal handlers (the host
    process owns them) and uses the caller-provided shutdown_event so
    server.py can wave the worker down during its own lifespan teardown.
    The _poll_loop itself handles the startup lease cleanup.
    """
    global _shutdown_event
    _shutdown_event = shutdown_event
    await _poll_loop(once=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="castor durable goal worker")
    parser.add_argument(
        "--once", action="store_true",
        help="claim one goal, run it, exit (for tests / dry runs)",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(_main(once=args.once))
        return 0
    except KeyboardInterrupt:
        return 130  # 128 + SIGINT


if __name__ == "__main__":
    sys.exit(main())
