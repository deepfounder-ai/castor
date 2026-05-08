# v0.18.4 — Community-driven cleanup + camera tuning + smoke_test fix

This release captures everything that was meant to ship as v0.18.3 (which never got tagged because of a CI failure) plus two community-merged PRs that closed long-standing skill_creator gaps. v0.18.4 is the first release after the README rebalanced positioning toward "business-oriented AI agent" — small/local models still fully supported, just no longer the headline.

## 🤝 Community merges

Three external contributors landed work this cycle:

### #13 closed by @forhim007 — `_smoke_test` now scopes param-usage check to `execute()` body (PR #17)

Pre-fix, the smoke test searched the **whole module source** for required param names. Since param names are by definition in the `TOOLS` dict literal, the substring check always passed — even when `execute()` didn't actually use them. The field-session `camera_diagnostics` skill caught itself in this gap: tool def declared `num_samples`, code used `args.get("samples", 30)`, smoke test happily approved.

@forhim007 added `_extract_execute_body(source)` — uses `ast.parse` to locate the `execute` `FunctionDef` and slice the body. The param-usage check now runs against THIS slice only. Returns empty string on failure (unparseable, no execute) so the caller degrades gracefully via simple truthiness.

8 new tests in `tests/test_skill_creator_smoke.py` covering the helper + before/after on the original repro.

### #15 closed by @dutchaiagency — `delete_skill` drops orphan tables (PR #19)

Before this fix, deleting a user-created skill removed only the `.py` file. The `skill_<name>_*` tables it had created in the shared SQLite stayed forever. Over many regenerate cycles, dead tables accumulated.

@dutchaiagency added two helpers:

- `_extract_skill_owned_tables(source, skill_name)` — regex with handling for backtick / quote / bracket variants of `CREATE TABLE`, optional `IF NOT EXISTS`, `isidentifier()` check. Only returns names matching the `skill_<name>_` prefix exactly (not `skill_name2_*`).
- `_drop_skill_owned_tables(skill_name, skill_path)` — runs before `target.unlink()` in `_delete_skill`. For each candidate table, verifies presence in `sqlite_master` BEFORE issuing `DROP TABLE`. Wraps the whole thing in try/except so cleanup failure doesn't block the file delete.

`delete_skill` return string now reports the count: `"Deleted skill 'X' (3 skill table(s) dropped)"`. Two tests pin the prefix-matching strictness.

### #18 — new bug from QA campaign

@dutchaiagency also filed #18: timer skill exposes only `set_timer`, no cancel/list path. Tagged `good first issue` with full acceptance criteria.

## 🎯 Repositioning

README rewritten to lead with the business-automation angle and de-emphasize the previous small-model-first framing:

- Tagline: `Self-hosted AI agent for business automation` → `Business-oriented AI agent`
- Sub-tagline: leads with hosted providers (Azure OpenAI, AWS Bedrock, OpenAI, Groq, OpenRouter), local as on-prem alternative
- "Why Small Models" section replaced with "Why qwe-qwe" comparing self-hosted vs vendor SaaS agents (data, LLM choice, cost, compliance, extensibility, reliability)
- "Recommended hardware" → "Hardware" with conditional structure: hosted needs almost nothing, local-LLM table follows
- "Small-model optimizations" → "Engineering around the LLM" — same techniques, framed as benefiting all model sizes
- Removed "100% offline" badge — too small-model-coded
- `soul.py` identity + `pyproject.toml` description updated to match

Local + small models remain a real value prop (on-prem privacy, offline, no per-token billing) — they're now a deployment choice in Quick Start, not the headline.

## 📷 Camera resolution + JPEG quality (the v0.18.3 work)

Two new settings in Settings → Camera → Capture quality:

**`camera_resolution`**: `auto` / `480p` / `720p` / `1080p`. Applied via `CAP_PROP_FRAME_WIDTH`/`HEIGHT` on device open. Each preset carries its own max-pixels cap for the resize step before JPEG encoding:

