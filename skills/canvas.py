"""Canvas skill — render HTML in a sandboxed right-side panel.

For interactive forms, dashboards, and mockups / prototypes where text
output isn't enough. The agent writes HTML; the Web UI renders it in a
~480px panel on the right side of the chat, inside an iframe with
strict sandbox (no same-origin, no access to parent DOM).

Five tools:

  * ``canvas_render(html, title?, slug?)``   — fire-and-forget. Open the
                                                panel and render. Use for
                                                dashboards / mockups /
                                                status views.

  * ``canvas_prompt(html, title?, timeout_s?)`` — BLOCKS until the user
                                                  submits the form, closes
                                                  the panel, or the timeout
                                                  elapses. Returns the
                                                  submitted data as JSON.
                                                  Mirrors ``camera_capture``.

  * ``canvas_save(slug, title?, html?)``     — persist an artifact for
                                                later reopen. If ``html``
                                                is omitted, the current
                                                panel is implicitly saved
                                                via the live state (not
                                                supported in v1 — pass the
                                                html explicitly).

  * ``canvas_load(slug)``                    — open a saved artifact in
                                                the panel.

  * ``canvas_list(limit?)``                  — list saved artifacts as a
                                                markdown table.

## Security model

The model's HTML runs inside ``<iframe sandbox="allow-scripts allow-forms"
srcdoc="...">``. The iframe origin is ``"null"``, so it CANNOT:

  * read parent window DOM, cookies, or localStorage
  * navigate the top window
  * open popups
  * make same-origin XHR/fetch back to the qwe-qwe server with session cookies

It CAN:

  * run inline ``<script>`` and ``<style>``
  * fetch public CDN resources (Chart.js, fonts.googleapis, etc.) — but
    without parent auth, so it can't read your local data via the server
  * submit data back via ``parent.postMessage({type: "canvas_submit",
    request_id, data: {...}}, "*")``

The 256 KB HTML cap (enforced both in this skill and in the REST
endpoint) bounds storage growth.
"""
from __future__ import annotations

import json

DESCRIPTION = (
    "Render HTML in a sandboxed right-side panel. Use for interactive "
    "forms (canvas_prompt blocks until user submits), dashboards "
    "(canvas_render + canvas_save), and mockups / prototypes. The "
    "iframe is sandboxed — no access to parent DOM, no cookies, no "
    "same-origin. External CDN resources (Chart.js etc.) are allowed."
)

INSTRUCTION = (
    "When the user asks you to show structured information visually "
    "(dashboard, table, chart, mockup), or to collect structured "
    "input from them (form with multiple fields), use the canvas "
    "skill instead of long markdown.\n\n"
    "Two tools, two intents:\n"
    "  - canvas_render(html, title) — fire-and-forget. For dashboards, "
    "mockups, status views. Returns immediately, panel stays open until "
    "the user closes it.\n"
    "  - canvas_prompt(html, title) — BLOCKS until the user submits the "
    "form. Returns the form data as JSON. Use for any interaction "
    "where you NEED the answer in this turn.\n\n"
    "The iframe is sandboxed. To send form data back, your HTML's "
    "submit handler MUST call:\n"
    "  parent.postMessage({type: 'canvas_submit', data: {...}}, '*')\n"
    "  // (request_id is auto-injected by the parent — don't try to "
    "set it yourself)\n\n"
    "HTML hard limits: 256 KB total. Inline CSS is fine. Inline JS "
    "is fine. External CDN scripts (https://cdn.jsdelivr.net/...) "
    "are allowed but slower — prefer inlined for offline-friendly. "
    "DO NOT attempt to access cookies, localStorage, parent.document, "
    "or fetch /api/* — the sandbox blocks all of that.\n\n"
    "For dashboards the user wants to keep: after rendering, call "
    "canvas_save(slug='descriptive-slug', title='Human Title', "
    "html=<same html>) — slug becomes the permanent id, shown in the "
    "Canvases left-nav view."
)

