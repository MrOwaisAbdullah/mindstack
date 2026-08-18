"""A cancelled request must not squat on its Idempotency-Key for 24h.

`CancelledError` is a `BaseException`: a client disconnect or a
`docker compose up -d` mid-call escaped `_dispatch`'s `except Exception`
handlers, so the just-claimed `IdempotentRequest` row was never
completed OR released. Every retry with the same key then answered 409
`idempotency_request_in_flight` — for a request that was not in flight
— until the daily purge aged the row out.

Driven with `async_to_sync`, never `asyncio.run` (the documented
gotcha: `asyncio.run` puts DB calls on a foreign thread with its own
connection and the rows leak into later tests).
"""
from __future__ import annotations

import asyncio

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import RequestFactory
from pydantic import BaseModel

from apps.core import idempotency
from apps.core.models import IdempotentRequest
from apps.core.registry import Endpoint, EndpointSpec
from apps.core.rest import make_endpoint_view
from apps.mind import consumers

pytestmark = pytest.mark.django_db

KEY = "cancel-me-once"


def test_a_cancelled_request_releases_its_idempotency_claim():
    user = User.objects.create_user("cancel-op", password="x" * 20, is_staff=True)
    minted = consumers.create(
        user, name="cancel-key", max_visibility="public", rate_limit_per_min=60
    )

    gate: dict = {}

    class Hang(Endpoint):
        class Input(BaseModel):
            pass

        class Output(BaseModel):
            ok: bool = True

        async def run(self, inp, ctx):
            gate["entered"].set()
            await asyncio.Event().wait()  # parks forever; only cancel ends it

    spec = EndpointSpec(
        slug="hang-forever",
        version="v1",
        cls=Hang,
        input_model=Hang.Input,
        output_model=Hang.Output,
    )
    view = make_endpoint_view(spec)
    request = RequestFactory().post(
        "/api/v1/hang-forever",
        data="{}",
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {minted.secret}",
        HTTP_IDEMPOTENCY_KEY=KEY,
    )

    async def drive():
        gate["entered"] = asyncio.Event()
        task = asyncio.create_task(view(request))
        # Wait until execution is INSIDE run() — the claim precedes it,
        # so this is the exact mid-flight state a disconnect hits.
        await asyncio.wait_for(gate["entered"].wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async_to_sync(drive)()

    assert not IdempotentRequest.objects.filter(key=KEY).exists(), (
        "the Pending row survived the cancellation — every retry with "
        "this key now 409s as 'in flight' until the 24h purge"
    )

    # The operator story, spelled out: the retry is a fresh execution,
    # not a 409.
    outcome = idempotency.process_request(
        key=KEY, user=user, method="POST", path="/api/v1/hang-forever", body=b"{}"
    )
    assert isinstance(outcome, idempotency.Pending)
