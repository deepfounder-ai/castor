"""Shared secret-redaction utility.

Castor's :mod:`memory` module has scrubbed common API-key shapes (sk-,
sk-ant-, github_pat_, ghp_, gsk_, AKIA, xox-, JWT) and dotenv-style
``FOO_API_KEY=...`` lines on every ``memory.save()`` call since v0.17.18.

Those patterns lived inside ``memory.py``, so the goal runtime that
landed in v0.22 (with the brand-new ``goal_facts`` / ``goal_events`` /
``goal_checkpoints`` storage paths) never benefited from them.

A real test surfaced the gap: a LinkedIn goal called
``fact_save("linkedin_password", "Qwerty446148044")`` and the plaintext
landed in three places — ``goal_facts``, the ``subagent_dispatched``
``goal_events`` payload, and every compressed ``goal_checkpoints``
messages blob (rounds 39, 42, 45 all contained the password).

This module is the canonical home for the redaction patterns. ``memory``
keeps its existing API (a thin wrapper here). ``db.fact_save`` /
``db.log_goal_event`` / ``db.save_checkpoint`` all now go through
:func:`scrub_text` so a fresh failure mode can't bypass the contract.

Adding a new key shape: extend ``_PATTERNS`` (and keep
``tests/test_secret_scrub.py`` as the audit trail).
"""
from __future__ import annotations

import re

# ── Provider-specific API key shapes ────────────────────────────────────────
# Ordered: most-specific prefixes first (sk-ant- before sk-, github_pat_
# before ghp_) so generic patterns don't eat the labels off specific ones.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{30,}"), "anthropic_key"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "github_pat"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "github_token"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "groq_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack_token"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "jwt"),
    # Generic sk- last so it doesn't swallow the more specific sk-ant- above.
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "openai_key"),
]

# Dotenv-style KEY=value (FOO_API_KEY=..., LINKEDIN_PASSWORD=...).
# Matches a label that ends in KEY/TOKEN/SECRET/PASSWORD/PASS (any case),
# captures it for traceability, redacts only the value.
_ENV_LINE_RE = re.compile(
    r"^([A-Z_][A-Z0-9_]{2,}_(?:KEY|TOKEN|SECRET|PASSWORD|PASS))(\s*=\s*)(.+)$",
    re.MULTILINE,
)

# Free-form "Password: hunter2" / "API Key = hunter2" style in prose
# (subagent prompts, fact values, log messages). Catches plain credentials
# that don't match any provider-specific shape.
#
# Value capture stops at typical JSON / sentence delimiters
# (``,"';)]}>`` + whitespace) so substituting inside a JSON blob doesn't
# eat the closing quote and corrupt the document.
#
# ``auth`` deliberately requires the full word ``authorization`` so we
# don't catch JWT-prefixed lines like ``auth: eyJ...`` and clobber the
# specific JWT label set by the pattern pass.
_INLINE_CRED_RE = re.compile(
    r"(?i)\b("
    r"password|passwd|pwd|api[_ -]?key|secret|token|bearer|"
    r"authorization|credential|access[_ -]?key"
    r")\b"
    r"\s*[:=]\s*"
    r"([^\s\"',;)\]}>]+)",
)

# Natural-language form: "password ... with: hunter2", "credential ... is
# X", "use password X". Catches the dispatch-prompt pattern that leaked
# in g_56532b01eb544616 ("Fill in the password field (#password) with:
# Qwerty446148044"). The keyword and value can have 0-40 chars of filler
# between them so prose like "fill in the password field with:" still
# triggers, but a long unrelated paragraph that happens to mention
# "password" does not.
_NL_CRED_RE = re.compile(
    r"(?i)\b("
    r"password|passwd|pwd|api[_ -]?key|secret|access[_ -]?token|"
    r"authorization|credential"
    r")\b"
    r"[^\n]{0,40}?"      # short filler — same line only
    r"\b(?:with|is|=|:)\s*"
    # Value: non-whitespace, no quotes, no JSON delimiters, no `:`/`=`/`/`
    # so the value capture starts AFTER the separator instead of swallowing
    # it (regression caught in test_scrub_text_redacts_natural_language_form).
    r"([^\s\"',;)\]}>:=/]+)",
)

