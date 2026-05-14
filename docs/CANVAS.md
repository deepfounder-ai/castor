# Canvas — render HTML in a side panel for forms, dashboards, mockups

The `canvas` skill gives the agent a **rich-UI escape hatch**: when text + chips aren't enough, the agent can ship a sandboxed HTML document that renders next to the chat. Three use cases drive the design:

1. **Interactive forms** — the agent asks the user for structured input (name + phone + source), the user fills + submits, the agent receives the data in the same turn.
2. **Dashboards** — the agent renders a styled HTML page with numbers, charts, tables; the user saves it; next week the user reopens it from the **Canvases** view.
3. **Mockups / prototypes** — the agent renders a layout for visual discussion; the user gives feedback in chat; the agent iterates and re-renders.

## Surface

The skill is **auto-active** on every install (it's in `_DEFAULT_SKILLS`). Discoverable via `tool_search("canvas")` and any of: `dashboard`, `form`, `mockup`, `prototype`, `widget`, `chart`, `visualize`, `render`, `artifact`, `ui`, `survey`, `questionnaire`, `panel`.

| Tool | When to use | Returns |
|---|---|---|
| `canvas_render(html, title?, slug?)` | Fire-and-forget. Dashboards, mockups, status views. | Immediately. |
| `canvas_prompt(html, title?, timeout_s?)` | **Blocking** form. Agent needs the user's input this turn. | The submitted form data as JSON, OR a close/timeout marker. |
| `canvas_save(slug, title?, html)` | Persist an artifact for later reopen. Idempotent — upserts by slug. | The slug used. |
| `canvas_load(slug)` | Open a previously-saved artifact in the panel. | Status message. |
| `canvas_list(limit?)` | Browse saved artifacts as a markdown table. | Table. |

## How forms talk back (postMessage protocol)

The HTML the model writes runs in `<iframe sandbox="allow-scripts allow-forms">` — **no `allow-same-origin`**. The iframe cannot access parent cookies, localStorage, or DOM. The only channel back to the agent is `window.postMessage`.

The model's HTML must include:

```html
<form id="my-form">
  <input name="full_name" required>
  <input name="phone" required>
  <select name="source">
    <option>Google</option>
    <option>Referral</option>
  </select>
  <button type="submit">Save</button>
</form>
<script>
  document.getElementById('my-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target).entries());
    parent.postMessage({type: 'canvas_submit', data}, '*');
  });
</script>
```

When the user clicks Save, the parent window's listener relays the data to the server as a `canvas_event` WS message. If the form was opened by `canvas_prompt`, the agent's pending tool call resolves with the submitted data as a JSON string. If it was opened by `canvas_render` (fire-and-forget), the data lands as a synthetic user-turn message instead.

Other message types the iframe can send:

- `{type: 'canvas_close_request'}` — user clicked an in-canvas "Cancel" button. The parent closes the panel and tells the server the prompt was dismissed.
- `{type: 'canvas_save_request', slug, title, html}` — explicit save from a button in the model's HTML. Server upserts into `canvas_artifacts`.

## What the sandbox blocks

The iframe is rigorously sandboxed. **Allowed:**

- Inline `<style>` and `<script>` for layout, interactivity, validation
- External CDN resources — Chart.js, fonts.googleapis.com, etc. (no auth flows through; lib loads work, your cookies don't)
- `parent.postMessage` for the protocol above

**Blocked:**

- Reading `parent.document` / `parent.cookie` / `parent.localStorage` — origin is `"null"`, same-origin policy blocks
- Top-level navigation (`top.location = ...`) — `allow-top-navigation` is off
- Popups (`window.open`) — `allow-popups` is off
- Same-origin XHR/fetch against castor's own server with session cookies — origin `null` doesn't carry the user's auth

## Size cap

**256 KB** per HTML document, enforced at:

1. **Skill entry** — `canvas_render` and `canvas_save` reject oversize before broadcasting / DB write. Error message is human-readable so the agent can recover.
2. **REST POST `/api/canvas/artifacts`** — returns 413 with the same error.

Inlined SVG charts and a small JS library fit easily. If you need more, split the artifact (e.g. multi-page dashboard with `canvas_load` between pages).

## Storage

Saved artifacts live in `~/.castor/castor.db` in the `canvas_artifacts` table:

| Column | Type | Notes |
|---|---|---|
| `slug` | TEXT PRIMARY KEY | Human-meaningful id like `weekly-sales`. Slugified from title if omitted. |
| `title` | TEXT NOT NULL | Display name shown in Canvases view. |
| `html` | TEXT NOT NULL | The artifact body (≤256 KB). |
| `created_at`, `updated_at` | REAL | UNIX timestamps. |
| `thread_id` | TEXT | Loose FK to threads (for "show me canvases I made in this thread"). |
| `meta` | TEXT | Optional JSON sidecar (tags, version, etc.). |

Migration: `migrations/006_canvas_artifacts.sql`.

## UI placement

The canvas panel is the **right-side slot** (480px wide) in the chat view. It's **mutually exclusive** with the inspector — opening canvas auto-closes the inspector, and toggling the inspector back on closes the canvas. On screens narrower than 1100px, the right slot is hidden entirely (same threshold as the inspector).

Browse saved artifacts via the new **Canvases** entry in the left navigation rail, alongside Memory / Routines / Presets. Click a card to open the artifact in the chat-side panel.

## Privacy

Canvas HTML lives entirely on your machine. The REST endpoints (`/api/canvas/artifacts*`) are local-only. No telemetry attaches the canvas content — `canvas_*` tool calls bucket into the existing `skills` telemetry category and report only `tool_calls_count` and `tool_errors_count`, never the artifact title, slug, or HTML.

## Patterns the agent should learn

The skill's `INSTRUCTION` (injected into the system prompt when any canvas tool is active) teaches the model:

- When to choose `canvas_render` vs `canvas_prompt` (fire-and-forget vs blocking)
- The exact postMessage shape for form submissions
- The 256 KB cap and how to react (split into multiple artifacts)
- That `parent.document` access is blocked — don't try

If the model emits broken HTML (missing the postMessage handler), `canvas_prompt` times out at 300s and returns a helpful error explaining what went wrong, so the agent can iterate.

## Reference iframe layout (HTML model output)

```html
<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Weekly sales</title>
  <style>
    body { font: 14px system-ui; padding: 16px; color: #222; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
  </style>
</head><body>
  <h1>Weekly sales — week 19</h1>
  <table>
    <tr><th>Product</th><th class="num">Units</th><th class="num">Revenue</th></tr>
    <tr><td>Apples</td><td class="num">142</td><td class="num">€213</td></tr>
    <tr><td>Pears</td><td class="num">87</td><td class="num">€174</td></tr>
  </table>
</body></html>
```

For a form, replace `<table>` with `<form>` plus the submit handler from the protocol section above. Same security model.
