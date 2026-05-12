"""JS-side contract tests for the reorganized Settings → Tools tab.

The tab previously rendered 29 core tools + 9 pluggable skills as a
flat list with no search, no grouping. With user-created skills and
MCP tools landing in the same list, scanning got hopeless.

The reorganization adds:
  - a search box that filters both sections
  - core tools grouped by category (Memory, Files & shell, Web,
    Vision, Vault, Automation, Profile, Meta)
  - per-section + per-category collapse, controlled by state.* flags
  - skill rows with tool-count badges + a "user" badge for
    user-created skills

These tests pin the structural contracts so a future refactor that
quietly drops the search or unflattens the categories fails loud.
Pattern matches other JS-contract tests in test_canvas.py and
test_telemetry.py — read static/index.html, grep for stable anchor
strings.
"""
from __future__ import annotations

from pathlib import Path


def _read_index_html() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


# ── State + helpers ────────────────────────────────────────────────


def test_tools_tab_state_fields_exist():
    """The new state fields drive the filter + collapse UX. They live
    next to other settings state at the top of the SPA's state object.
    A refactor that removes them would break the tab silently."""
    src = _read_index_html()
    for field in ("toolsTabSearch:", "toolsCoreOpen:", "toolsSkillsOpen:", "toolsExpandedCats:"):
        assert field in src, f"state field {field!r} missing"


def test_categoryForCoreTool_helper_exists():
    """Tools are categorized into human-friendly buckets via the
    `categoryForCoreTool` helper. Without it, the Core section would
    fall back to a flat list (the bug the reorganization fixed)."""
    src = _read_index_html()
    assert "const categoryForCoreTool = (n) =>" in src, (
        "categoryForCoreTool helper missing — Core tools won't be "
        "grouped, defeating the reorganization."
    )
    # Every category in the human-facing order list must be returned
    # by at least one branch of the helper. (Sanity check: the order
    # array and the helper agree.)
    helper_at = src.find("const categoryForCoreTool")
    helper_window = src[helper_at: helper_at + 1000]
    for cat in ("Memory", "Knowledge base", "Files & shell", "Web & HTTP",
                "Vision", "Automation", "Vault", "Profile", "Meta"):
        assert cat in helper_window, f"category {cat!r} not in helper"


def test_core_tool_category_order_is_explicit():
    """The display order of categories isn't alphabetical — Memory
    + Knowledge base lead because they're the most-used buckets,
    Meta lands last because it's plumbing the user rarely thinks
    about. Pin the array so a refactor doesn't quietly sort.it."""
    src = _read_index_html()
    arr_at = src.find("CORE_TOOL_CATEGORY_ORDER")
    assert arr_at >= 0
    window = src[arr_at: arr_at + 400]
    mem_idx = window.find("'Memory'")
    meta_idx = window.find("'Meta'")
    assert mem_idx >= 0 and meta_idx >= 0
    assert mem_idx < meta_idx, "Meta must come AFTER Memory in display order"


# ── Render structure ───────────────────────────────────────────────


def test_search_box_is_rendered_in_tools_tab():
    """The search box is the entry point for filtering. Without it,
    a user with 60+ tools has to scroll through everything."""
    src = _read_index_html()
    tab_at = src.find("function renderTabTools()")
    assert tab_at >= 0
    body = src[tab_at: tab_at + 8000]
    assert "data-tools-search" in body, (
        "Search input attribute missing from renderTabTools — filter "
        "UX broken."
    )
    assert 'placeholder="Filter tools or skills…"' in body or \
           "Filter tools or skills" in body


def test_clear_button_only_renders_when_search_has_text():
    """Small UX detail: the × clear button should only show when
    there's something to clear, otherwise it's noise. Pinned via
    the conditional grep."""
    src = _read_index_html()
    # The conditional is a ternary on state.toolsTabSearch
    assert "state.toolsTabSearch" in src and 'data-act="tools-search-clear"' in src


def test_core_section_collapsed_by_default():
    """Core tools are always on — most users don't need to scan
    them. Default state has the section collapsed (toolsCoreOpen:
    false) so the tab opens with the relevant section (Skills)
    visible and Core tucked away."""
    src = _read_index_html()
    assert "toolsCoreOpen: false" in src, (
        "Core section should default to collapsed — toolsCoreOpen "
        "must initialize to false."
    )


def test_skills_section_expanded_by_default():
    """Skills are the section users actually toggle, so it opens
    by default."""
    src = _read_index_html()
    assert "toolsSkillsOpen: true" in src


def test_search_auto_expands_collapsed_sections():
    """When the user types a query, hidden results would be useless
    — so an active search forces both sections + all categories
    open. Pinned via the `!!q ||` shortcut in coreOpen / catOpen /
    skillsOpen."""
    src = _read_index_html()
    tab_at = src.find("function renderTabTools()")
    body = src[tab_at: tab_at + 8000]
    # coreOpen and skillsOpen are computed with `!!q || state.*`
    assert "!!q || state.toolsCoreOpen" in body
    assert "!!q || state.toolsSkillsOpen" in body
    # And per-category catOpen too
    assert "!!q || !!state.toolsExpandedCats" in body or \
           "catOpen = (cat) => !!q" in body


def test_skill_rows_show_tool_count_badge():
    """Each skill row shows how many tools the skill exposes — useful
    signal when the skill name alone is ambiguous (e.g. 'browser' has
    24 tools, 'weather' has 1)."""
    src = _read_index_html()
    tab_at = src.find("function renderTabTools()")
    body = src[tab_at: tab_at + 8000]
    assert "tt-toolcount" in body, "tool-count badge missing on skill rows"


def test_user_skills_get_badge():
    """User-created skills (from skill_creator) carry s.user_skill in
    the /api/skills payload. The row renders a small 'user' badge so
    the user can distinguish their own skills from the bundled ones
    when the list grows."""
    src = _read_index_html()
    assert "tt-userbadge" in src, "user-skill badge class missing"
    tab_at = src.find("function renderTabTools()")
    body = src[tab_at: tab_at + 8000]
    assert "s.user_skill" in body


# ── Event wiring ───────────────────────────────────────────────────


def test_search_input_handler_wired():
    """The input must trigger a re-render so results update live as
    the user types."""
    src = _read_index_html()
    wired_at = src.find('data-tools-search]\').forEach')
    assert wired_at >= 0, "Search input handler not wired in wireEvents"
    window = src[wired_at: wired_at + 1000]
    assert "state.toolsTabSearch = " in window
    assert "render()" in window
    # Must preserve focus/cursor after re-render — otherwise typing
    # one character closes the input. Pinned by the post-render
    # focus() call.
    assert ".focus()" in window or "fresh.focus" in window, (
        "Search handler doesn't restore focus after render — input "
        "loses focus on every keystroke."
    )


def test_section_toggle_handler_wired():
    src = _read_index_html()
    wired_at = src.find('data-toggle-tools]\').forEach')
    assert wired_at >= 0
    window = src[wired_at: wired_at + 500]
    assert "toolsCoreOpen" in window and "toolsSkillsOpen" in window


def test_category_toggle_handler_wired():
    src = _read_index_html()
    wired_at = src.find('data-toggle-toolcat]\').forEach')
    assert wired_at >= 0
    window = src[wired_at: wired_at + 500]
    assert "toolsExpandedCats" in window