| Preset | Capture | Sent to LLM |
|---|---|---|
| auto | camera default | ≤256×192 (49K pixels, legacy cap) |
| 480p | 640×480 | ≤256×192 |
| 720p | 1280×720 | ≤512×384 (196K) |
| 1080p | 1920×1080 | ≤1024×768 (786K) |

**`camera_quality`**: int 1-100, default 70. Replaces hardcoded `cv2.IMWRITE_JPEG_QUALITY=70`. Read on every encode.

Trade-off the user controls: 1080p + quality 90 sends a sharp ~1MB base64 to the vision LLM (slow, expensive, but readable text + tiny details). auto + quality 50 sends a cheap 256×192 thumbnail.

9 unit tests in `tests/test_camera_settings.py` pin presets, helper behaviour, and config metadata. The 3 tests that import cv2 transitively are now `pytest.importorskip("cv2")`-guarded so CI without OpenCV cleanly skips them — that's what blocked v0.18.3 from auto-releasing. Fixed in this cycle.

## 🤖 Skill creator now generates working engineering skills (the v0.18.3 work, finally shipping)

Three coupled fixes from the field session that took skill_creator from "generates stubs that fail validation" to "generates camera-using engineering skills with raw OpenCV + statistics + time-series SQLite — first try":

- **Soul rule 14 expansion**: agent must STOP after `create_skill`, never `write_file` in `~/.qwe-qwe/skills/`, skills are SINGLE `.py` files (not directories), "run skill_name" = call its tool directly.
- **Pipeline `elif`→`if` fix**: when execute_body is empty (all tools custom), prompt the LLM to start the FIRST tool with `if name == "..."`. Plus defensive regex post-process. Closed the recurring `syntax error on attempt N: invalid syntax (line 70)` that bit every workspace_meter / camera_diagnostics attempt.
- **End-to-end pipeline test**: `tests/test_skill_creator_pipeline.py` with mocked LLM, asserts produced `.py` calls `tools.execute("camera_capture")` + `memory.save()` + uses `skill_<name>_` table prefix + parses cleanly.

## 🖱️ task_update no longer ghost-streams the chat

After `create_skill`, the chat indicator stayed in "generating" state with the typing dot blinking forever — `task_update` WS events were falling through to the streaming-message-creation branch. Now handled explicitly: surfaces as toast (`✅`/`❌` styling), auto-refreshes the skills list on success.

## 🔧 CI fix that unblocked everything

`tests/test_camera_settings.py` was importing `cv2` directly in 3 tests. CI doesn't install OpenCV (it's an optional heavy dep) so those tests `ModuleNotFoundError`'d and the whole Tests workflow failed. That meant **release.yml was skipped on every push since v0.18.2** because it gates on Tests success. v0.18.3 was bumped in code but never tagged on GitHub.

Fixed by `pytest.importorskip("cv2")` at the top of those 3 tests. CI without OpenCV now cleanly skips them; CI with OpenCV runs them. Local dev unchanged.

## 🔄 Upgrading

```bash
git pull && pip install -e .
# restart cli.py --web for camera + skill_creator fixes
# hard-reload the browser tab (Ctrl+Shift+R) for the UI fix
```

No data migration needed.

## 📊 Stats

- 9 commits since v0.18.2 (no v0.18.3 was ever tagged — bumped in code but blocked on CI)
- **2 community PRs merged** (PR #17 forhim007, PR #19 dutchaiagency)
- 4 community contributors active this cycle: forhim007, dutchaiagency, snakefood3232, EugeneKorr
- 394 → 418 tests passing (+24)
- Full lint + JS-syntax + 3.11/3.12 pytest matrix green

## 🐞 Open for community

- [#8](https://github.com/deepfounder-ai/qwe-qwe/issues/8) onnxruntime-gpu doctor check — claimed by @snakefood3232
- [#14](https://github.com/deepfounder-ai/qwe-qwe/issues/14) AST fix for code outside if/elif branches — claimed by @snakefood3232 (48h promise)
- [#18](https://github.com/deepfounder-ai/qwe-qwe/issues/18) timer skill cancel/list — `good first issue` with acceptance criteria
- [#12](https://github.com/deepfounder-ai/qwe-qwe/issues/12) ongoing QA campaign — find more bugs, file with repro
