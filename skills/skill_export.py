"""Export Castor `.py` skills into the agentskills.io SKILL.md format.

Companion to ``skills/skill_import.py``. Import already supports loading
agentskills.io-format skills FROM the public Skills Hub. Export goes the
other direction — turn a local Castor skill into a SKILL.md bundle that
can be shared via the same standard.

The exported bundle is a directory:

    <name>/
        SKILL.md                   # YAML frontmatter + markdown body
        scripts/<name>.py          # the original .py (assets layer)
        references/CASTOR_TOOLS.md # optional — the TOOLS schema as a
                                   #   human-readable reference

Frontmatter follows the agentskills.io v1 spec:

    ---
    name: my-skill
    description: One-line skill description (<= 256 chars)
    metadata:
      source: castor
      castor_version: <version>
      exported_at: <ISO date>
    ---

The body is the skill's INSTRUCTION docstring or, if absent, a generated
summary of the tools it exposes.

NOT in scope: round-tripping perfectly so an exported-then-imported skill
behaves identically. Castor's `.py` is the source of truth; the SKILL.md
is a portable wrapper. Re-importing produces a thin adapter (per skill_import).
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path


class SkillExportError(Exception):
    """Raised on any export failure with a stable ``code`` attribute for
    REST consumers (the message is human-readable; the code is what
    callers branch on)."""
    def __init__(self, message: str, code: str = "export_failed"):
        super().__init__(message)
        self.code = code


# Names that are NOT real skills - built-in helpers, package internals.
_RESERVED_NAMES = frozenset({
    "__init__", "skill_creator", "skill_import", "skill_export",
})


def _validate_skill_name(name: str) -> None:
    """The agentskills.io spec requires names to match
    ``^[a-z0-9]+(-[a-z0-9]+)*$``, <= 64 chars. We accept Castor names too
    (snake_case) and convert at export time."""
    if not name or not isinstance(name, str):
        raise SkillExportError("name must be a non-empty string",
                               code="bad_name")
    if len(name) > 64:
        raise SkillExportError(f"name too long ({len(name)} > 64 chars)",
                               code="bad_name")
    if name in _RESERVED_NAMES:
        raise SkillExportError(f"reserved skill name: {name!r}",
                               code="reserved_name")


def _slugify(name: str) -> str:
    """Convert ``my_great_skill`` -> ``my-great-skill`` for SKILL.md
    frontmatter. Lowercases, replaces underscores/spaces with hyphens,
    strips non-[a-z0-9-] chars.
    """
    out = name.lower()
    out = re.sub(r"[\s_]+", "-", out)
    out = re.sub(r"[^a-z0-9-]+", "", out)
    out = re.sub(r"-+", "-", out).strip("-")
    return out or "unnamed-skill"


def _safe_parse_tools(node: ast.AST) -> list:
    """Safely parse a TOOLS = [...] literal at module level.

    Uses ast.literal_eval - which despite the name only evaluates
    LITERAL values (str/int/float/list/dict/tuple/set/None/bool). NOT
    code execution. This is the standard secure replacement for eval()
    when parsing structured literals.

    Returns [] if the TOOLS assignment is anything other than a flat
    literal (e.g. built via list comprehension or imports) - the skill
    still exports, the tools reference just won't be included.
    """
    try:
        parsed = ast.literal_eval(node)
        if isinstance(parsed, list):
            return parsed
        return []
    except (ValueError, SyntaxError):
        return []


def _extract_skill_metadata(skill_py_path: Path) -> dict:
    """Parse a Castor skill .py file to extract metadata WITHOUT importing
    it. Uses AST so we don't execute side-effects (which could run
    arbitrary code at import time on untrusted skills).

    Returns ``{description, instruction, tools, module_docstring}``.
    Missing fields default to empty string / empty list - the export
    proceeds; the body just looks thinner.
    """
    if not skill_py_path.is_file():
        raise SkillExportError(f"skill file not found: {skill_py_path}",
                               code="not_found")
    try:
        source = skill_py_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillExportError(f"cannot read skill source: {e}",
                               code="read_error") from e

    try:
        tree = ast.parse(source, filename=str(skill_py_path))
    except SyntaxError as e:
        raise SkillExportError(f"skill has syntax errors: {e}",
                               code="syntax_error") from e

    out = {
        "description": "",
        "instruction": "",
        "tools": [],
        "module_docstring": ast.get_docstring(tree) or "",
    }

    for node in tree.body:
        # Top-level assignments DESCRIPTION = "...", INSTRUCTION = "...",
        # TOOLS = [...]
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if target.id == "DESCRIPTION":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    out["description"] = node.value.value
            elif target.id == "INSTRUCTION":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    out["instruction"] = node.value.value
            elif target.id == "TOOLS":
                out["tools"] = _safe_parse_tools(node.value)
    return out


def _build_frontmatter(name: str, description: str,
                       extras: dict | None = None) -> str:
    """Emit the YAML frontmatter block. We write it by hand (not via
    PyYAML) so the output is canonical and stable across PyYAML versions
    - agentskills.io's frontmatter contract is a tiny subset of YAML.
    """
    slug = _slugify(name)
    lines = ["---", f"name: {slug}"]
    # Quote description if it contains anything that needs escaping
    if description and any(c in description for c in ":#'\""):
        # JSON-style quoting is valid YAML and renders escapes correctly.
        lines.append(f"description: {json.dumps(description)}")
    else:
        lines.append(f"description: {description or '(no description)'}")
    lines.append("metadata:")
    lines.append("  source: castor")
    lines.append(f"  exported_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    try:
        import config as _config
        if getattr(_config, "VERSION", None):
            lines.append(f"  castor_version: {_config.VERSION}")
    except Exception:
        pass
    if extras:
        for k, v in extras.items():
            if isinstance(v, str) and not any(c in v for c in ":#'\""):
                lines.append(f"  {k}: {v}")
            else:
                lines.append(f"  {k}: {json.dumps(v)}")
    lines.append("---")
    return "\n".join(lines)


def _build_body(metadata: dict) -> str:
    """Compose the markdown body of SKILL.md. Prefers the skill's own
    INSTRUCTION over its module docstring over a generated tools-summary.
    """
    parts: list[str] = []
    if metadata["instruction"]:
        parts.append(metadata["instruction"].strip())
    elif metadata["module_docstring"]:
        parts.append(metadata["module_docstring"].strip())
    else:
        parts.append(
            f"({metadata['description'] or 'Skill'} - exported from Castor. "
            "Original `.py` is in `scripts/` for reference.)"
        )

    tools = metadata.get("tools") or []
    if tools:
        parts.append("\n## Tools exposed\n")
        for t in tools:
            try:
                fn = t.get("function", {})
                name = fn.get("name", "?")
                desc = fn.get("description", "")
                parts.append(f"- **`{name}`** - {desc}")
            except (TypeError, AttributeError):
                continue
    return "\n".join(parts).strip() + "\n"


def export_skill(skill_py_path: Path, output_dir: Path,
                 *, overwrite: bool = False) -> dict:
    """Export a Castor `.py` skill into an agentskills.io-format bundle.

    Args:
      skill_py_path: path to the source `.py`
      output_dir: where to write the bundle (a subdirectory named
                  after the slugified skill name is created here)
      overwrite: if True, replace an existing bundle directory; otherwise
                 raises SkillExportError(``code="exists"``)

    Returns a dict with the export result:

        {
          "name": "weather",                # slugified
          "bundle_dir": "/path/to/weather/",
          "skill_md": "/path/to/weather/SKILL.md",
          "script_path": "/path/to/weather/scripts/weather.py",
          "files_written": N,
          "size_bytes": N,
        }

    Never deletes or modifies the source `.py`.
    """
    skill_py_path = Path(skill_py_path)
    output_dir = Path(output_dir)
    if not skill_py_path.is_file():
        raise SkillExportError(f"source not found: {skill_py_path}",
                               code="not_found")
    if not skill_py_path.suffix == ".py":
        raise SkillExportError(
            f"source must be a .py file, got {skill_py_path.suffix!r}",
            code="bad_source")

    # Skill name is the file stem (e.g. weather.py -> weather)
    raw_name = skill_py_path.stem
    _validate_skill_name(raw_name)
    slug = _slugify(raw_name)

    metadata = _extract_skill_metadata(skill_py_path)

    bundle_dir = output_dir / slug
    if bundle_dir.exists() and not overwrite:
        raise SkillExportError(
            f"bundle directory already exists: {bundle_dir} "
            f"(pass overwrite=True to replace)",
            code="exists")

    bundle_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = bundle_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)

    # 1. SKILL.md
    frontmatter = _build_frontmatter(slug, metadata["description"])
    body = _build_body(metadata)
    skill_md_path = bundle_dir / "SKILL.md"
    skill_md_path.write_text(f"{frontmatter}\n\n{body}", encoding="utf-8")

    # 2. scripts/<slug>.py - original source preserved verbatim
    script_path = scripts_dir / f"{slug}.py"
    script_path.write_text(
        skill_py_path.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. Optional: references/CASTOR_TOOLS.md - the TOOLS schema in
    # readable form. Skipped if no tools.
    files_written = 2
    if metadata["tools"]:
        refs_dir = bundle_dir / "references"
        refs_dir.mkdir(exist_ok=True)
        tools_md = ["# Castor TOOLS schema\n"]
        for t in metadata["tools"]:
            try:
                fn = t.get("function", {})
                tools_md.append(f"## `{fn.get('name', '?')}`\n")
                tools_md.append(f"{fn.get('description', '')}\n")
                params = fn.get("parameters", {})
                if params.get("properties"):
                    tools_md.append("\n**Parameters:**\n")
                    for pname, pinfo in params["properties"].items():
                        ptype = pinfo.get("type", "any")
                        pdesc = pinfo.get("description", "")
                        required = pname in params.get("required", [])
                        marker = " *(required)*" if required else ""
                        tools_md.append(f"- `{pname}` ({ptype}){marker} - {pdesc}")
                tools_md.append("\n")
            except (TypeError, AttributeError):
                continue
        (refs_dir / "CASTOR_TOOLS.md").write_text(
            "\n".join(tools_md), encoding="utf-8")
        files_written = 3

    # Total bundle size
    size_bytes = sum(
        f.stat().st_size for f in bundle_dir.rglob("*") if f.is_file()
    )

    return {
        "name": slug,
        "bundle_dir": str(bundle_dir),
        "skill_md": str(skill_md_path),
        "script_path": str(script_path),
        "files_written": files_written,
        "size_bytes": size_bytes,
    }


def export_skill_to_zip(skill_py_path: Path, output_zip: Path,
                        *, overwrite: bool = False) -> dict:
    """Export and zip the bundle in one step - useful for "Download as
    SKILL.zip" UI flows.

    Internally exports to a temp dir, zips it, then cleans up.
    """
    import tempfile
    import zipfile

    output_zip = Path(output_zip)
    if output_zip.exists() and not overwrite:
        raise SkillExportError(
            f"output zip already exists: {output_zip}", code="exists")

    with tempfile.TemporaryDirectory(prefix="castor_export_") as tmp:
        info = export_skill(skill_py_path, Path(tmp), overwrite=True)
        bundle_dir = Path(info["bundle_dir"])
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in bundle_dir.rglob("*"):
                if f.is_file():
                    arcname = f.relative_to(bundle_dir.parent)
                    zf.write(f, arcname)
        info["zip_path"] = str(output_zip)
        info["zip_size_bytes"] = output_zip.stat().st_size
        # bundle_dir is gone after this block; replace with zip info
        info.pop("bundle_dir", None)
        # Also drop the temp paths since they don't exist anymore
        info.pop("skill_md", None)
        info.pop("script_path", None)
    return info
