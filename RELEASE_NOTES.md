# v0.18.5 — Anonymous opt-in telemetry + first-run consent flow + blog feed in Presets

The headline in v0.18.5 is the **opt-in anonymous usage telemetry system** — fully built end to end this cycle (Stages 1–9), guarded by a strict whitelist + closed enums + two-gate consent contract. **Default OFF.** Every event is documented in `docs/PRIVACY.md`. There is no UI surface to redirect telemetry elsewhere — this is by design (single trust target, not a buffet of "alternative endpoints").

Three other features round out the release: a `thread_created` event for new-chat volume signal, a "From the blog" RSS strip in the Presets view, and two browser-cache bug fixes that were biting users immediately after opt-in.

## 📊 Anonymous opt-in telemetry — the long arc closes

`telemetry.py` ships with **6 whitelisted event types**, each prop type-strict and string-valued props enum-constrained. A future refactor that accidentally added a free-text field can't smuggle chat content past the validator without explicitly editing `ALLOWED_EVENTS`.

Events:

- `session_start` — version, OS, Python, provider kind, model size bucket, feature flags (booleans), counts (skills, jobs, indexed sources). **Never** the URL, model id, skill names.
- `turn_complete` — duration, rounds, tool *categories* used (closed enum), token counts, recall hits, source surface.
- `tool_error` — tool category + error kind (both enums). Never tool name, args, or message text.
- `skill_creator_pipeline` — outcome enum, attempts, duration, generated tools count.
- `feature_first_use` — first-time-per-session activation of major features (camera_capture, live_voice, scheduler_create, …) — closed enum.
- `thread_created` — new this release, see below.

### Wire format: Countly Community Edition

After a Plausible detour, settled on **self-hosted Countly** at `https://qwelytics.deepfounder.ai/i`. Native cross-day per-user retention without persistent server-side state — Plausible's daily-rotating salt would have made retention metrics impossible. Same code anyone could run; same privacy guarantees on the wire; same data inventory in `docs/PRIVACY.md`.

The HTTP sender is **production-grade**:

