"""`error_hook` reaches a real sink, and the sink writes a real row.

The hook shipped registered by nobody. Every call site — the REST 500
handler, the MCP proxy's, `ctx.trace.exception`, and `/_csp-report/` —
short-circuited on `if _recorder is None: return None`, so an endpoint
crash and a browser CSP violation were both recorded precisely nowhere.

Two things have to hold and they fail independently:

1. Something calls `error_hook.register()` at boot. A registry with no
   registrant is the original bug and nothing about it is loud.
2. The registered recorder actually persists, and swallows its own
   failures — the 500 handler calls it, so a sink that raises would turn
   a handled 500 into an unhandled one.
"""
from __future__ import annotations

import pytest

from apps.core import error_hook
from apps.events import sinks
from apps.events.models import Event


def test_boot_registers_an_error_recorder() -> None:
    """`EventsConfig.ready()` ran during test setup. If this fails, the
    hook is back to being a no-op registry."""
    assert error_hook.is_enabled(), (
        "No error recorder is registered. `apps.core.error_hook` dispatches "
        "to nothing, so every 500 and every CSP report is discarded — see "
        "apps/events/apps.py."
    )


@pytest.mark.django_db
def test_record_error_writes_an_event_row() -> None:
    returned = error_hook.record_error(
        exc=ValueError("kaboom"),
        request_id="req-abc",
        source="rest",
        endpoint_slug="get-note",
        request_path="/api/v1/get-note",
        request_method="POST",
        status_code=500,
        user_id=7,
        ip="203.0.113.9",
        user_agent="curl/8.0",
        handled=False,
    )

    event = Event.objects.get(type="error")
    assert returned == str(event.pk), "the recorder must return the row id"
    assert event.details["exc_class"] == "ValueError"
    assert event.details["message"] == "kaboom"
    assert event.details["source"] == "rest"
    assert event.details["endpoint_slug"] == "get-note"
    assert event.details["path"] == "/api/v1/get-note"
    assert event.details["status"] == 500
    assert event.details["handled"] is False
    assert event.details["request_id"] == "req-abc"


@pytest.mark.django_db
def test_handled_flag_distinguishes_recovered_from_died() -> None:
    """`handled` is the first thing an operator triages by, so it has to
    survive the round-trip rather than defaulting."""
    error_hook.record_error(
        exc=RuntimeError("degraded but served"),
        source="rest",
        status_code=200,
        handled=True,
    )
    assert Event.objects.get(type="error").details["handled"] is True


@pytest.mark.django_db
def test_long_message_is_truncated_not_stored_whole() -> None:
    """An exception raised while serving a note can quote note content
    into its message, and this table is in the backup set."""
    error_hook.record_error(exc=ValueError("x" * 5000), source="rest")

    message = Event.objects.get(type="error").details["message"]
    assert len(message) <= sinks._MAX_MESSAGE
    assert message.endswith("…")


@pytest.mark.django_db
def test_no_traceback_is_persisted() -> None:
    """Deliberate: the traceback is in the application log, correlated by
    request_id. Storing it here would bloat a table a dashboard polls."""
    try:
        raise ValueError("boom")
    except ValueError as exc:
        error_hook.record_error(exc=exc, source="rest", request_id="req-1")

    details = Event.objects.get(type="error").details
    assert "traceback" not in details
    assert not any("Traceback" in str(v) for v in details.values())


def test_recorder_swallows_its_own_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No django_db mark — the DB is unavailable, which is precisely the
    condition under which a 500 handler calls in here. It must return
    None, not raise."""

    class _Boom:
        objects = None  # attribute access below raises AttributeError

    import apps.events.models as models_module

    monkeypatch.setattr(models_module, "Event", _Boom)
    assert sinks.record_error(exc=ValueError("x"), source="rest") is None


@pytest.mark.django_db
def test_csp_report_view_records_a_violation(client) -> None:
    """The end-to-end path the review called out: `/_csp-report/` accepted
    reports and dropped them."""
    response = client.post(
        "/_csp-report/",
        data=(
            '{"csp-report": {"violated-directive": "style-src-attr",'
            ' "blocked-uri": "inline", "document-uri": "/ops/feeds/"}}'
        ),
        content_type="application/csp-report",
    )

    assert response.status_code == 204
    event = Event.objects.get(type="error")
    assert event.details["exc_class"] == "CSPViolation"
    assert event.details["endpoint_slug"] == "csp_violation"
    assert "style-src-attr" in event.details["message"]
    # A violation report is not a crash — the page rendered.
    assert event.details["handled"] is True
