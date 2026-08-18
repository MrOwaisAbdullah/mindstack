"""Add `admin_only` to EndpointFlag — per-endpoint hide-from-non-staff gate.

Sibling to the existing `disabled` flag. When set, the endpoint is hidden
from non-staff users across /docs/, the playground, the MCP tool list,
_catalog, and _openapi.json, and a non-staff caller gets 404 on REST/MCP.
Operators flip it from the admin Endpoints page; reads go through the same
Redis-cached path as `disabled`.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_runtimesetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="endpointflag",
            name="admin_only",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text=(
                    "When True the endpoint is hidden from non-staff users in "
                    "/docs/, the playground, the MCP tool list, _catalog, and "
                    "_openapi.json, and a non-staff caller gets 404 on REST/MCP. "
                    "Staff (is_staff=True) still see and can call it — ship an "
                    "endpoint dark, flip it public when ready. Independent of "
                    "`disabled`; both gates apply."
                ),
            ),
        ),
    ]
