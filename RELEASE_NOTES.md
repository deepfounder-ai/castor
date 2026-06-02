## v0.23.3 — Coach, recovery helpers, polish

Patch release on v0.23.2: opt-in daily anti-pattern coach, a recovery path for Qdrant ↔ markdown desync, a sharper `--doctor` warning for `onnxruntime-gpu` (community PR), and a brand refresh on the web UI.

No schema migrations, no breaking changes. Drop-in upgrade.

### Coach — daily anti-pattern scan (opt-in, no LLM cost)

Inspired by Microsoft's [AI Engineer Coach](https://github.com/microsoft/AI-Engineering-Coach) VS Code extension. A small scheduled job (`__coach_daily__`, fires at 09:00) walks the last N days of `agent_runs` + `goals` + `scheduled_tasks` and writes a markdown summary to memory + an archive copy under `$DATA_DIR/uploads/coach-YYYY-MM-DD.md`. Pure SQL/Python, zero LLM cost.

Six built-in rules:

- `mega_session` — non-subagent run >30 min (loop/stuck candidate)
- `cost_outlier` — any single run ≥ $1.00
- `capitulating_goals` — goal status='done' with failed subtasks or no acceptance criteria
- `shell_heavy` — input/output token ratio >50:1 across 3+ runs (proxy for shell-poking)
- `synthesis_overspend` — system synthesis crons burning more than $0.10/day (regression guard for the v0.23.2 `_is_routine` fix)
- `no_skills_used` — 30+ chat sessions with zero skill / tool_search hits

Each finding ships with severity, headline, and an actionable recommendation. Dry-run against the developer's actual ~/.castor surfaced 5 real anti-patterns including the historical synthesis cost leak.

Opt-in via `setting:coach_enabled = 1`. Window configurable via `setting:coach_lookback_days` (default 7). 20 unit tests pin the rules + scheduler wire-up.

### Knowledge graph recovery: `memory.reindex_from_markdown`

User-facing symptom this fixes: the knowledge-graph view in the Web UI is empty and `memory.search` returns 0 results, despite hundreds of memory atoms visible via the markdown layer (`~/.castor/memories/atoms/`).

Phase-1 Living Memory writes Qdrant + markdown as siblings. If Qdrant gets wiped or rebuilt — corrupt-rebuild, manual `/api/knowledge/graph/clear`, or a migration that drops the collection — the markdown layer survives but the search indexes are gone. There was no reverse path to recreate them (`memory_store.backfill_from_qdrant` goes the wrong direction).

New `memory.upsert_with_id(point_id, text, tag, ...)` and `memory.reindex_from_markdown(skip_existing=True)`:

- Scrolls every markdown atom under `$DATA_DIR/memories/atoms/`
- Re-embeds dense + sparse vectors
- Upserts to Qdrant under the SAME point id (entity `relations[]` cross-references stay valid) + FTS5
- `skip_existing=True` (default) scrolls Qdrant up-front to collect already-present ids and skips them — a no-op on a healthy install
- Never raises; malformed atoms count as `errors` and the sweep continues

New `POST /api/knowledge/reindex` endpoint exposes it for one-click recovery from the UI / CLI.

Verified on the affected install: 159 scanned, 133 written, 26 skipped, 0 errors. The knowledge-graph endpoint immediately returned 19 nodes + 38 links again.

### Web UI: server-broadcast notifications no longer open a phantom bubble

Carry-over fix from the v0.23.2 release-day investigation, restated here because more notification types were caught. `handleWsMessage` short-circuits all 12 broadcast notification types (`cron`, `compaction`, `update_*`, `telegram`, `knowledge_*`, `task_update`, `canvas_*`, `get_frame`/`frame_request`, `interrupted_turn`) BEFORE the streaming-gate that creates an assistant bubble. The cron handler additionally filters `__`-prefixed system jobs so users aren't toasted by their own background curator every 15 minutes. A JS-contract test walks `server.py` for new `_broadcast({"type": ...})` sites — adding a notification type without a client-side handler now fails CI rather than ships as a phantom bubble.

### Doctor: `onnxruntime-gpu` warning is now actionable (closes #8)

Community contribution from @gberaberry-sys (PR #40).

The doctor check that warns about `onnxruntime-gpu` (3 GB of CUDA DLLs Castor doesn't use under CPU-only embeddings) now:

- Reports the **disk space** that would be freed (e.g. `~3.1 GB disk`).
- Softens the warning when `CUDA_PATH` / `CUDA_HOME` is set — the user installed CUDA Toolkit intentionally, so the message switches to an informational "embeddings use CPU by default; GPU package is unused unless `embed_device=cuda`."
- Skips the warning entirely when `setting:embed_device = cuda` is explicitly set — user knows what they're doing.

### skill_creator AST repair (closes #14)

Carry-over from v0.23.2 — restated for the changelog. New `_fix_stub_branch_outside_code` does AST-level repair for the LLM anti-pattern where small models emit `elif name == "x":  pass` and then write the real implementation outside the branch at function-body indent. The line-based `_fix_elif_body_indent` catches the common shape; the AST pass handles blank lines, comments, chained-elif tail-stubs, and tab/space inconsistencies. 15 new tests pin the contract.

### Brand refresh

`static/logo.png` updated. Apple touch icon and favicon regenerated from the same source. `logo-spicy.png` (the easter-egg variant toggled by `state.spicy`) intentionally left alone.

### Tests

1500+ passing (was 1453 in v0.23.2). 29 new tests across `test_coach.py` (20), `test_memory_reindex.py` (9), plus the cli.py doctor improvements from PR #40.

### Upgrading

`git pull` + restart. No config or schema changes.

If you were affected by the empty-knowledge-graph desync, run once:

```bash
curl -X POST http://localhost:7860/api/knowledge/reindex
```

To enable the coach (off by default):

```python
# Via the Settings UI, or:
import db; db.kv_set("setting:coach_enabled", "1")
```
