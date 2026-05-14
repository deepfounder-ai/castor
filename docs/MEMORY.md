# Memory — what the agent remembers, and how to influence it

Memory is the difference between a stateless chat that re-introduces itself every turn and an agent that knows you, your context, and your history. Castor has memory by default — you don't configure it, you just notice it works.

This doc explains how it works **from the user's side**: what gets saved, when, what the agent recalls, and how to nudge it. For the architecture, see [how-memory-works.md](how-memory-works.md).

## What gets saved

**By the agent, automatically:** durable facts the agent thinks matter weeks later.

```
You:    My name is Kirill, I work on Castor.
Agent:  [memory_save "User's name is Kirill; works on Castor project"]
        Got it.
```

**By the agent, NOT automatically:** chat noise, jokes, one-off questions, tool outputs. Soul rule 8 (MEMORY DISCIPLINE) is explicit — default is **don't save**. The agent only saves when:

- A durable fact about you came up ("my dog is named Rex")
- A project decision was made ("we decided to use FastAPI for v2")
- A preference or constraint was stated ("don't write emoji in code")
- A piece of context you'll want back next session

If you want something saved that the agent didn't catch:

```
You:    Remember: delivery service contact — +7 ...
Agent:  [memory_save "Delivery service contact: +7 ..."]
        Got it.
```

If you want something forgotten:

```
You:    Forget the delivery service number.
Agent:  [memory_search "delivery service"] → finds it
        [memory_delete <id>]
        Deleted.
```

## What gets recalled

Every time you send a message, castor does an **auto-recall** before the LLM sees the message:

1. Embed your message
2. Hybrid search across the memory store (dense + sparse + BM25, fused via RRF)
3. Inject the top-K most relevant memories into the system prompt as **auto-context**

You see this in real time in the Web UI **Inspector**: the **Recalled memories** panel populates as the search runs, before the model starts streaming. Each memory shows its source (`thread` / `wiki` / `entity` / etc.) and the actual chunk text.

So when you say "remind me what we decided about FastAPI", the agent has already loaded the relevant memory chunks before generating a single token — it's not searching mid-turn, it's reading from context the system pre-loaded.

## Three layers of memory

Same store, different content:

| Layer | What's in it | When written |
|---|---|---|
| **Raw** | `memory_save` calls + auto-compaction summaries | Immediately, during the conversation |
| **Entity** | Graph nodes (people, products, concepts) with typed relations | Night synthesis job (03:00 default) |
| **Wiki** | Synthesized markdown summaries of related raw chunks | Night synthesis job |

The night job (`synthesis.py`) takes raw chunks tagged `synthesis_status=pending` and asks the LLM to extract entities + write wiki summaries. After synthesis:

- `rag_search` returns wiki chunks first — they're better-quality embeddings than the raw chunks they came from
- Entity relations let the agent answer "who works on X?" by walking the graph
- The Web UI **Knowledge Graph** view (force-directed) becomes useful for browsing

Synthesis is **off-thread** — it doesn't block your conversation. If your LLM is a cloud provider, the chunk content is sent to that provider during synthesis. Settings → Memory → `synthesis_enabled=0` to disable; raw search still works, just no wiki / entities.

## Thread isolation

Each conversation is its own thread. **Raw memories saved in one thread are NOT pulled into another thread's auto-recall.** This prevents your work-context bleeding into your personal-chat context.

What DOES cross threads:

