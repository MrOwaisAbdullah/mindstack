"""The job-queue and bucket-provider registries are gone.

Two more registries with no registrant, both inherited from the
template:

`apps.core.jobs_hook` backed `ctx.enqueue` / `ctx.aenqueue` /
`ctx.report_progress` / `ctx.defer_to_webhook`, which returned a
`JobHandle` the caller was meant to poll at `/api/v1/_jobs/<id>`. That
route was never mounted, no endpoint here ever called any of it, and
real background work in this project goes straight to
`django_q.tasks.async_task`. Its two failure messages told the operator
to "run `make worker`" — there is no Makefile.

`lockout.consume` + `register_bucket_provider` dispatched to
`apps.rate_limit`, also not vendored. Zero callers: the lockout paths
that are live (`is_locked`, `record_failure`, and the four
API-key/URL-token prefix helpers) need no provider. Called, it logged
"failing open" and allowed the request.

Structural, for the same reason as the sibling guardrails: an
unregistered registry passes every behavioural test by definition.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_jobs_hook_module_is_gone() -> None:
    assert not (REPO_ROOT / "apps/core/jobs_hook.py").exists()
    with pytest.raises(ImportError):
        import apps.core.jobs_hook  # noqa: F401


def test_ctx_exposes_no_job_surface() -> None:
    from apps.core.ctx import Ctx, build_ctx

    ctx = build_ctx(request_id="r", source="test")
    for gone in ("enqueue", "aenqueue", "report_progress", "defer_to_webhook"):
        assert not hasattr(ctx, gone), (
            f"Ctx.{gone} is back. It dispatches to a registry nothing "
            "registers against; real background work uses "
            "django_q.tasks.async_task directly."
        )
    # `.trace` is the surviving Ctx extra and must not have been swept up.
    assert hasattr(ctx, "trace")
    assert hasattr(Ctx, "trace")


def test_job_handle_is_gone() -> None:
    import apps.core.ctx as ctx_module

    assert not hasattr(ctx_module, "JobHandle")


def test_endpoint_spec_has_no_async_timeout() -> None:
    """`async_timeout_seconds` existed only to reach `async_task`'s
    q_options through the enqueue chain."""
    import inspect

    from apps.core.registry import EndpointSpec, endpoint

    assert "async_timeout_seconds" not in EndpointSpec.__dataclass_fields__
    assert "async_timeout_seconds" not in inspect.signature(endpoint).parameters


def test_lockout_consume_and_bucket_provider_are_gone() -> None:
    from apps.core import security
    from apps.core.security import lockout

    for gone in ("consume", "register_bucket_provider", "_bucket_provider"):
        assert not hasattr(lockout, gone), f"lockout.{gone} is back"
    assert not hasattr(security, "consume")
    assert "consume" not in security.__all__
    assert "consume" not in lockout.__all__


def test_the_live_lockout_surface_survived() -> None:
    """The removal above must not have touched the brute-force defence
    that REST, the MCP proxy, admin login and the honeypot all use."""
    from apps.core.security import lockout

    for kept in (
        "is_locked",
        "record_failure",
        "clear_failures",
        "force_unlock",
        "is_token_locked",
        "record_api_key_fail",
        "clear_api_key_fail",
        "is_url_token_locked",
        "record_url_token_fail",
        "clear_url_token_fail",
    ):
        assert hasattr(lockout, kept), f"lockout.{kept} went missing"


def test_no_make_worker_instructions_remain() -> None:
    """Both jobs_hook RuntimeErrors told the operator to run
    `make worker`. There is no Makefile in this repo."""
    assert not (REPO_ROOT / "Makefile").exists()
    hits = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "apps").rglob("*.py")
        # Production code only — this file names the string it forbids.
        if "tests" not in path.parts and "make worker" in path.read_text(encoding="utf-8")
    ]
    assert not hits, f"these still tell the operator to run `make worker`: {hits}"
