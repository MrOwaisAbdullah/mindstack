"""The `admin_only` gate is gone and must not come back.

`EndpointFlag.admin_only` shipped from the vendored multi-tenant template
as "hide this endpoint from non-staff callers". Every gate that consulted
it read `principal.user.is_staff`, and `/setup/` sets `is_staff=True` on
the single account that every credential in this product resolves to — so
the branch was dead by construction while reading, in the docs UI and the
model help text, as a live feature.

The risk this pins is a vendor refresh quietly reintroducing it. That
would not fail any behavioural test: an inert gate passes everything.
So the assertions here are structural.

DB-free on purpose (CLAUDE.md: keep new tests DB-free where possible) —
these read the model's field list and the source tree, not rows.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Where an admin-only gate would have to live to do anything. Migrations
# are excluded: 0004 adds the column and 0005 drops it, and both must
# keep naming it.
_GATE_SOURCES = [
    "apps/core/endpoint_gating.py",
    "apps/core/rest.py",
    "apps/core/urls.py",
    "apps/core/openapi.py",
    "apps/mcp_proxy/views.py",
    "apps/docs/views.py",
    "apps/docs/context.py",
    "apps/docs/services/catalog.py",
]

_TEMPLATES = ["templates/docs/index.html", "templates/docs/endpoint_detail.html"]

# `admin_only` / `is_admin_only` / `admin_only_slugs` / `set_admin_only`
# as an *identifier*, not the word inside prose explaining the removal —
# those notes are load-bearing and should stay readable.
_IDENTIFIER = re.compile(r"\b(?:is_|set_)?admin_only(?:_slugs)?\b")

# Prose refers to the removed flag in backticks (`EndpointFlag.admin_only`).
# Stripping inline-code spans first is what separates "this file explains
# why the gate is gone" from "this file has the gate back".
_BACKTICKED = re.compile(r"`[^`\n]*`")


def _code_lines(text: str) -> list[str]:
    """Source lines with whole-line `#` comments and backticked prose
    spans dropped."""
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(_BACKTICKED.sub("", line))
    return out


@pytest.mark.parametrize("relpath", _GATE_SOURCES)
def test_no_admin_only_identifier_in_enforcement_sources(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} moved — update this guardrail"
    hits = [
        line
        for line in _code_lines(path.read_text(encoding="utf-8"))
        if _IDENTIFIER.search(line)
    ]
    assert not hits, (
        f"{relpath} references an `admin_only` identifier again:\n  "
        + "\n  ".join(h.strip() for h in hits)
        + "\n\nThe flag was removed before launch because it gated on "
        "`user.is_staff`, which is True for every credential this "
        "single-operator product issues. Re-adding it re-adds a gate that "
        "cannot fire. See apps/core/migrations/0005."
    )


@pytest.mark.parametrize("relpath", _TEMPLATES)
def test_no_admin_only_badge_in_docs_templates(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} moved — update this guardrail"
    text = path.read_text(encoding="utf-8")
    assert "admin_only" not in text and "admin-only" not in text, (
        f"{relpath} renders an admin-only badge/banner again. The gate "
        "behind it is gone, so the badge would promise an enforcement "
        "that does not exist."
    )


def test_endpointflag_has_no_admin_only_field() -> None:
    """The column itself. A vendor refresh that re-adds the model field
    would regenerate the gates around it."""
    from apps.core.models import EndpointFlag

    names = {f.name for f in EndpointFlag._meta.get_fields()}
    assert "admin_only" not in names, (
        "EndpointFlag.admin_only is back. It is unwritable from the ops UI "
        "and unreadable by any gate that can fire — see migration 0005."
    )
    # The surviving knob, so the removal can't be mistaken for deleting both.
    assert "disabled" in names


def test_endpoint_gating_exports_only_the_disable_surface() -> None:
    from apps.core import endpoint_gating

    assert not [n for n in endpoint_gating.__all__ if "admin" in n]
    for gone in ("is_admin_only", "set_admin_only", "admin_only_slugs"):
        assert not hasattr(endpoint_gating, gone), f"{gone} is back"
    # The disable gate is untouched by the removal.
    for kept in ("is_disabled", "set_disabled", "disabled_slugs"):
        assert hasattr(endpoint_gating, kept), f"{kept} went missing"
