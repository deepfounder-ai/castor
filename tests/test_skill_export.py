"""Export Castor .py skills into agentskills.io SKILL.md format.

Companion to test_skill_import.py — this side of the round-trip.

Tests cover:
  - SKILL.md generation: frontmatter shape, body composition
  - Bundle directory layout (SKILL.md + scripts/ + references/)
  - AST-based metadata extraction (no skill-execution side-effects)
  - Slug conversion (Castor snake_case → agentskills hyphen-case)
  - Reserved/invalid name rejection
  - Overwrite guard
  - Optional zip output
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from skills import skill_export


# ── helpers ─────────────────────────────────────────────────────────────────


def _write_test_skill(tmp_path: Path, name: str = "weather",
                      *, with_tools: bool = True,
                      with_instruction: bool = True,
                      description: str = "Get current weather") -> Path:
    """Write a fake Castor .py skill to tmp_path/<name>.py and return its path."""
    parts = [f'"""{name.title()} skill module docstring."""', "", ""]
    parts.append(f'DESCRIPTION = "{description}"')
    if with_instruction:
        parts.append('INSTRUCTION = "Use get_weather(city) to get current conditions."')
    if with_tools:
        parts.append(
            "TOOLS = [\n"
            '    {"type": "function", "function": {\n'
            '        "name": "get_weather",\n'
            '        "description": "Fetch weather for a city",\n'
            '        "parameters": {\n'
            '            "type": "object",\n'
            '            "properties": {\n'
            '                "city": {"type": "string", "description": "City name"}\n'
            '            },\n'
            '            "required": ["city"]\n'
            "        }\n"
            "    }}\n"
            "]"
        )
    parts.append("")
    parts.append("def execute(name, args):")
    parts.append('    return "stub"')
    src = "\n".join(parts)
    path = tmp_path / f"{name}.py"
    path.write_text(src, encoding="utf-8")
    return path


# ── _slugify ────────────────────────────────────────────────────────────────


def test_slugify_snake_case_to_hyphen():
    assert skill_export._slugify("my_great_skill") == "my-great-skill"


def test_slugify_already_lowercase_hyphen():
    assert skill_export._slugify("lead-gen") == "lead-gen"


def test_slugify_strips_uppercase_and_punctuation():
    assert skill_export._slugify("LinkedIn Lead-Gen!") == "linkedin-lead-gen"


def test_slugify_collapses_runs_of_hyphens():
    assert skill_export._slugify("foo___---bar") == "foo-bar"


def test_slugify_strips_leading_trailing_hyphens():
    assert skill_export._slugify("--foo--") == "foo"


def test_slugify_empty_fallback():
    """Pathological inputs that strip to nothing get a stable fallback."""
    assert skill_export._slugify("***") == "unnamed-skill"
    assert skill_export._slugify("") == "unnamed-skill"


# ── _validate_skill_name ────────────────────────────────────────────────────


def test_validate_skill_name_accepts_normal():
    skill_export._validate_skill_name("weather")
    skill_export._validate_skill_name("my_skill")


def test_validate_skill_name_rejects_empty():
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export._validate_skill_name("")
    assert e.value.code == "bad_name"


def test_validate_skill_name_rejects_too_long():
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export._validate_skill_name("a" * 65)
    assert e.value.code == "bad_name"


def test_validate_skill_name_rejects_reserved():
    for bad in ("__init__", "skill_creator", "skill_import", "skill_export"):
        with pytest.raises(skill_export.SkillExportError) as e:
            skill_export._validate_skill_name(bad)
        assert e.value.code == "reserved_name"


# ── _extract_skill_metadata ─────────────────────────────────────────────────


def test_extract_metadata_pulls_description_instruction_and_tools(tmp_path):
    p = _write_test_skill(tmp_path)
    meta = skill_export._extract_skill_metadata(p)
    assert meta["description"] == "Get current weather"
    assert "get_weather" in meta["instruction"]
    assert len(meta["tools"]) == 1
    assert meta["tools"][0]["function"]["name"] == "get_weather"
    assert "Weather skill module" in meta["module_docstring"]


def test_extract_metadata_handles_missing_instruction(tmp_path):
    p = _write_test_skill(tmp_path, with_instruction=False)
    meta = skill_export._extract_skill_metadata(p)
    assert meta["instruction"] == ""
    # Description + tools should still be present
    assert meta["description"]
    assert meta["tools"]


def test_extract_metadata_handles_missing_tools(tmp_path):
    p = _write_test_skill(tmp_path, with_tools=False)
    meta = skill_export._extract_skill_metadata(p)
    assert meta["tools"] == []


def test_extract_metadata_does_NOT_execute_skill(tmp_path):
    """Skill source could have arbitrary side-effects at import time (open
    files, network, etc.). Our extractor uses ast.parse, NEVER importlib,
    so a destructive top-level statement is harmless.
    """
    sneaky = tmp_path / "evil.py"
    sneaky.write_text(
        '"""evil skill"""\n'
        'DESCRIPTION = "supposedly harmless"\n'
        'TOOLS = []\n'
        '\n'
        'import sys\n'
        # Real exploit attempt would be `os.unlink(...)`; we just write a
        # canary file. If extract executed the source, this file appears.
        f'open({str(tmp_path / "canary")!r}, "w").write("boom")\n',
        encoding="utf-8",
    )
    skill_export._extract_skill_metadata(sneaky)
    assert not (tmp_path / "canary").exists()


def test_extract_metadata_rejects_missing_file(tmp_path):
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export._extract_skill_metadata(tmp_path / "nonexistent.py")
    assert e.value.code == "not_found"


def test_extract_metadata_rejects_syntax_error(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("def execute(:\n    not python", encoding="utf-8")
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export._extract_skill_metadata(p)
    assert e.value.code == "syntax_error"


# ── _build_frontmatter ──────────────────────────────────────────────────────


def test_build_frontmatter_has_required_fields():
    fm = skill_export._build_frontmatter("weather", "Get the weather")
    assert fm.startswith("---\n")
    assert fm.endswith("---")
    assert "name: weather" in fm
    assert "description: Get the weather" in fm
    assert "source: castor" in fm
    assert "exported_at:" in fm


def test_build_frontmatter_quotes_descriptions_with_special_chars():
    """If description has YAML-significant chars, JSON-style quote it."""
    fm = skill_export._build_frontmatter("foo", 'with "quotes" and: colons')
    # JSON-quoted form
    assert 'description: "with \\"quotes\\" and: colons"' in fm


def test_build_frontmatter_slugifies_name():
    fm = skill_export._build_frontmatter("My_Skill", "desc")
    assert "name: my-skill" in fm


# ── export_skill (main entry point) ─────────────────────────────────────────


def test_export_skill_produces_expected_layout(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    out = tmp_path / "exports"
    result = skill_export.export_skill(src, out)

    bundle = Path(result["bundle_dir"])
    assert bundle.exists()
    assert bundle.name == "weather"  # slug == raw name here
    assert (bundle / "SKILL.md").is_file()
    assert (bundle / "scripts" / "weather.py").is_file()
    # Tools were present → references/CASTOR_TOOLS.md should exist
    assert (bundle / "references" / "CASTOR_TOOLS.md").is_file()
    assert result["files_written"] == 3
    assert result["size_bytes"] > 0


def test_export_skill_skips_references_when_no_tools(tmp_path):
    src = _write_test_skill(tmp_path, "no-tools-skill", with_tools=False)
    out = tmp_path / "exports"
    result = skill_export.export_skill(src, out)
    bundle = Path(result["bundle_dir"])
    assert (bundle / "SKILL.md").is_file()
    assert (bundle / "scripts" / "no-tools-skill.py").is_file()
    assert not (bundle / "references").exists()
    assert result["files_written"] == 2


def test_export_skill_preserves_source_verbatim(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    src_content = src.read_text(encoding="utf-8")
    out = tmp_path / "exports"
    result = skill_export.export_skill(src, out)
    copied = Path(result["script_path"]).read_text(encoding="utf-8")
    assert copied == src_content


def test_export_skill_skill_md_has_frontmatter_and_body(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    out = tmp_path / "exports"
    result = skill_export.export_skill(src, out)
    md = Path(result["skill_md"]).read_text(encoding="utf-8")
    # Frontmatter block
    assert md.startswith("---\n")
    assert re.search(r"^---\nname: weather\n", md)
    assert "---\n\n" in md  # blank line between frontmatter and body
    # Body contains the INSTRUCTION text
    assert "Use get_weather(city)" in md
    # Body lists the tool
    assert "get_weather" in md.split("---\n\n", 1)[1]


def test_export_skill_body_falls_back_to_docstring_when_no_instruction(
        tmp_path):
    """When INSTRUCTION is missing, body uses the module docstring."""
    src = _write_test_skill(tmp_path, "skillb", with_instruction=False)
    result = skill_export.export_skill(src, tmp_path / "exports")
    md = Path(result["skill_md"]).read_text(encoding="utf-8")
    assert "Skillb skill module docstring" in md


def test_export_skill_rejects_existing_bundle(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    out = tmp_path / "exports"
    skill_export.export_skill(src, out)
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export.export_skill(src, out)
    assert e.value.code == "exists"


def test_export_skill_overwrite_replaces_bundle(tmp_path):
    src = _write_test_skill(tmp_path, "weather", description="v1")
    out = tmp_path / "exports"
    skill_export.export_skill(src, out)

    # Modify the source and re-export with overwrite
    src.write_text(src.read_text().replace('"v1"', '"v2"'), encoding="utf-8")
    skill_export.export_skill(src, out, overwrite=True)

    md = (out / "weather" / "SKILL.md").read_text(encoding="utf-8")
    assert "description: v2" in md


def test_export_skill_rejects_non_py_source(tmp_path):
    fake = tmp_path / "notpy.md"
    fake.write_text("# not python")
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export.export_skill(fake, tmp_path / "out")
    assert e.value.code == "bad_source"


def test_export_skill_rejects_missing_source(tmp_path):
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export.export_skill(tmp_path / "ghost.py", tmp_path / "out")
    assert e.value.code == "not_found"


def test_export_skill_slugifies_name_for_bundle_dir(tmp_path):
    src = _write_test_skill(tmp_path, "linkedin_lead_gen")
    result = skill_export.export_skill(src, tmp_path / "out")
    bundle = Path(result["bundle_dir"])
    assert bundle.name == "linkedin-lead-gen"  # underscore → hyphen
    # And the inner script gets renamed to match the slug
    assert (bundle / "scripts" / "linkedin-lead-gen.py").is_file()


# ── export_skill_to_zip ─────────────────────────────────────────────────────


def test_export_skill_to_zip_produces_valid_zip(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    zip_path = tmp_path / "weather-bundle.zip"
    result = skill_export.export_skill_to_zip(src, zip_path)

    assert zip_path.is_file()
    assert result["zip_path"] == str(zip_path)
    assert result["zip_size_bytes"] > 0

    # Inspect zip contents
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "weather/SKILL.md" in names
    assert "weather/scripts/weather.py" in names
    assert "weather/references/CASTOR_TOOLS.md" in names


def test_export_skill_to_zip_rejects_existing(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    zip_path = tmp_path / "out.zip"
    zip_path.write_bytes(b"existing")
    with pytest.raises(skill_export.SkillExportError) as e:
        skill_export.export_skill_to_zip(src, zip_path)
    assert e.value.code == "exists"


def test_export_skill_to_zip_overwrite_works(tmp_path):
    src = _write_test_skill(tmp_path, "weather")
    zip_path = tmp_path / "out.zip"
    zip_path.write_bytes(b"existing")
    skill_export.export_skill_to_zip(src, zip_path, overwrite=True)
    # Confirm it's now a real zip, not the placeholder bytes
    assert zipfile.is_zipfile(zip_path)


def test_export_skill_to_zip_cleans_temp_bundle(tmp_path):
    """The temp dir used to build the bundle should NOT survive the export.
    Only the .zip + the source skill remain."""
    src = _write_test_skill(tmp_path, "weather")
    zip_path = tmp_path / "weather.zip"
    skill_export.export_skill_to_zip(src, zip_path)
    # The bundle key is removed because the dir doesn't exist after temp cleanup
    # (we replace it with zip_path in the return dict)
    # Confirm by listing tmp_path — no `weather/` subdir, just the zip + source
    entries = {p.name for p in tmp_path.iterdir()}
    assert "weather.zip" in entries
    assert "weather.py" in entries
    assert "weather" not in entries  # no leftover bundle directory
