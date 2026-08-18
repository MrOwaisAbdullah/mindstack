"""The circuit-breaker / retry / timeout toolkit is gone.

`apps.core.resilience` shipped a `CircuitBreaker` state machine, a
`@retry` decorator and a `with_timeout` context manager, plus a
module-level `_BREAKERS` registry whose docstring explained that
`/readyz` would list every named breaker and that
`apps.observability.health` would read it through `iter_breakers()`.

None of that exists here. `apps.observability` was never vendored,
`/readyz` returns no breaker state, and nothing in this repo ever
constructed a breaker, decorated anything with `@retry`, or entered
`with_timeout` — so the registry had no registrant and the breakers had
no call sites. The real resilience decisions in this project are
explicit and local: `IGNORE_EXCEPTIONS` on the cache backend, the
fall-through-to-DB reads in `endpoint_gating` and
`runtime_setting_store`, `asyncio.wait_for` in the SDK runner, and the
degraded-mode mapping in `stream_agent`.

Structural, like its sibling guardrails: an unused toolkit passes every
behavioural test by construction, so the only thing worth asserting is
that it is not here and did not come back under another name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_module_is_gone() -> None:
    assert not (REPO_ROOT / "apps/core/resilience.py").exists()
    with pytest.raises(ImportError):
        import apps.core.resilience  # noqa: F401


def test_nothing_imports_it() -> None:
    hits = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "apps").rglob("*.py")
        # Production code only — this file names the path it forbids.
        if "tests" not in path.parts and "core.resilience" in path.read_text(encoding="utf-8")
    ]
    assert not hits, f"these still import the deleted module: {hits}"


def test_its_primitives_did_not_reappear_elsewhere() -> None:
    """Deleting a module is not the point; not carrying an unexercised
    breaker registry is. These names had zero call sites when it went."""
    forbidden = ("CircuitBreakerOpen", "iter_breakers", "record_breaker_open")
    hits = []
    for path in (REPO_ROOT / "apps").rglob("*.py"):
        if "tests" in path.parts:  # this file names the strings it forbids
            continue
        text = path.read_text(encoding="utf-8")
        hits += [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{name}"
            for name in forbidden
            if name in text
        ]
    assert not hits, f"the resilience toolkit came back: {hits}"


def test_readyz_still_answers() -> None:
    """It advertised itself as feeding `/readyz`. It did not, and the
    probe has to keep working without it."""
    from django.test import Client

    response = Client().get("/readyz")

    assert response.status_code in (200, 503), response.status_code
    assert b"breaker" not in response.content.lower()