# Key-name heuristic — when a (key, value) pair has a key that *names*
# itself as a secret, the value is treated as a secret no matter what
# shape it has. Cases like the LinkedIn one in v0.23.x:
#   fact_save("linkedin_password", "Qwerty446148044")
# would otherwise slip through every pattern above. The heuristic only
# applies to functions that take a KEY argument (``scrub_fact``); free
# text via :func:`scrub_text` doesn't have a key to inspect.
_SECRET_KEY_NAME_RE = re.compile(
    r"(?i)(?:^|[_-])"
    r"(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|token|bearer|session|cookie|credential|auth)"
    r"(?:[_-]|$)",
)


def scrub_text(text: str) -> tuple[str, bool]:
    """Strip known secret shapes from arbitrary text.

    Returns ``(scrubbed_text, was_scrubbed)``. Matches are replaced with
    ``[REDACTED:<type>]`` (or ``[REDACTED]`` for env-style lines, which
    keep the variable name so the redaction stays auditable).

    Empty / None text returns ``("", False)`` — never raises.
    """
    if not text:
        return text or "", False
    scrubbed = text
    hit = False

    for pat, label in _PATTERNS:
        new_text, n = pat.subn(f"[REDACTED:{label}]", scrubbed)
        if n:
            hit = True
            scrubbed = new_text

    def _env_sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}[REDACTED]"

    new_text, n = _ENV_LINE_RE.subn(_env_sub, scrubbed)
    if n:
        hit = True
        scrubbed = new_text

    def _inline_sub(m: re.Match[str]) -> str:
        # Don't re-redact a value that an earlier pattern already labeled
        # (e.g. ``auth: [REDACTED:jwt]`` should keep its specific label).
        if m.group(2).startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group(1)}: [REDACTED]"

    new_text, n = _INLINE_CRED_RE.subn(_inline_sub, scrubbed)
    if n:
        # Count as a hit only if the substitution actually changed text.
        if new_text != scrubbed:
            hit = True
            scrubbed = new_text

    # Natural-language form (catches "password field with: <value>",
    # "credential is X", etc.). Runs LAST so the more specific patterns
    # above get first crack at the text.
    def _nl_sub(m: re.Match[str]) -> str:
        if m.group(2).startswith("[REDACTED"):
            return m.group(0)
        # Replace only the value, keep the surrounding context.
        full = m.group(0)
        val = m.group(2)
        return full[: full.rfind(val)] + "[REDACTED]"

    new_text, n = _NL_CRED_RE.subn(_nl_sub, scrubbed)
    if n and new_text != scrubbed:
        hit = True
        scrubbed = new_text

    return scrubbed, hit


def scrub_fact(key: str, value: str) -> tuple[str, bool]:
    """Scrub a single (key, value) fact pair.

    Two layers of protection:

    1. If the KEY name self-identifies as a secret
       (``linkedin_password``, ``api_key``, ``access_token``, ...), the
       value is fully redacted regardless of its shape. Catches plain
       string passwords that don't match any provider regex.

    2. Otherwise the value runs through the generic :func:`scrub_text`
       pattern set so API keys / JWTs embedded in larger strings still
       get redacted.
    """
    if value is None:
        return value, False
    if not isinstance(value, str):
        # Defensive — db.fact_save coerces to str, but keep the contract
        # explicit for callers that might pass a dict/list/json fragment.
        value = str(value)
    if key and _SECRET_KEY_NAME_RE.search(key):
        return "[REDACTED:keyed_as_secret]", True
    return scrub_text(value)


__all__ = [
    "scrub_text",
    "scrub_fact",
]
