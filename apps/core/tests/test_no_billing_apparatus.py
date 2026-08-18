"""There is no billing in this product, and no code pretending there is.

The vendored template was a metered SaaS API. It brought a whole credits
apparatus with it: a charge context manager wrapped around every
`run()`, `InsufficientCreditsError` → 402 on both the REST and MCP
paths, refund-on-error, a `credits_cost` on every endpoint spec, an
`x-credits-cost` OpenAPI extension, and "N credits/call" chips in the
docs UI.

None of it ran. No endpoint declared a non-zero cost, and
`apps.core.charging` was a registry nobody registered against, so
`charge()` returned a no-op context manager on every request. What it
did do was tell anyone reading `/docs/` that calling this server costs
credits, and point them at `/dashboard/billing/` — a URL that has never
existed here.

Structural assertions, because the failure mode is a vendor refresh
bringing it back and nothing behavioural noticing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_SOURCES = [
    "apps/core/rest.py",
    "apps/core/registry.py",
    "apps/core/openapi.py",
    "apps/core/events.py",
    "apps/core/ctx.py",
    "apps/mcp_proxy/views.py",
    "apps/docs/services/catalog.py",
]

_TEMPLATES = [
    "templates/docs/index.html",
    "templates/docs/endpoint_detail.html",
    "templates/docs/_try_it.html",
]

# `credits_cost`, `credits_charged`, `charge_credits`, `charge_cm`,
# `_finalize_charge`, `insufficient_credits`. Not the word "credential",
# which shares a prefix and is everywhere.
_IDENTIFIER = re.compile(r"\bcredits?_\w+|\b\w*charge_\w+|\bcharge_cm\b")
_BACKTICKED = re.compile(r"`[^`\n]*`")


def _code_lines(text: str) -> list[str]:
    return [
        _BACKTICKED.sub("", line)
        for line in text.splitlines()
        if not line.strip().startswith("#")
    ]


@pytest.mark.parametrize("relpath", _SOURCES)
def test_no_billing_identifiers_in_the_request_pipeline(relpath: str) -> None:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} moved — update this guardrail"
    hits = [
        line
        for line in _code_lines(path.read_text(encoding="utf-8"))
        if _IDENTIFIER.search(line)
    ]
    assert not hits, (
        f"{relpath} references credit-charging again:\n  "
        + "\n  ".join(h.strip() for h in hits)
        + "\n\nThis server is not metered. See test docstring."
    )


@pytest.mark.parametrize("relpath", _TEMPLATES)
def test_docs_ui_does_not_advertise_credits(relpath: str) -> None:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert "credit" not in text.lower(), (
        f"{relpath} tells the reader that calls cost credits. Nothing "
        "charges anything, and the top-up link it implies does not exist."
    )


def test_charging_module_is_gone() -> None:
    assert not (REPO_ROOT / "apps/core/charging.py").exists()
    with pytest.raises(ImportError):
        import apps.core.charging  # noqa: F401


def test_endpoint_spec_has_no_price() -> None:
    from apps.core.registry import EndpointSpec, endpoint

    assert "credits_cost" not in EndpointSpec.__dataclass_fields__
    # The decorator too — a spec built with `credits_cost=` should be a
    # loud TypeError, not a silently-ignored kwarg.
    import inspect

    assert "credits_cost" not in inspect.signature(endpoint).parameters


def test_catalog_entries_carry_no_price() -> None:
    """`_catalog` and `_openapi.json` are published documents; a stale
    `credits_cost: 0` in them would keep implying a price list."""
    from apps.core.openapi import build_openapi
    from apps.core.registry import registry

    for spec in registry.all():
        assert "credits_cost" not in spec.to_catalog_entry()

    doc = build_openapi(version="v1")
    for path_item in doc["paths"].values():
        for operation in path_item.values():
            assert "x-credits-cost" not in operation


def test_no_402_in_the_error_contract() -> None:
    """402 is the one status in the vendored contract this server can
    never legitimately return."""
    for relpath in ("apps/core/rest.py", "apps/mcp_proxy/views.py"):
        text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        assert "status=402" not in text
        assert "insufficient_credits" not in text
