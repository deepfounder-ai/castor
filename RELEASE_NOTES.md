## v0.24.0 — Telegram Rich Messages (Bot API 10.1) + MiniMax provider

Feature release. The headline is end-to-end Telegram **Rich Messages** — Castor now renders the full Bot API 10.1 formatting dialect — plus a new **MiniMax** provider, a quieter Telegram chat, and test-suite hygiene fixes. No schema migrations. No breaking changes. Drop-in upgrade.

### Telegram: full Bot API 10.1 Rich Messages

Telegram's Bot API 10.1 (2026-06-11) added `sendRichMessage` / `editMessageText(rich_message=)`, taking an `InputRichMessage` with a `markdown` or `html` string that Telegram parses server-side. Castor now ships the agent's reply through that as the PRIMARY send path, so the agent's Markdown renders as actual rich content:

- **Headings** (`#`…`######`), **tables**, inline + display **math** (`$x^2$`, `$$E=mc^2$$`), ordered / unordered / **task lists** (real checkboxes), dividers, block + pull **quotations**, **footnotes**, **marked** text (`==x==`), sub/superscript.
- **Spoilers** (`||x||`), **underline**, **custom emoji**, and inline **media embeds** (`![](url "caption")` → photo / audio / video / GIF).
- **Live `<tg-thinking>` streaming** — private chats now stream the agent's reasoning in an ephemeral "Thinking…" block (via `sendRichMessageDraft`, which also fixes the long-broken draft path that always failed with `RANDOM_ID_INVALID`). The final message stays clean; reasoning lives only in the transient preview.
- The classic MarkdownV2 / HTML converters remain as the graceful fallback for deployments whose Bot API predates 10.1, with capability detection cached per process.

Along the way: agent-emitted raw HTML now renders instead of showing literal `<b>` tags; the blockquote MarkdownV2/HTML divergence was fixed (consecutive quote lines group into one block); and a terse Telegram-only capability hint tells the agent the surface supports rich Markdown + inline media so it uses them when helpful (the shared soul stays clean for web / CLI).

### Telegram: inbound non-text message types

Inbound parsing covered only text, caption, photo, document, and voice/audio — every other type (location, venue, contact, poll, dice, sticker, video, video_note, animation) hit a silent-drop gate and the user got no reply. `_describe_nontext_message` now maps each to a short bracketed text injection so the agent actually sees them.

### Telegram: system cron tasks no longer DM the owner

The owner was getting a `⏰ __synthesis_continuous__ — No pending items` DM every 15 minutes, plus similar noise from synthesis / coach / trajectory-prune. Cron notifications are now gated to user-created routines only; `__name__` system tasks stay silent.

### New provider: MiniMax

MiniMax (international) drops in as an OpenAI-compatible preset at `https://api.minimax.io/v1` (China: `https://api.minimaxi.com/v1`) — Bearer auth, no GroupId, sold as a token subscription. Default model suggestions for the M2 family (M2.5 / M2.1 / M2 / M1 / Text-01), editable in the UI; key-hint links straight to the MiniMax interface-key page.

### Test-suite hygiene

- `qwe_temp_data_dir` fixture leaked `castor_pytest_*` tempdirs on locked-Qdrant / crash teardown — one dev tree hit 8157 dirs / 24 GB. Now self-heals: startup sweep of stale dirs, a `pytest_sessionfinish` cleanup of this run's dirs, and Qdrant-close-before-rmtree.
- Migration tests moved off `tempfile.mkdtemp()` (which leaked) to pytest's `tmp_path`, and their sqlite connections close via `contextlib.closing`.

### Dependencies

9 Dependabot bumps merged: rich ≥15, Pillow ≥12.2, pyyaml ≥6.0.3, python-docx ≥1.2, markitdown ≥0.1.6, and four docker GitHub Actions (metadata/setup-buildx/login/build-push).

### Upgrading

`git pull` + restart. No config or schema changes.

To use the live Telegram rich formatting, just chat with the bot — replies render rich automatically. To use MiniMax, pick it in Settings → Provider, paste your token-subscription key, and choose a model.
