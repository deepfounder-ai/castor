## v0.25.0 — MiniMax tool-calling, reliable Telegram streaming, Docker server package

Reliability + deploy release. The headline is end-to-end **MiniMax-M2 tool use** (it now actually runs browser/secret/extended tools instead of leaking XML to the chat), a **rewritten Telegram streaming path** that no longer goes silent or loses the reasoning block, a knowledge-graph **de-duplicator**, an **Inspector** pass, and a **production-ready Docker package** with a persistent-memory volume. No schema migrations. No breaking changes. Drop-in upgrade.

### MiniMax-M2 (and Anthropic-style) tool calls now execute

MiniMax-M2.7 emits tool calls as Anthropic-style XML (`<invoke name="…"><parameter name="…">`) in the content stream, not as native `delta.tool_calls`. Castor mishandled this end-to-end — the tags leaked into the chat as raw text and the tools never ran ("castor broke on a browser request"). Fixed across the loop:

- **Text-to-tool extraction** learned the `<invoke>`/`<minimax:tool_call>` dialect (new Pattern 1b), ordered ahead of the fuzzy prose heuristics so a `browser_open` call with a URL isn't mangled.
- **Tool-call XML is suppressed from the streamed reply** — the markup is executed, not shown. The final message is clean instead of `document.querySelector(…) </minimax:tool_call>` + a bare tools list.
- **Extended tools auto-activate.** MiniMax calls `browser_wait_for`, `schedule`, etc. straight from training without a prior `tool_search`. The main chat agent now recognises a text-emitted call to ANY known tool, executes it, and activates it for later turns. Subagents keep their restricted whitelist as the gate.
- **The bot is never silent.** A turn that ended on a tool call with no closing summary used to drop the whole Telegram message (`if response:`); it now sends when either the reply or the streamed buffer has content, with a "done" acknowledgement fallback.

### Telegram streaming: thinking that stays put

- **No more truncated replies.** Inline-thinking models split `</think>answer` across one delta; the answer text riding alongside the closing tag was dropped. The loop now splits on the tag boundary and emits both sides — losslessly, for web streaming too.
- **Reasoning no longer vanishes mid-task.** On a long multi-round turn the ephemeral rich draft could expire (or get rejected when oversized), latching the render to a placeholder path that dropped the thinking block. The placeholder now shows a `💭` reasoning block too, a keepalive thread refreshes the live view during long gaps (slow LLM rounds, multi-second browser tools), and both draft and placeholder cap the partial answer so a long turn can't produce an oversized draft.

### Knowledge graph: duplicate entity de-dup

Night synthesis spawned a fresh entity node every run instead of updating the existing one (a fuzzy `search(limit=1)` missed the exact match when a near-name out-ranked it), so the graph filled with up to 14× `Drayage` / `LinkedIn` nodes. Now:

- `synthesis._upsert_entity` looks the node up by **exact name**, merges every copy into one, and drops the extras — it stops spawning duplicates and self-heals touched entities.
- New **"merge duplicates"** button in the graph toolbar + `POST /api/knowledge/graph/dedupe` collapse same-named nodes (relations + observation counts preserved; identity is by name so links stay intact).
- The graph endpoint also merges by name at render time, so the view is clean immediately.

### Inspector

A pass over the right-side Inspector:

- **Context-window gauge** now refreshes on a settings save (was stuck showing the pre-save value), shows `1M` instead of `1000k`, and falls back to the real settings dump instead of a dead `state.settings` reference. `model_context` is now settable in **Settings → Inference** (the gauge tooltip already pointed there).
- **Recalled memories** — removed a dead KB-preview fallback that left the "RECALLED · this session" counter stuck and could imply recall the agent never made; the live WS path is authoritative. The `live` badge no longer shows on an empty turn.
- **Active tools** now includes the `tool_search`-activated extended tools for the thread (dashed chips), and the header count matches the deduped chips.
- **Latency** — the decode row is labelled `tok/s` (it's a rate).

### Docker: production server package with persistent memory

The shipped Docker setup is now actually deployable:

- **Dockerfile fixes** — the old `CMD` ran a non-existent `qwe-qwe` command (the console script is `castor`), and it never copied `prompts/` or `schemas/`, so goals and presets crashed. Added a `/data` `VOLUME`, sane env defaults, and a `/api/status` `HEALTHCHECK`.
- **`docker-compose.yml`** pulls the prebuilt GHCR image, **bind-mounts `./castor-data:/data`** so all state (SQLite, Qdrant vectors, wiki, skills, uploads, presets, logs) survives restarts and upgrades, reads config from `.env`, and sets `shm_size: 1gb` so Chromium doesn't crash.
- New **`.env.example`** (provider URL/model/key, `CASTOR_PASSWORD` web auth) and **`docs/DEPLOY.md`** with quick-start, build-from-source, update, backup, and terminal-access (with the Qdrant disk-lock caveat) instructions.

### Internal: legacy cleanup (~1150 lines removed)

- Removed the **legacy v1 agent loop** and the `agent_loop_v2` flag — v2 has been the only path in production. With it went the v1-only self-check cluster and the `self_check_enabled` setting.
- **Wired trajectory recording** into the live loop (opt-in via `trajectory_enabled`) — it existed but was never attached.
- Dropped a batch of dead symbols (`discover_first`, `completed_count`, `provider_kind_from_url`, server file-text helpers, unused `agent_budget` limit fields, a dead `scheduler._log_run` branch, the `SKILLS_DIR` alias, and stale agent-event constants/methods).

---

Full diff: `v0.24.0...v0.25.0`.
