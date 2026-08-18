"""Test helper for invoking endpoints in-process.

`call_endpoint` constructs a `Ctx` and runs an `Endpoint` subclass without
spinning up Django's test client or the FastMCP subprocess. This is the
primary unit-test entry point for endpoint authors:

    from apps.core.testing import call_endpoint
    from apps.app_endpoints.hello.endpoints import Hello

    out = await call_endpoint(Hello, {"name": "test"})
    assert out.greeting == "Hello, test"

For HTTP-level testing (status codes, headers, OpenAPI), use Django's
async test client against `/api/<version>/<slug>` directly.
"""
from __future__ import annotations

import uuid
from typing import Any, TYPE_CHECKING

from apps.core.ctx import build_ctx
from apps.core.registry import Endpoint

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


async def call_endpoint(
    ep_cls: type[Endpoint],
    input_dict: dict[str, Any] | None = None,
    *,
    user: "AbstractBaseUser | None" = None,
    credential: Any = None,
    request_id: str | None = None,
) -> Any:
    """Validate `input_dict`, run the endpoint, return the typed `Output` instance.

    Raises pydantic `ValidationError` if `input_dict` doesn't fit the
    endpoint's `Input` model. Anything `run()` raises propagates — tests
    can `pytest.raises(...)` on it.
    """
    inp = ep_cls.Input.model_validate(input_dict or {})
    ctx = build_ctx(
        request_id=request_id or uuid.uuid4().hex,
        source="test",
        user=user,
        credential=credential,
    )
    return await ep_cls().run(inp, ctx)
