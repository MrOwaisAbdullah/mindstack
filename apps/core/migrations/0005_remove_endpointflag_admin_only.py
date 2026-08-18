"""Drop `admin_only` from EndpointFlag — the gate could never fire.

0004 added it as a hide-from-non-staff toggle inherited from the
multi-tenant template it was vendored out of. This is a single-operator
product: `/setup/` sets `is_staff=True` on the one account, and every
credential — API key, connector URL token — resolves to that account. So
`if not principal.user.is_staff` was False on every request that ever
reached it, on both the REST and MCP paths.

It was worse than a no-op: `/ops/` offered no way to set it, the docs
page rendered a banner explaining an enforcement that did not happen, and
`_catalog` / `_openapi.json` kept a per-requester body cache to support
filtering that never filtered. Removed rather than re-pointed at some
other identity axis, because "ship an endpoint dark" has no audience when
the only consumer of the API is the operator's own agents. `disabled`
(503 + Retry-After) survives as the runtime knob.

Data loss on apply is nil in practice: nothing could write the column.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_endpointflag_admin_only"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="endpointflag",
            name="admin_only",
        ),
    ]
