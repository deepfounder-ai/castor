"""AST-level repair for ``stub-Pass + code outside branch`` LLM anti-pattern.

Closes #14. Verifies ``_fix_stub_branch_outside_code`` against the exact
buggy shapes observed in field sessions (workspace_meter,
camera_diagnostics — small local models that prompt fixes alone don't
catch).

Each test feeds a deliberately broken snippet into the fixer and asserts:
1. The output is valid Python (``ast.parse`` doesn't raise).
2. Each ``if name == "...":`` branch now contains the real impl, NOT
   the bare ``pass`` stub.
3. There are no top-level statements between dispatch branches at the
   function-body indent.

The line-based ``_fix_elif_body_indent`` is also called by the pipeline
BEFORE the AST pass. Some inputs are repaired by that pass and reach
the AST pass already clean; the AST pass is the safety net for the
shapes the regex misses.
"""
from __future__ import annotations

import ast
import textwrap

import pytest


# NB: ``from skills.skill_creator import ...`` is deliberately NOT at
# module level. skill_creator imports ``db`` / ``pricing`` /
# ``providers`` transitively; pytest collection then primes those
# modules against the unsandboxed CASTOR_DATA_DIR, which poisons later
# tests that use ``qwe_temp_data_dir`` (observed CI flake on 3.12 as
# ``no such table: kv`` in ``test_telemetry`` and ``no such table:
# skill_imports`` in ``test_skill_import``). Provide a session-scoped
# fixture that defers the import until pytest is past collection.


@pytest.fixture(scope="module")
def skc():
    """Module-scoped lazy import of skill_creator helpers. Single
    import happens after pytest collection so the DB-touching
    transitive imports don't sandbox-pollute later tests."""
    from skills import skill_creator as m
    return m


def _runs(code: str) -> ast.Module:
    """Wrap as function body + parse — same shape the production caller uses."""
    wrapped = "def execute(name, args):\n" + textwrap.indent(code, "    ")
    return ast.parse(wrapped)


def _branches_inside_body(code: str, skc) -> list[tuple[str, list[str]]]:
    """Walk the parsed body and return [(branch_constant, [stmt_types])]
    for each top-level dispatch If (including chained elifs).

    Takes ``skc`` so the helper can reach ``skill_creator``'s
    predicates without a module-level import — see fixture comment."""
    tree = _runs(code)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    out = []
    for stmt in func.body:
        if skc._is_name_dispatch_if(stmt):
            cur = stmt
            while True:
                key = cur.test.comparators[0].value
                body_kinds = [type(s).__name__ for s in cur.body]
                out.append((key, body_kinds))
                if (
                    len(cur.orelse) == 1
                    and isinstance(cur.orelse[0], ast.If)
                    and skc._is_name_dispatch_if(cur.orelse[0])
                ):
                    cur = cur.orelse[0]
                else:
                    break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests (isolated, no LLM)
# ─────────────────────────────────────────────────────────────────────────────


def test_is_name_dispatch_recognises_eq_to_name(skc):
    tree = ast.parse('if name == "foo":\n    pass\n')
    assert skc._is_name_dispatch_if(tree.body[0]) is True


def test_is_name_dispatch_rejects_non_eq(skc):
    tree = ast.parse('if name in ("foo",):\n    pass\n')
    assert skc._is_name_dispatch_if(tree.body[0]) is False


def test_is_name_dispatch_rejects_other_var(skc):
    tree = ast.parse('if tool == "foo":\n    pass\n')
    assert skc._is_name_dispatch_if(tree.body[0]) is False


def test_is_name_dispatch_rejects_compound_test(skc):
    tree = ast.parse('if name == "foo" and args:\n    pass\n')
    # Top-level is BoolOp, not Compare — should fail.
    assert skc._is_name_dispatch_if(tree.body[0]) is False


def test_last_in_elif_chain_returns_innermost(skc):
    src = (
        'if name == "a":\n    pass\n'
        'elif name == "b":\n    pass\n'
        'elif name == "c":\n    pass\n'
    )
    tree = ast.parse(src)
    last = skc._last_in_elif_chain(tree.body[0])
    assert last.test.comparators[0].value == "c"


def test_body_is_only_pass_true(skc):
    tree = ast.parse('if name == "x":\n    pass\n')
    assert skc._body_is_only_pass(tree.body[0]) is True


def test_body_is_only_pass_false_when_real_code(skc):
    tree = ast.parse('if name == "x":\n    return "ok"\n')
    assert skc._body_is_only_pass(tree.body[0]) is False


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end repair tests against issue #14 buggy shapes
# ─────────────────────────────────────────────────────────────────────────────


def test_issue14_camera_benchmark_shape(skc):
    """The exact shape from the issue description."""
    buggy = (
        'if name == "camera_benchmark":\n'
        '    pass\n'
        'import time\n'
        'samples = int(args.get("samples", 30))\n'
        'durations = []\n'
        'for _ in range(samples):\n'
        '    durations.append(0.1)\n'
        'return f"benchmark: {sum(durations):.2f}s"\n'
    )
    fixed = skc._fix_stub_branch_outside_code(buggy)
    # Parses cleanly
    ast.parse("def execute(name, args):\n" + textwrap.indent(fixed, "    "))
    # The real code now lives inside the branch
    branches = _branches_inside_body(fixed, skc)
    assert len(branches) == 1
    key, kinds = branches[0]
    assert key == "camera_benchmark"
    # No Pass remains; real statements are there
    assert "Pass" not in kinds
    assert any(k in ("Import", "Assign", "For", "Return") for k in kinds)


