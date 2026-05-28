<p align="center">
<pre>
 ██████╗ █████╗ ███████╗████████╗ ██████╗ ██████╗ 
██╔════╝██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗
██║     ███████║███████╗   ██║   ██║   ██║██████╔╝
██║     ██╔══██║╚════██║   ██║   ██║   ██║██╔══██╗
╚██████╗██║  ██║███████║   ██║   ╚██████╔╝██║  ██║
 ╚═════╝╚═╝  ╚═╝╚══════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝
</pre>
</p>

<h3 align="center">Business-oriented AI agent</h3>

<p align="center">
  Self-hosted AI agent ready to drop into business workflows. Bring any OpenAI-compatible LLM — Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, or a local model on your own hardware. Your data, your provider, your rules.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#interfaces">Interfaces</a> •
  <a href="docs/README.md"><b>Documentation</b></a> •
  <a href="#tools">Tools</a> •
  <a href="#skills">Skills</a> •
  <a href="#telegram-bot">Telegram</a> •
  <a href="#diagnostics">Doctor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.23.2-blue" alt="version">
  <img src="https://img.shields.io/badge/python-3.11+-green" alt="python">
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="license">
  <a href="https://t.me/castor_ai"><img src="https://img.shields.io/badge/community-Telegram-blue?logo=telegram" alt="Telegram"></a>
</p>

---

## What is Castor?

A **business-oriented AI agent** built to drop into real workflows: customer ops, internal automation, knowledge retrieval, scheduled reporting, custom integrations, **hardware on the floor**, and **rich UI in chat** (forms, dashboards, mockups). Deploys on your infrastructure — a workstation, your own server, or the cloud account you already have. Chat via web UI, terminal, or Telegram, with tools, semantic memory, browser control, MCP integrations, a cron-like scheduler, direct USB/serial access to scales, scanners, GPS, label printers, and PLCs — and a sandboxed canvas panel where the agent can render arbitrary HTML for visual artifacts.

**Bring your own LLM**: works with any OpenAI-compatible provider — Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, DeepSeek, Together — or a local model via LM Studio / Ollama if you need everything on-prem. Your provider, your context window, your budget. Switch providers per-thread without restarting the agent.

> **Philosophy**: the system around the LLM should do the heavy lifting. Tool search keeps the prompt lean, recall keeps state out of the conversation, scheduler runs work without you, skills extend capability without redeploys. The result is an agent that's reliable on whatever model you pick — small enough to run on a laptop or large enough to handle complex multi-step tasks.

## Why Castor