- Retry with exponential backoff `[1s, 2s, 4s]` on 5xx + network errors
- 4xx does NOT retry (config error, re-sending just spams)
- 10s urlopen timeout, single cap (urllib doesn't separate connect/read)
- Bounded queue (`maxlen=1000`) — never grows unbounded if a never-flushed install sits offline forever
- Lists become CSV strings on the wire (Countly's segmentation type)
- `duration_ms` props auto-mapped to Countly's native `dur` field for receiver-side averaging

### First-run consent flow

Two surfaces, same contract:

- **Web** (`static/index.html` boot hook) — modal "Help improve qwe-qwe?" appears once, only when `consent_decision_made` is false. Either button stamps the version. X / ESC fallback fires silent opt-out via `onClose` so the modal can't loop forever.
- **CLI** (`cli.py:main`) — TTY prompt, defaults to opt-out on no-interactive. Identical wording.

Settings → Privacy → Telemetry stripped to **two choices**: enable / disable. No "alternative endpoint" override surfaced — operators / forks edit `config.py::EDITABLE_SETTINGS` defaults directly.

### Two-gate consent enforcement

Both `track_event()` AND `flush()` refuse when `consent_needs_reprompt()` is true (stored consent version < `_CURRENT_CONSENT_VERSION`). Events queue but don't send. The yellow "policy updated, please re-confirm" banner in Settings → Privacy is now backed by **actual gates**, not advisory text.

Bumped to **consent v2** this release because `ALLOWED_EVENTS` shape changed (`thread_created` added, `SOURCES` enum widened). Existing v1 opt-ins see the re-confirm banner; events queue but don't send until they re-stamp.

### What's NEVER sent

> Chat content. Soul / personality. Memory entries. Knowledge-base text. RAG queries. File paths. Tool args / results. Exact model name. Provider URL. Skill names. IP / hostname / username / machine id. API keys. Telegram tokens.

Audit by grep — every collection goes through `telemetry.track_event()`, single funnel:

```bash
grep -rn "telemetry.track_event" .
```

## 💬 `thread_created` event — see how often new chats are created

Single-field event (`source` from a closed enum) firing once per `threads.create()` call. Lets the project see whether new-thread volume is driven by users (web/cli/telegram) or system surfaces (scheduler activations / preset onboarding). The thread name and meta are **never** part of the event — just the source bucket.

Wired into 6 production call sites: `cli.py`, `server.py`, `telegram_bot.py` (topic + DM), `scheduler.py` (create + backfill), `presets.py`. Lazy-imported helper in `threads.py` swallows any telemetry hiccup so a queue full / network blip never breaks thread creation.

## 📰 "From the blog" RSS strip in Presets

The Presets view now renders up to 5 newest project posts above the preset grid, fetched server-side from `https://deepfounder.ai/tag/qwe-qwe/rss/`. Compact list: title + relative date + 2-line description. Empty feed → nothing rendered, no empty box.

Backend is forgiving: 30-min in-process cache, 15s upstream timeout, every parser field length-bounded so a malicious upstream can't blow up the response. On any fetch error the endpoint still returns 200 with the last-known cached items + an `error` field — the Presets view never breaks because deepfounder.ai is down.

This is **project-controlled outbound HTTP, not telemetry** — empty body, no `anonymous_id`. The only signal `deepfounder.ai` receives is "an install asked for the feed" (your IP + `qwe-qwe/<ver>` UA). Documented under a new "Other project-controlled outbound HTTP" section in `docs/PRIVACY.md`.

## 🐛 Browser-cache bugs that were biting users immediately after opt-in

Two related fixes, both rooted in browsers heuristic-caching JSON GET responses (FastAPI doesn't set `Cache-Control` headers):

### #22 — "opt-in did not persist" false-positive toast (`bdcd459`)

The defensive verify-step added during the consent flow build was reading **stale-from-cache** `/status` responses right after a successful POST `/opt-in`. Backend persisted correctly; browser served the cached `cdm:false` to the verify call; UI screamed "did not persist". Removed the verify entirely — POST 2xx is now trusted, and HTTP-flow regression tests pin the actual contract in pytest where it belongs.

### #25 — telemetry modal re-opening on every reload (`f81cd49`)

Same root cause, different code path. Boot's `checkTelemetryFirstRun()` was getting served the cached pre-opt-in `/status` response on every page reload, re-opening the modal forever even though the backend correctly stored `consent_version=2`. **One-line generic fix**: `api()` helper now sets `cache: 'no-store'` on every JSON call. Server is the authoritative source; browsers never replay stale state. Prevents an entire class of "stale UI after POST" bugs across every settings panel.

## 🤝 Community merges

### #13 + #18 closed by @forhim007 — timer skill cancel + smoke_test scope (PR #20)

Combined PR landing two related fixes:

- **Smoke-test scope** — `_extract_execute_body()` AST helper scopes `_smoke_test`'s param-usage check to the body of `execute()` only, not the whole module source. Caught the field-session `camera_diagnostics` regression where `tool def: num_samples` / `code: args.get("samples", 30)` slipped through because `num_samples` literally appeared in the `TOOLS` dict.
- **Timer skill** — `list_timers` + `cancel_timer` tools added, backed by a thread-safe in-memory registry with 8-char UUID ids. Cancel removes from registry; the daemon thread's print-on-fire becomes a no-op.

13 new unit tests pinning both behaviors.

## 🛠️ Build / docs

- **Docker** image now publishes to `ghcr.io/deepfounder-ai/qwe-qwe` on every push to main + tags. Playwright/Chromium dependencies added to the image so the browser skill works out of the box.
- **`CLAUDE.md`** refreshed for the v0.18.x state — new sections for Skill Creator (5-step pipeline) and Telemetry (privacy contract + Countly), updated test count and provider count, soul rules 14 + 16 documented, extended add-a-feature checklist with telemetry + skills/gitignore quirks.
- **`docs/PRIVACY.md`** is now the single source of truth for what telemetry collects, where it goes, and what the project will never touch.

## 🔢 Stats

- **520+ tests pass**, 3 skip, 0 fail (was 495 in v0.18.4)
- **520 passed in 60s** in the full suite
- New test files: `test_blog_feed.py`, expanded `test_telemetry.py` (70 tests, was ~40)
- Coverage floor 24% holds

## 🚀 Upgrade

```bash
pip install -e . --upgrade        # from a checkout
# or
pip install --upgrade qwe-qwe     # if installed as a package
```

Telemetry stays off unless you opt in. Existing v1 opt-ins will see a yellow "policy updated, please re-confirm" banner the first time they open Settings → Privacy after upgrading — events queue but don't send until you re-stamp.

Hard-refresh the web UI once after upgrade (Ctrl+Shift+R / Cmd+Shift+R) to drop any stale browser cache from the pre-fix `api()` helper.