_HTML_CAP_BYTES = 256 * 1024  # mirror server.py::_CANVAS_HTML_CAP_BYTES


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "canvas_render",
            "description": (
                "Open the right-side canvas panel and render HTML in a "
                "sandboxed iframe. Fire-and-forget — returns immediately. "
                "Use for dashboards, mockups, status views, anywhere you "
                "want to show rich UI without blocking on user input."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": (
                            "Complete HTML document. Inline <style> and "
                            "<script> are allowed. 256 KB cap."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title shown in the panel header.",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Optional sticky id. If supplied, the panel "
                            "remembers this slug so canvas_save can be "
                            "called without re-specifying."
                        ),
                    },
                },
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "canvas_prompt",
            "description": (
                "Render an interactive form and BLOCK until the user "
                "submits it. Returns the submitted form data as a JSON "
                "string. Mirrors camera_capture. Use ONLY for forms that "
                "the agent needs to receive an answer for in this turn — "
                "for plain dashboards use canvas_render."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": (
                            "Form HTML. Your <form> submit handler MUST "
                            "preventDefault() and call parent.postMessage("
                            "{type: 'canvas_submit', data: {...formData}}, "
                            "'*'). Without that, the agent will hang until "
                            "timeout."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title shown in the panel header.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": (
                            "Max seconds to wait for user input. Default 300 "
                            "(5 minutes). Range 5-1800."
                        ),
                    },
                },
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "canvas_save",
            "description": (
                "Save an HTML artifact to the canvas_artifacts table for "
                "later reopen. Idempotent — calling with an existing slug "
                "updates the row. The saved artifact shows up in the "
                "Canvases left-nav view."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": (
                            "Human-meaningful id like 'weekly-sales'. If "
                            "omitted, slugified from title."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Display name shown in the Canvases view.",
                    },
                    "html": {
                        "type": "string",
                        "description": (
                            "The HTML to save. Must be supplied — v1 does "
                            "not auto-grab the live panel state."
                        ),
                    },
                },
                "required": ["html"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "canvas_load",
            "description": (
                "Open a previously-saved canvas artifact in the panel by "
                "its slug. Use after canvas_list to surface a dashboard "
                "the user asks for by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug from canvas_list output.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "canvas_list",
            "description": (
                "List saved canvas artifacts (dashboards, forms, mockups) "
                "ordered by most-recently-updated. Returns a markdown table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max rows. Default 20, max 200.",
                    },
                },
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    if name == "canvas_render":
        return _do_render(args)
    elif name == "canvas_prompt":
        return _do_prompt(args)
    elif name == "canvas_save":
        return _do_save(args)
    elif name == "canvas_load":
        return _do_load(args)
    elif name == "canvas_list":
        return _do_list(args)
    return f"Unknown tool: {name}"


# ── Helpers ────────────────────────────────────────────────────────


def _check_html(html: str) -> str | None:
    """Validate html. Returns an error message if invalid, else None."""
    if not isinstance(html, str) or not html.strip():
        return "Error: 'html' required (non-empty string)."
    size = len(html.encode("utf-8", errors="replace"))
    if size > _HTML_CAP_BYTES:
        return (
            f"Error: html is {size} bytes, exceeds 256 KB cap. "
            f"Trim inlined assets or split into multiple artifacts."
        )
    return None


def _server_module():
    """Lazy server-module accessor.

    Mirrors the pattern in `skills/skill_creator.py:1180-1187` — we
    don't take a hard dependency on `server` at import time so this
    skill loads cleanly in CLI / test contexts where the FastAPI app
    isn't bootstrapped.
    """
    import sys
    return sys.modules.get("server")


# ── canvas_render ──────────────────────────────────────────────────


def _do_render(args: dict) -> str:
    html = args.get("html") or ""
    err = _check_html(html)
    if err:
        return err
    title = (args.get("title") or "").strip() or "Canvas"
    slug = (args.get("slug") or "").strip() or None

    server = _server_module()
    if server is None or not getattr(server, "_ws_loop", None) or not getattr(server, "_ws_clients", None):
        return (
            "Canvas panel cannot open — no Web UI client is connected. "
            "Open the Web UI (python cli.py --web) and try again."
        )

    ok = server.broadcast_canvas_render_sync(html=html, title=title, slug=slug)
    if not ok:
        return "Canvas render: WS broadcast failed (no client?)."
    return f"Canvas opened: {title} ({len(html)} bytes of HTML)."


# ── canvas_prompt ──────────────────────────────────────────────────


def _do_prompt(args: dict) -> str:
    html = args.get("html") or ""
    err = _check_html(html)
    if err:
        return err
    title = (args.get("title") or "").strip() or "Form"
    timeout_s = float(args.get("timeout_s") or 300.0)
    timeout_s = max(5.0, min(1800.0, timeout_s))

    server = _server_module()
    if server is None or not getattr(server, "_ws_loop", None) or not getattr(server, "_ws_clients", None):
        return (
            "Canvas prompt cannot open — no Web UI client is connected. "
            "Open the Web UI and try again."
        )

    data = server.request_canvas_prompt_sync(html=html, title=title, timeout=timeout_s)
    if data is None:
        return (
            f"Canvas timeout — user did not submit within {int(timeout_s)}s. "
            "Either the user couldn't fill the form in time, or the form's "
            "submit handler isn't calling parent.postMessage correctly."
        )
    if not data:
        return "Canvas closed by user without submission."
    try:
        return f"Form submitted:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
    except Exception:
        return f"Form submitted: {data!r}"


# ── canvas_save ────────────────────────────────────────────────────


def _do_save(args: dict) -> str:
    html = args.get("html") or ""
    err = _check_html(html)
    if err:
        return err
    title = (args.get("title") or "").strip()
    slug = (args.get("slug") or "").strip()

    server = _server_module()
    if server is None:
        return "Canvas save unavailable — server module not loaded."
    try:
        slug_used = server._canvas_save_artifact(
            slug=slug, title=title or (slug or "Untitled"), html=html,
        )
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error saving canvas: {e}"
    return (
        f"Canvas saved as '{slug_used}'. "
        f"Reopen anytime with canvas_load(slug='{slug_used}') or via the "
        f"Canvases view in the Web UI."
    )


# ── canvas_load ────────────────────────────────────────────────────


def _do_load(args: dict) -> str:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return "Error: 'slug' required. Use canvas_list to see saved artifacts."

    import db
    row = db.fetchone(
        "SELECT slug, title, html FROM canvas_artifacts WHERE slug=?", (slug,)
    )
    if not row:
        return f"Canvas '{slug}' not found. Use canvas_list to see saved artifacts."

    server = _server_module()
    if server is None or not getattr(server, "_ws_loop", None) or not getattr(server, "_ws_clients", None):
        return f"Canvas '{slug}' found but no Web UI client is connected to render it."

    ok = server.broadcast_canvas_render_sync(html=row[2], title=row[1], slug=row[0])
    if not ok:
        return f"Canvas '{slug}' load: WS broadcast failed."
    return f"Canvas loaded: {row[1]} (slug: {row[0]})."


# ── canvas_list ────────────────────────────────────────────────────


def _do_list(args: dict) -> str:
    limit = int(args.get("limit") or 20)
    limit = max(1, min(200, limit))

    import db
    rows = db.fetchall(
        "SELECT slug, title, updated_at FROM canvas_artifacts "
        "ORDER BY updated_at DESC LIMIT ?", (limit,)
    )
    if not rows:
        return "No saved canvas artifacts yet."

    import time
    lines = ["**Saved canvas artifacts:**", "", "| Slug | Title | Updated |", "|---|---|---|"]
    now = time.time()
    for slug, title, updated_at in rows:
        # Relative date for readability — "today / 3d ago / Mar 12"
        delta_days = int((now - (updated_at or 0)) / 86400)
        if delta_days < 1:
            rel = "today"
        elif delta_days < 2:
            rel = "yesterday"
        elif delta_days < 30:
            rel = f"{delta_days}d ago"
        else:
            rel = time.strftime("%b %d", time.localtime(updated_at))
        lines.append(f"| `{slug}` | {title or slug} | {rel} |")
    return "\n".join(lines)
