# Skills — the agent's extensible capability layer

A **skill** is a self-contained Python module that bundles related tools + an instruction string that teaches the agent when to use them. Skills are how Castor stays small (the core ships with ~30 tools, the rest live in skills) and extensible (you can add a skill without restarting, without touching core code).

Three things to know:

1. Some skills are **auto-active** — their tools are searchable from day one.
2. The rest activate on demand via `tool_search(keyword)` — keeps the system prompt lean.
3. Anyone can write a new skill. You can ask the agent to write one (`skill_creator`), import one from skills.sh, or drop a `.py` into `~/.castor/skills/`.

## Built-in skills

| Skill | Status | What it does |
|---|---|---|
| `skill_creator` | auto-active | Chat-create new skills via a 5-step LLM pipeline |
| `mcp_manager` | auto-active | Add / remove / restart MCP servers — see [MCP.md](MCP.md) |
| `soul_editor` | auto-active | AI-assisted personality tuning — see [SOUL.md](SOUL.md) |
| `notes` | auto-active | Lightweight note storage (separate from memory + knowledge) |
| `timer` | auto-active | Countdown timers with notifications |
| `weather` | auto-active | Weather reports via `wttr.in` (no API key needed) |
| `browser` | on `tool_search("browser")` | 23 Playwright tools — see [BROWSER.md](BROWSER.md) |
| `serial_port` | on `tool_search("serial")` | Hardware I/O — see [HARDWARE.md](HARDWARE.md) |
| `canvas` | auto-active | Sandboxed HTML side panel — see [CANVAS.md](CANVAS.md) |
| `skill_import` | auto-active | Install community skills from skills.sh / GitHub — see [SKILLS_IMPORT.md](SKILLS_IMPORT.md) |
| `spicy_duck` | auto-active | Rubber-duck debugging companion that asks pointed questions |

"Auto-active" doesn't mean the tools are always loaded — they're still gated by `tool_search`. It means their keywords are pre-indexed, so `tool_search("note")` reliably surfaces `create_note` etc.

## How tools get activated

Castor loads **only ~29 core tools** into the system prompt by default. To use anything else, the agent calls `tool_search("keyword")` which:

1. Scans the keyword index across all installed skills
2. Activates the matching skill's tools for this turn (system prompt gains them via injection)
3. Returns a summary "Found 5 tools: ..."

The keywords are baked into each skill's `_TOOL_SEARCH_INDEX` entries (see `tools.py`). Examples:

| Keyword | Activates |
|---|---|
| `browser`, `web`, `scrape` | Browser quickstart tools (7) |
| `serial`, `modbus`, `scale`, `barcode` | Hardware tools (3) |
| `note`, `notes`, `memo` | Notes tools (5) |
| `timer`, `countdown` | Timer tools (3) |
| `canvas`, `dashboard`, `form`, `chart` | Canvas tools (5) |
| `mcp` | MCP manager tools (4) |
| `skill` | `create_skill`, `delete_skill`, `import_skill` |
| `soul` | Soul-editor tools |
| `schedule`, `cron`, `routine` | Scheduled-task tools |

Why this design: a 16k-token system prompt with every tool wastes 75% of your context budget. Keeping it under 1500 tokens leaves room for conversation, recall, tool output.

## Creating a skill from chat

The `skill_creator` skill turns a user request into a working `.py` skill in ~30-60 seconds:

```
You:    Create a habit tracking skill — daily check-in, history, stats.
Agent:  [create_skill name="habit_tracker"
          description="Track daily habits — log, history, stats"]
        → 5-step pipeline kicks off in background:
          1. Plan (docstring, instruction, tables, tool list)
          2. Tool definitions (OpenAI function schemas)
          3. Mapping + code assembly (CRUD ops from templates)
          4. Table DDL
          5. Validate + smoke test
        Skill 'habit_tracker' created with 4 tools (45s).
You:    Log that I went for a walk today.
Agent:  [tool_search("habit")] [habit_tracker_log habit="walk"]
        Logged. That's the third time this week.
```

The pipeline runs in a background thread; you get a "skill ready" notification when it lands. Skills get a SQLite schema (`skill_<name>_*` tables, prefixed to prevent collisions) and a `.py` file at `~/.castor/skills/<name>.py`.

### What skill_creator is good at

- **CRUD-shaped capabilities** — track X, list X, delete X, stats on X. The pipeline recognises these patterns and uses templates (no LLM call for the common case).
- **Service integrations** — wraps `http_request` + secrets + tool definitions for any HTTP API.
- **Domain DSLs** — small calculators, converters, format-specific generators.

### What it's NOT good at

- **Large stateful capabilities** — multi-file logic, complex algorithms, anything needing a UI. Use the Python ecosystem directly (write the code yourself, drop the `.py` in `~/.castor/skills/`).
- **Real-time / streaming workflows** — skills are turn-shaped; for "watch this stream and act on events", use routines + a polling skill.

## Importing a skill from skills.sh

Different angle on the same problem: instead of writing a new skill from a prompt, **install one someone already wrote**:

```
You:    Install the PDF skill from Anthropic.
Agent:  [tool_search("import")]
        [import_skill url="https://skills.sh/anthropics/skills/pdf"]
        → Imported 'pdf' (Anthropic source-available license — see staged
          LICENSE.txt). Tools: pdf_help.
```