| | Castor | Hosted SaaS agents |
|---|---|---|
| **Data** | Stays on your infrastructure | Sent to the vendor |
| **LLM choice** | Any OpenAI-compatible provider | Locked to vendor's model |
| **Customization** | Full code + soul + skills | System prompt + few hooks |
| **Cost model** | Your existing LLM bill, no per-seat | Per-seat / per-action SaaS pricing |
| **Compliance** | Self-hosted = your audit trail | Vendor's compliance posture |
| **Extensibility** | Skills, MCP, custom tools | Vendor's marketplace |
| **Hardware access** | Native USB / serial — scales, scanners, GPS, PLCs | None (cloud agents can't see your floor) |
| **Reliability** | No vendor outages or rate limits | Vendor SLA |

## Quick Start

### Prerequisites

- **Python 3.11+**
- **An LLM endpoint** — pick one:
  - **Hosted** (any OpenAI-compatible API): Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter, DeepSeek, Together. Set `CASTOR_LLM_URL` + `CASTOR_LLM_KEY` and you're done.
  - **Local** (data stays on-prem): [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.ai) with any tool-capable model. Qwen 9B / Gemma 4B work well on a single consumer GPU; bigger models if you have the hardware.
- **Embeddings**: FastEmbed (ONNX, local, CPU) — multilingual-MiniLM (384d, 50+ languages) + SPLADE++. Runs comfortably on a laptop without a GPU.

### Install

Runs natively on **Linux**, **macOS** (Intel & Apple Silicon) and **Windows 10/11** — single `pip install -e .` pulls every runtime dep (including MarkItDown, python-docx/pptx, openpyxl, pdfminer.six, pypdf, fastembed, qdrant-client, uvicorn).

#### 🐧 Linux / 🍎 macOS — one-line

```bash
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/castor/main/install.sh | bash
```

This clones the repo, creates a venv, installs everything, verifies critical deps, pre-downloads the embedding model, and drops `castor` on your `$PATH`.

#### 🪟 Windows

```cmd
git clone https://github.com/deepfounder-ai/castor.git
cd castor
setup.bat
```

On Windows shell commands are routed through **Git Bash** (auto-detected at install time — install [Git for Windows](https://git-scm.com/download/win) if missing). Falls back to `cmd.exe` if not found.

#### Manual (any platform)

```bash
git clone https://github.com/deepfounder-ai/castor.git
cd castor

# Create venv
python3 -m venv .venv            # or `python -m venv .venv` on Windows
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows PowerShell / cmd

# Install package + all runtime deps
pip install -e .

# Verify everything is wired
castor --doctor
```

#### Update an existing install

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/deepfounder-ai/castor/main/install.sh | bash

# Any platform, inside the checkout:
git pull && pip install -e . --upgrade
```

The update script is idempotent — re-running it detects an existing checkout and refreshes deps.

### Run

```bash
castor              # terminal chat
castor --web        # web UI at http://localhost:7860
castor --doctor     # check everything works
```

LM Studio / Ollama are auto-detected on localhost during setup. If your server is on another machine:
```bash
export CASTOR_LLM_URL=http://<your-ip>:1234/v1
```

### System requirements

For **hosted-LLM** deployments, Castor itself is light — any modern laptop or small VM works (the agent process is ~300MB resident, plus Qdrant on disk for memory).

For **local-LLM** deployments where the model runs on the same machine:

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | 4GB VRAM (4B Q4) | 8GB VRAM (9B Q4_K_M) or larger |
| RAM | 8GB | 16GB |
| Storage | 10GB | 20GB (models + memory) |

Works on: gaming laptops, desktop GPUs (RTX 3060+), Mac M1+ (via Ollama), Linux servers.

## Architecture

```
                               +-- Qdrant (semantic memory, hybrid search)
CLI (terminal)  <--+           +-- RAG (file indexing & search)
Web UI (browser) <--+-- Agent -+-- SQLite (history, threads, state)
Telegram bot    <--/    Loop   +-- Tools (8 core + tool_search)
                        |      +-- Skills (9 built-in, user-creatable)
                        |      +-- Browser (Playwright/Chromium)
                        |      +-- MCP (external tool servers)
                        |      +-- Scheduler (cron tasks)
                        |      +-- Vault (encrypted secrets)
                        |      +-- Hardware (USB/serial via pyserial — scales,
                        |                    scanners, GPS, PLCs, sensors)
                        |      +-- Canvas (sandboxed HTML side panel — forms,
                        |                  dashboards, mockups)
                        v
                   LLM (local or cloud)
                   10 providers supported
```

### Engineering around the LLM

These are the techniques the agent uses to stay reliable across model sizes — they make small models capable enough for production work *and* keep large models cheap by burning fewer tokens per turn:

- **Tool Search** — only 8 core tools loaded by default (~750 tokens); model calls `tool_search("keyword")` to activate more. Saves **75% tokens** vs loading all 49 tools
- **Compact system prompt** (~1200 tokens) — no redundant tool descriptions
- **JSON repair engine** — fixes malformed tool calls (trailing commas, unclosed brackets, single quotes)
- **Anti-hedge nudge** — if model talks instead of acting, it gets pushed to use tools
- **Self-check validation** — validates tool args before execution, with required-field checks
- **Smart compaction** — summarizes old messages when context fills up, saves to memory
- **Stuck detection** — warns model after 5+ tool errors per turn
- **Experience learning** — agent remembers past task outcomes and adapts strategies
- **Shell via Git Bash** — UNIX commands work on Windows, auto-detected

## Interfaces

- **Web UI** — `castor --web` (add `--ssl --port 7861` for mic/camera). Single-file SPA, zero runtime JS deps. Chat, memory browser, scheduler, presets, settings, knowledge graph, canvas panel, live voice mode.
- **Terminal** — `castor`. Rich-formatted chat with 20+ slash commands (`/soul`, `/skills`, `/memory`, `/model`, `/cron`, `/doctor`, …).
- **Telegram** — full mobile access: streaming replies, slash commands, topic-to-thread mapping, image vision. Setup → [docs/TELEGRAM.md](docs/TELEGRAM.md).

## Features

Castor's design principle: the system around the LLM does the heavy lifting, so the agent stays reliable on small local models and cheap on large hosted ones. Each feature below has a deep-dive guide in [`docs/`](docs/README.md).

**Tool Search** — a meta-tool architecture that keeps the prompt lean. Only ~8 core tools load by default (`memory_search`, `memory_save`, `read_file`, `write_file`, `shell`, `http_request`, `spawn_task`, `tool_search`); the model calls `tool_search("browser")` / `"schedule"` / `"secret"` / … to activate the rest on demand. Saves ~75% of the tokens a flat 49-tool list would burn.

**Memory & Knowledge Graph** — 3-layer system in one Qdrant collection: raw facts (saved instantly) → entities with typed relations → wiki summaries (both built by a nightly synthesis job). Hybrid retrieval fuses dense (FastEmbed MiniLM, 384d, 50+ languages) + sparse (SPLADE++) + BM25 via RRF. Thread-isolated, auto-chunked, secret-scrubbed. Interactive force-directed graph in the Web UI. → [docs/MEMORY.md](docs/MEMORY.md)

**Knowledge ingest** — 50+ formats via Microsoft MarkItDown: PDF / DOCX / PPTX / XLSX / EPUB / HTML / code / data / images. Drop files, paste a URL, or scan a folder. Chunked, embedded, and queued for entity + wiki synthesis. → [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)

**Skills** — pluggable single-file Python modules. Nine built in (`browser`, `canvas`, `serial_port`, `mcp_manager`, `skill_creator`, `soul_editor`, `notes`, `timer`, `weather`); create new ones from chat (`skill_creator` runs a plan→code→validate pipeline) or import from the agentskills.io spec. → [docs/SKILLS.md](docs/SKILLS.md) · [docs/SKILLS_IMPORT.md](docs/SKILLS_IMPORT.md)

**Browser** — Playwright + Chromium. Navigate, read, click, fill forms, screenshot. Headless by default; visible mode for logged-in sessions and OAuth flows. → [docs/BROWSER.md](docs/BROWSER.md)

**Hardware** — the `serial_port` skill talks USB-serial / RS-232 / RS-485 to scales, barcode/RFID readers, GPS, label & receipt printers, PLCs (Modbus RTU), VFDs, and sensors. Cross-platform via `pyserial`. Actuator writes are gated behind an explicit `confirm=true` with a hex preview. → [docs/HARDWARE.md](docs/HARDWARE.md)

**Canvas** — render model-supplied HTML in a sandboxed 480px side panel: blocking forms that return submitted data, saveable dashboards, throwaway mockups. Iframe is `sandbox="allow-scripts allow-forms"` with no `allow-same-origin`, so generated HTML can't read parent state. → [docs/CANVAS.md](docs/CANVAS.md)

**Routines** — scheduled tasks that live as chat threads: each firing appends a turn, and corrections you add between runs become context for the next. Natural schedule syntax (`every 2h`, `weekdays 09:00`, `mon,wed,fri 14:30`). Per-routine USD budget caps. → [docs/ROUTINES.md](docs/ROUTINES.md)

**Goals** — long-running autonomous tasks. A durable SQLite queue + worker daemon survives disconnects and restarts; an orchestrator breaks the goal into subtasks, dispatches specialized subagents, and an acceptance gate validates deliverables before marking done. → [docs/GOALS.md](docs/GOALS.md)

**MCP** — connect external Model Context Protocol tool servers (stdio or HTTP). Tools surface as `mcp__server__tool` and flow through tool_search. Manage via chat (`mcp_manager`) or Settings. → [docs/MCP.md](docs/MCP.md)

**Providers** — any OpenAI-compatible endpoint (LM Studio, Ollama, OpenAI, OpenRouter, Groq, Together, DeepSeek, + more) plus a native Anthropic adapter for prompt caching & thinking budgets. Switch per-thread via `/model` or Settings. → [docs/PROVIDERS.md](docs/PROVIDERS.md)

**Voice & Camera** — live voice mode (VAD → STT → LLM → TTS → auto-listen), local or API STT, multiple TTS backends; camera capture via browser PiP or OpenCV. → [docs/VOICE.md](docs/VOICE.md) · [docs/CAMERA.md](docs/CAMERA.md)

**Personality (Soul)** — 8 adjustable traits (humor, honesty, curiosity, brevity, formality, proactivity, empathy, creativity) plus custom traits, agent name, and language. Edit via `/soul`, Settings, or chat. → [docs/SOUL.md](docs/SOUL.md)

**Cost tracking** — every LLM call records tokens + USD by thread, source, model, and provider, with LiteLLM-backed pricing. Surfaced in the Web UI. → [docs/COST_TRACKING.md](docs/COST_TRACKING.md)

The reliability internals that keep all of this working on small models — JSON repair, anti-hedge nudging, self-check, loop detection, compaction, auto-resume — are described under [Engineering around the LLM](#engineering-around-the-llm) above.

## Diagnostics

```bash
castor --doctor
```

Checks 30+ components: Python, deps, SQLite, Qdrant, provider + LLM API, model loaded, embeddings, inference latency, MCP servers, browser skill, Telegram, threads, skills, tools, cron/heartbeat, STT/TTS, indexed files, knowledge graph, synthesis, BM25 index, disk space, and logs.

## Config

Environment variables:

```bash
CASTOR_LLM_URL=http://localhost:1234/v1   # LLM server URL
CASTOR_LLM_MODEL=qwen/qwen3.5-9b          # Model name
CASTOR_LLM_KEY=lm-studio                  # API key
CASTOR_DB_PATH=~/.castor/castor.db      # Database path
CASTOR_DATA_DIR=~/.castor                # Where threads / memory / uploads live
CASTOR_QDRANT_MODE=disk                   # memory | disk | server
CASTOR_PASSWORD=                          # Web UI password (shows login modal if set)
CASTOR_STT_DEVICE=cpu                     # STT inference device (cpu | cuda)
```

Everything else (30+ knobs — `context_budget`, `rag_chunk_size`, `synthesis_time`, `tts_api_url`, etc.) lives in **Settings → Advanced → Settings** and persists in SQLite.

### Data layout

All user data in `~/.castor/` (configurable via `CASTOR_DATA_DIR`):

```
castor.db        SQLite — messages, threads, KV, settings
memory/           Qdrant vectors (disk mode)
wiki/             Synthesized markdown pages
skills/           User-created skills
uploads/          Images, documents, camera captures
  kb/             Knowledge-base files awaiting / done indexing
workspace/        Default CWD for relative paths (switches per-preset)
presets/<id>/     Installed presets (each with own workspace/, knowledge/, skills/)
logs/             castor.log (INFO+), errors.log (WARNING+)
```

## Docker

```bash
docker compose up
```

LM Studio / Ollama should be running on the host. Persistent data in `./data/`.

A module-by-module map of the codebase lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## Documentation

Per-feature user guides live in [`docs/`](docs/README.md). The hub indexes everything:

| Topic | Guide |
|---|---|
| LLM providers, where to get keys, switching per-thread | [docs/PROVIDERS.md](docs/PROVIDERS.md) |
| Personality (8 traits + name + language + custom) | [docs/SOUL.md](docs/SOUL.md) |
| Live Voice Mode, STT (local + API), TTS, Fish Speech | [docs/VOICE.md](docs/VOICE.md) |
| Camera capture, PiP overlay, vision models | [docs/CAMERA.md](docs/CAMERA.md) |
| Knowledge ingest — 50+ formats, URL/folder/YouTube | [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) |
| Memory — what to save, recall, secret scrubbing, vault | [docs/MEMORY.md](docs/MEMORY.md) |
| Browser modes — visible (logged-in) vs headless | [docs/BROWSER.md](docs/BROWSER.md) |
| Hardware — serial / USB / Modbus / scales / PLCs | [docs/HARDWARE.md](docs/HARDWARE.md) |
| Canvas — sandboxed HTML side panel | [docs/CANVAS.md](docs/CANVAS.md) |
| Skills — built-ins, skill_creator, anatomy | [docs/SKILLS.md](docs/SKILLS.md) |
| Skill import — skills.sh / Anthropic SKILL.md spec | [docs/SKILLS_IMPORT.md](docs/SKILLS_IMPORT.md) |
| Routines — scheduled tasks, debug-via-dialogue | [docs/ROUTINES.md](docs/ROUTINES.md) |
| MCP — Model Context Protocol clients | [docs/MCP.md](docs/MCP.md) |
| Telegram — bot setup, multi-user, voice / image | [docs/TELEGRAM.md](docs/TELEGRAM.md) |
| Presets — bundled role-specific configs | [docs/PRESET_GUIDE.md](docs/PRESET_GUIDE.md) |
| Privacy + telemetry contract | [docs/PRIVACY.md](docs/PRIVACY.md) |

## Contributing

**Contributions welcome.** Castor is a small open project — your PR won't get lost in a queue.

- 📘 Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup + workflow
- 🏗️ See [ARCHITECTURE.md](ARCHITECTURE.md) for the big picture
- 🐛 [Open an issue](../../issues/new/choose) if you found a bug or want a feature
- 💬 [Start a Discussion](../../discussions) for questions and workflow sharing
- 🔒 [Security vulnerabilities](SECURITY.md) — private report via GitHub Security Advisory
- 🤝 Everyone is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md)

### Good first issues

If you want to help but don't know where to start, we label easy tasks as [`good first issue`](../../issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22). Typical starting points:

- Add a new [skill](skills/) (weather, notes, timers — each is 50-100 lines of Python)
- Add a new [provider](providers.py) preset (`PRESETS` dict — ~5 lines)
- Improve [doctor checks](cli.py) — add detection for a new subsystem edge case
- Write [integration tests](tests/test_integration.py) for a 0%-covered module (check `pytest --cov`)

### What I'm NOT looking for

Be upfront so we don't waste each other's time:

- Cloud-first features that don't work offline
- Rewrites of the single-file web UI to React/Vue/Svelte
- Splitting `server.py` for the sake of splitting (until it's actually causing pain)
- Generic LLM wrapper features that exist in 20 other projects

### Housekeeping

Dependencies are tracked by [Dependabot](.github/dependabot.yml) — weekly grouped PRs for pip (minor + patch bundled) and monthly PRs for GitHub Actions land in the inbox. Security updates bypass the grouping and open their own PR immediately.

## Community

- 💬 [Telegram — @castor_ai](https://t.me/castor_ai) — quick chat, show-and-tell, release announcements
- 💭 [GitHub Discussions](../../discussions) — long-form questions, workflow sharing
- ⭐ If Castor is useful — **star the repo**. It's the clearest signal we're on the right track.

## License

MIT

---

<p align="center">
  Built with care by <a href="https://deepfounder.ai">DeepFounder</a>
</p>
