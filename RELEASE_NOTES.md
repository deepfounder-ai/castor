## v0.23.2 — Phantom "generating" bubble fix

### Critical user-facing fix

**Phantom "generating" assistant bubble appeared out of nowhere on idle chats and blocked further sends.**

User report: idle chat, agent's last reply already delivered, everything looked done — and suddenly a "castor 09:39 PM generating" status appeared with the typing indicator on. The bubble never closed, so the composer stayed in a busy state and new messages couldn't be sent.

Root cause: ``static/index.html::handleWsMessage`` short-circuited only on a few notification WS types (``task_update``, ``canvas_*``, ``get_frame``, ``interrupted_turn``). The server emits 8 more notification types via ``_broadcast`` to every connected client regardless of which thread is in view — ``cron``, ``compaction``, ``update_progress``, ``update_done``, ``telegram``, ``knowledge_progress``, ``knowledge_gpu_warning``, ``knowledge_done``. Each slipped past the (incomplete) short-circuit list and hit the streaming-message creation gate, which opened a pending assistant bubble that NEVER received the ``done`` event that notifications don't emit.

The exact 09:39 PM scenario: the ``__synthesis_continuous__`` cron fires every 15 minutes, the cron callback broadcasts a ``cron`` WS message, every open web client opens a phantom bubble. Same class of bug as ``task_update`` (fixed in v0.18.3) but for the remaining notification types that were never wired up.

Fix: explicit short-circuit handler for every broadcast notification type with appropriate UI treatment (toast for transient events, silent for events with their own panel). System-internal cron jobs (``__synthesis_continuous__``, ``__heartbeat__``) are silently filtered so the user isn't toasted by their own background curator every 15 minutes.

### Auditing guard

The original bug pattern can recur whenever someone adds a new ``_broadcast({"type": "..."})`` call in ``server.py`` and forgets the corresponding client-side handler. New JS-contract test (``tests/test_ws_notification_short_circuit.py``) walks ``server.py`` for every ``_broadcast`` type literal and asserts the client has a short-circuit BEFORE the streaming gate. Adding a new notification type without wiring the client will now fail CI rather than ship as a phantom bubble.

### skill_creator: AST-level repair (closes #14)

Issue #14 documented a recurring LLM failure mode in the skill-creation pipeline: small models emit a tool-dispatch ``elif name == "...":`` with body ``pass`` and write the real implementation OUTSIDE the branch at function-body indent. The line-based regex fixer (``_fix_elif_body_indent``) caught the common shape but missed edge cases observed in the workspace_meter and camera_diagnostics field sessions — blank lines between Pass and the stray code, comments in between, chained-elif tail-stub patterns, tab/space inconsistencies.

New ``_fix_stub_branch_outside_code`` does AST-level repair: parses the LLM output, walks dispatch ``If`` nodes whose tail is body=[Pass], pulls following non-dispatch siblings into the branch's body, re-emits via ``ast.unparse``. Defensive: returns the input unchanged if ``ast.parse`` can't handle it (lets downstream syntax check report the real error). Wired in two pipeline call sites (the main custom-code assembly and the SyntaxError recovery path).

15 new tests pin the contract against the exact buggy shapes from the field sessions.

### Dependency updates

Dependabot PRs #35-39 applied in batch:

  openpyxl       3.1   -> 3.1.5   (patch)
  python-pptx    1.0   -> 1.0.2   (patch)
  qdrant-client  1.11.0 -> 1.18.0 (7 minor — verified memory + rag still work)
  readchar       4.0.0  -> 4.2.2  (minor)
  requests       2.31.0 -> 2.34.2 (patch)

### Tests

1451 passing, 24 skipped (was 1345 in v0.23.1). 19 new tests added across:

- ``test_ws_notification_short_circuit.py`` (4) — broadcast notification short-circuits + cron filter + auditing guard
- ``test_skill_creator_ast_fix.py`` (15) — AST-level repair for issue #14

Plus test isolation fix for CI Python 3.12 (``test_provider_error_classification`` was pulling ``goal_runner`` at module level, polluting ``test_skill_import``'s database state).

### Upgrading

``git pull`` + restart. No config changes, no migrations.