skills.sh hosts skills following the [Anthropic SKILL.md spec](https://agentskills.io/specification). Castor imports them via a thin adapter — the SKILL.md body becomes the agent's instructions, the `scripts/` and `references/` get staged for `read_file` access.

Full doc: [SKILLS_IMPORT.md](SKILLS_IMPORT.md). Covers security (domain allowlist, SSRF guard, license surfacing), what skills work well, and the audit trail.

## Anatomy of a skill

If you want to write one by hand, here's the minimal structure (`~/.castor/skills/example.py`):

```python
"""Example skill — illustrates the contract."""

DESCRIPTION = "Single-line summary shown in the skills list."

# Optional: injected into the system prompt when ANY tool from this
# skill is active. Use it to tell the model when/how to use the tools.
INSTRUCTION = """
When the user asks about widgets, use widget_list / widget_create.
Default sort is by created_at DESC.
"""

# OpenAI-function-format tool schemas
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "widget_list",
            "description": "List widgets",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "widget_create",
            "description": "Create a widget",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"]
            }
        }
    },
]

# (optional) SQL DDL — tables MUST be prefixed `skill_<name>_*`
TABLES = [
    "CREATE TABLE IF NOT EXISTS skill_example_widgets ("
    " id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
]


def execute(name: str, args: dict) -> str:
    if name == "widget_list":
        # ... query skill_example_widgets, return markdown table
        return "..."
    if name == "widget_create":
        # ... insert
        return "Created."
    return f"Unknown tool: {name}"
```

That's the whole contract. Drop the file in `~/.castor/skills/`, restart castor (or hot-reload via `/reload` CLI), it's live.

Built-in skills (in `skills/`) follow the same shape — read `skills/weather.py` or `skills/notes.py` for short reference implementations.

### Conventions

- **Table prefixing** — every SQLite table a skill creates MUST be named `skill_<name>_*`. Enforced for `create_skill` and recommended for hand-written skills. Prevents collisions with core tables (`messages`, `kv`, `threads`) and other skills.
- **Tool naming** — tool names should start with the skill name (`weather_get`, `notes_create`, `widget_list`). Helps tool_search keyword matching.
- **Secret-aware** — use the [vault](MEMORY.md#vault) for API keys, not config files or memory. `secret_get("key_name")` in your skill.
- **Errors** — return a string starting with `Error:` so the agent learns to retry or report. Don't raise; the agent doesn't see raised exceptions cleanly.

## Listing / managing skills

```bash
# CLI
/skills              # list installed skills with status
/skills <name>       # show a skill's tools, instruction, status
/reload              # hot-reload skills from disk after editing
```

```bash
# Web UI
Settings → Tools & skills
```

The Tools & skills tab has:

- **Search box** — filters across tool name + description + category
- **Collapsible category headers** — Memory / Files / Web / Browser / Hardware / Skills / Meta
- **Import button** — paste a skills.sh URL, install in one step
- Per-skill enable / disable toggle (for the skill's tools to participate in `tool_search`)
- Click into a skill to see its tools, instruction text, and (if user-created or imported) the source `.py`

## Deleting a skill

```
[delete_skill name="habit_tracker"]
→ Drops the skill_habit_tracker_* SQLite tables, unlinks the .py.
```

For imported skills, use `delete_import` instead — it preserves the `.py` if you've edited it (sentinel-protected, see [SKILLS_IMPORT.md](SKILLS_IMPORT.md)).

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `skills_dir` | `~/.castor/skills` | Where user-installed skills live (env: `CASTOR_DATA_DIR`-derived) |
| `skill_creator_provider` | (inherit) | Use a separate provider for skill generation (e.g. local model to avoid cloud cost) |
| `skill_creator_retries` | `3` | How many times the pipeline retries on validation failure |

## Telemetry

`skill_creator` emits a dedicated telemetry event (`skill_creator_pipeline`) with the **outcome** (`success`, `validation_failed`, `parse_error`, etc.) and the **duration** — never the skill name or generated code. Helpful for tracking pipeline reliability without leaking what users build.

Regular tool calls from skills bucket into the `skills` telemetry category in `turn_complete`. No tool name, no input/output. See [PRIVACY.md](PRIVACY.md).

## Troubleshooting

**Skill won't load** — syntax error in the `.py`. `castor --doctor` lists skills with their validation status; an invalid skill is flagged with the line number. Open the file, fix, `/reload`.

**`tool_search` doesn't find my skill's tools** — keyword index didn't register. The skill needs an entry in `_TOOL_SEARCH_INDEX` for the keywords you expect (`tools.py` for built-ins, the skill's own module for user-installed).

**`skill_creator` keeps failing validation** — the model is too small or the request is too ambitious. Try simpler requests, or switch to a stronger LLM via `/model` before retrying.

**Skill calls fail with "Unknown tool"** — `execute(name, args)` doesn't handle the tool name. Common copy-paste mistake when adding a tool to `TOOLS` but forgetting the dispatch branch.

**Table name not prefixed** — `delete_skill` will refuse to drop tables that aren't `skill_<name>_*`. If you wrote a skill that creates a `widgets` table (no prefix), rename it to `skill_<name>_widgets`. Otherwise `delete_skill` leaves the table behind.

## Cross-links

- [SKILLS_IMPORT.md](SKILLS_IMPORT.md) — import community skills
- [CANVAS.md](CANVAS.md) — the canvas built-in skill
- [HARDWARE.md](HARDWARE.md) — the serial_port built-in skill
- [MCP.md](MCP.md) — MCP servers as an alternative integration path
- [SOUL.md](SOUL.md) — the soul_editor built-in skill