- **Wiki + entity** chunks — synthesized memories are considered "cleaned up" and cross-thread
- **`memory_save` with the global tag** — pass `tag="global"` to make a memory cross-thread (the agent uses this for "user's name", "user's preferences", etc.)
- **Knowledge base ingest** — files dropped into the Knowledge tab are global, shared across threads (unless you're in a preset, which has its own knowledge namespace)

If you say "what's my dog's name?" in a new thread and the agent doesn't know, that's because the memory was saved as thread-scoped raw. Tell it again in the new thread, or ask the agent to save it globally:

```
You:    Remember globally: my dog's name is Rex.
Agent:  [memory_save "User's dog: Rex" tag="global"]
        Saved globally.
```

## Compaction — when the conversation gets long

When the conversation hits the **context budget** (default 24 000 tokens), castor runs **structured compaction**:

1. Old messages get summarised by the LLM into a 9-section structured summary (Current State / Goals / Key Files / Learnings / Next Steps / Open Questions / Errors / Decisions / Pending)
2. The summary is injected back into the conversation as a system message
3. The old raw messages are dropped from context but **saved to memory** (so `rag_search` can still find them)
4. The agent picks up from the summary

You see this in the Web UI as a "💭 Compacted N messages" marker in the thread. Compaction is silent — no LLM-visible "context filled up" event, the agent just continues. Saves cost on long threads.

## Tools the agent uses

Core (always loaded):

| Tool | What it does |
|---|---|
| `memory_save(text, tag?)` | Save a fact. `tag="global"` for cross-thread. |
| `memory_search(query, top_k?)` | Hybrid search. The agent uses this when it knows it needs an old fact. |
| `memory_delete(id_or_match)` | Forget a memory. By ID (from `memory_search`) or by content match. |

Auto-recall runs invisibly before every turn — the agent doesn't call `memory_search` for it.

## Secret scrubbing

`memory_save` runs **automatic secret scrubbing** before persisting. Patterns matched:

- API keys (OpenAI `sk-...`, Anthropic, Groq, GitHub PATs, AWS access keys, Slack tokens)
- JWT tokens
- `.env` lines (`KEY=VALUE` where the value looks like a credential)

A redacted memory still records what kind of secret it was — useful audit trail without the actual value:

```
You:    Remember my OpenAI key sk-proj-abc123...
Agent:  [memory_save "OpenAI key: <REDACTED:openai_key>"]
        I scrubbed the key value — secrets shouldn't go in memory. Use the
        Vault for credentials instead.
```

For real secret storage, use the [Vault](#vault) — encrypted, opt-in.

### Vault — encrypted secrets

`tool_search("secret")` activates the 4 vault tools:

| Tool | What it does |
|---|---|
| `secret_save(name, value)` | Encrypt + store. Master key in `~/.castor/.vault_key`. |
| `secret_get(name)` | Decrypt + return. Tool result clears at end of turn so it doesn't leak into history. |
| `secret_list()` | List secret names (NOT values). |
| `secret_delete(name)` | Remove. |

Use the vault for API keys, passwords, tokens — anything the agent needs to USE but you don't want stored in plain memory.

## Configuration

**Settings → Memory** (web) or `EDITABLE_SETTINGS`:

| Setting | Default | What it does |
|---|---|---|
| `context_budget` | `24000` | Compact when prompt hits this many tokens |
| `recall_top_k` | `5` | How many memories auto-recall injects |
| `recall_min_score` | `0.5` | Skip memories below this similarity |
| `synthesis_enabled` | `1` | Toggle the nightly synthesis job |
| `synthesis_time` | `03:00` | When synthesis runs |
| `auto_save_enabled` | `1` | Let the agent auto-save (turn off for "don't ever save anything") |

`CASTOR_QDRANT_MODE` env var picks the vector store backend:

- `memory` — in-process, lost on restart. For testing.
- `disk` (default) — Qdrant on disk under `~/.castor/memory/`. What you want.
- `server` — remote Qdrant at `CASTOR_QDRANT_URL`. For multi-machine setups.

## Patterns

### "Start a new conversation but keep context"

Memory tab → Memories list → mark important ones as **Pinned**. Pinned memories get a boost in auto-recall and survive even aggressive cleanup. Use sparingly — too many pins means the auto-recall slot fills with stale info.

### "Show me everything you remember about X"

Memory tab → search box. Same hybrid search as `rag_search`, results sorted by relevance. Filter by tag to scope (e.g. `tag=global` for cross-thread, `tag=thread:abc` for a specific thread).

### "Wipe everything and start fresh"

Memory tab → Settings (gear icon) → **Reset memory**. Deletes the Qdrant collection + clears the FTS index. Threads' message history stays in SQLite (you can re-synthesise from it if needed).

### "I want to see the knowledge graph"

Web UI → Knowledge → **Graph** tab. Force-directed SVG with entities + relations. Hover edges to highlight, drag nodes to rearrange, search to filter. The graph populates after the first synthesis job runs.

## Telemetry

Memory operations bucket into the `tool_calls_count` / `tool_errors_count` numbers in the `turn_complete` event. **Never the content** — neither saved text nor recalled chunks ever leave your machine via telemetry. See [PRIVACY.md](PRIVACY.md) for the full contract.

## Troubleshooting

**Agent doesn't remember a fact I told it** — was it auto-saved? Check Memory tab → search. If not there, the agent didn't think it was durable enough. Be explicit ("remember X") or ask for global tag ("remember globally").

**Auto-recall pulls irrelevant memories** — your `recall_min_score` might be too low. Raise it (Settings → Memory) so weaker matches don't make it into the context.

**Knowledge graph is empty** — synthesis hasn't run yet. Either wait for 03:00 or run manually via `/cron run synthesis` (CLI). Check `logs/castor.log` for `[synthesis]` lines.

**Memory takes forever to search** — disk-mode Qdrant on a slow disk. Try `CASTOR_QDRANT_MODE=memory` for the session (loses memory at restart but very fast) or move `~/.castor/memory/` to an SSD.

**Compaction summary loses important context** — the structured summary's 9 sections are designed to cover what matters, but for long highly-technical threads the summary can drop nuance. Workaround: use [presets](PRESET_GUIDE.md) — each preset gets its own thread + memory namespace, so context never has to span weeks of unrelated work.

## Cross-links

- [KNOWLEDGE.md](KNOWLEDGE.md) — the knowledge base (ingested files) vs memory (conversational facts)
- [how-memory-works.md](how-memory-works.md) — design-doc deep dive
- [PRIVACY.md](PRIVACY.md) — what data lives where, telemetry contract
- [PRESET_GUIDE.md](PRESET_GUIDE.md) — per-role memory + knowledge namespaces