def test_blank_lines_between_pass_and_real_code(skc):
    """LLM puts blank lines between the stub and the stray code —
    the line-based regex sometimes loses track. AST pass shouldn't."""
    buggy = (
        'if name == "x":\n'
        '    pass\n'
        '\n'
        '\n'
        'result = 42\n'
        'return str(result)\n'
    )
    fixed = skc._fix_stub_branch_outside_code(buggy)
    branches = _branches_inside_body(fixed, skc)
    assert branches[0][0] == "x"
    assert "Pass" not in branches[0][1]
    assert "Return" in branches[0][1]


def test_chained_elif_tail_stub_pulls_into_last(skc):
    """Chained elif where ONLY the last is a stub. Stray code following
    the whole chain must land in the LAST elif's body, not earlier ones."""
    buggy = (
        'if name == "a":\n'
        '    return "a-impl"\n'
        'elif name == "b":\n'
        '    return "b-impl"\n'
        'elif name == "c":\n'
        '    pass\n'
        'rv = compute_c()\n'
        'return rv\n'
    )
    fixed = skc._fix_stub_branch_outside_code(buggy)
    branches = _branches_inside_body(fixed, skc)
    # a and b are unchanged
    assert branches[0][0] == "a"
    assert "Return" in branches[0][1]
    assert branches[1][0] == "b"
    assert "Return" in branches[1][1]
    # c now has the pulled code, no Pass
    assert branches[2][0] == "c"
    assert "Pass" not in branches[2][1]
    assert "Return" in branches[2][1]


def test_idempotent_when_already_correct(skc):
    """Well-formed input is returned unchanged (preserves comments / formatting)."""
    good = (
        'if name == "x":\n'
        '    return "x-impl"\n'
        'elif name == "y":\n'
        '    return "y-impl"\n'
        'return "unknown tool"\n'
    )
    assert skc._fix_stub_branch_outside_code(good) == good


def test_empty_input_unchanged(skc):
    assert skc._fix_stub_branch_outside_code("") == ""
    assert skc._fix_stub_branch_outside_code("   \n\n  ") == "   \n\n  "


def test_unparseable_input_returned_as_is(skc):
    """If the LLM emits genuinely broken Python the AST pass MUST NOT
    crash — let downstream syntax check report the error."""
    broken = "elif name == 'x':\n    pass\nthis is not python\n"
    # Should NOT raise; should return something (possibly the original).
    out = skc._fix_stub_branch_outside_code(broken)
    assert isinstance(out, str)


def test_does_not_pull_subsequent_dispatch_into_stub(skc):
    """Pulled code stops at the next dispatch — so a stub followed by
    another dispatch leaves the stub Pass alone (next branch's code
    isn't the stub's)."""
    buggy = (
        'if name == "x":\n'
        '    pass\n'
        'elif name == "y":\n'
        '    return "y"\n'
    )
    fixed = skc._fix_stub_branch_outside_code(buggy)
    # x stays a stub (nothing was outside to pull), y is intact
    branches = _branches_inside_body(fixed, skc)
    assert branches[0][0] == "x"
    assert branches[1][0] == "y"
    assert "Return" in branches[1][1]


# ─────────────────────────────────────────────────────────────────────────────
# Integration with the pipeline (line-fixer + AST)
# ─────────────────────────────────────────────────────────────────────────────


def test_pipeline_order_regex_then_ast_repairs_field_session_shape(skc):
    """Run the pipeline shape (empty_blocks -> elif_body_indent -> AST)
    against the field-session symptom and verify each branch has real code."""
    # Pre-pipeline raw shape: the regex's expected fix-target (pass
    # already added by an earlier pass; stray code at branch indent).
    raw = (
        'if name == "metric_capture":\n'
        '    pass\n'
        'reading = float(args["value"])\n'
        'db.execute("INSERT INTO skill_workspace_meter_metrics ...", (reading,))\n'
        'return f"recorded {reading}"\n'
        'elif name == "metric_stats":\n'
        '    pass\n'
        'rows = db.execute("SELECT ...").fetchall()\n'
        'return json.dumps({"count": len(rows)})\n'
    )
    # The second elif after non-If statements is itself a SyntaxError,
    # which the regex `_fix_elif_body_indent` cleans up by indenting
    # the stray code. Then the AST pass tidies anything left.
    step1 = skc._fix_empty_blocks(raw)
    step2 = skc._fix_elif_body_indent(step1)
    step3 = skc._fix_stub_branch_outside_code(step2)
    # End result parses
    ast.parse("def execute(name, args):\n" + textwrap.indent(step3, "    "))
    # Each branch has its own implementation
    branches = _branches_inside_body(step3, skc)
    keys = [b[0] for b in branches]
    assert "metric_capture" in keys
    assert "metric_stats" in keys
    for _, kinds in branches:
        assert "Pass" not in kinds
        assert "Return" in kinds
