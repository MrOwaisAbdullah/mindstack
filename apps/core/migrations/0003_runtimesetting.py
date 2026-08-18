"""RuntimeSetting — durable store for operator-flippable runtime flags.

Moves `maintenance_enabled`, `maintenance_message`, `billing_mode`,
`admin_ip_allowlist`, and `audit_retention_days` from Redis-only into
Postgres, with Redis kept as a read-through cache. See the model
docstring for the why.

This migration also runs a best-effort data backfill: any value that
was previously stored in Redis (the old call site) is copied into the
new DB row so existing deployments don't lose operator-chosen
settings on first deploy. The backfill is wrapped in try/except —
fresh installs and Redis-down boots just produce no rows, and the
service layer's env-var fallback handles that case the same way it
did pre-migration.
"""
from django.conf import settings as dj_settings
from django.db import migrations, models


# (db_key, old_cache_key) pairs. The old keys are what the pre-refactor
# `cache.set(...)` calls used; the data step reads each one and seeds
# a RuntimeSetting row if present. Keep these literals — don't import
# from the service modules because module-import order during migration
# isn't guaranteed.
_LEGACY_KEY_MAP: list[tuple[str, str]] = [
    ("billing_mode", "billing:mode"),
    ("maintenance_enabled", "maintenance:enabled"),
    ("maintenance_message", "maintenance:message"),
    ("admin_ip_allowlist", "settings:admin_ip_allowlist"),
    ("audit_retention_days", "settings:audit_retention_days"),
]


def _seed_from_cache(apps, schema_editor):
    """Copy any pre-existing Redis values into RuntimeSetting rows.

    Best-effort — a Redis outage or empty cache produces zero rows,
    same effective behavior as a fresh install. The service layer
    falls through to env-var defaults when the row is absent.
    """
    RuntimeSetting = apps.get_model("core", "RuntimeSetting")
    try:
        from django.core.cache import cache
    except Exception:
        return

    for db_key, cache_key in _LEGACY_KEY_MAP:
        try:
            v = cache.get(cache_key)
        except Exception:
            continue
        if v is None or v == "":
            continue
        if isinstance(v, bytes):
            try:
                v = v.decode("utf-8")
            except UnicodeDecodeError:
                continue
        RuntimeSetting.objects.update_or_create(
            key=db_key, defaults={"value": str(v)}
        )


def _noop_reverse(apps, schema_editor):
    """Reversing this migration only drops the table; the data step
    doesn't need explicit cleanup because the table goes away with it."""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_endpointflag"),
        migrations.swappable_dependency(dj_settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RuntimeSetting",
            fields=[
                (
                    "key",
                    models.CharField(
                        max_length=64, primary_key=True, serialize=False
                    ),
                ),
                ("value", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="+",
                        to=dj_settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "core_runtime_setting",
                "ordering": ["key"],
            },
        ),
        migrations.RunPython(_seed_from_cache, _noop_reverse),
    ]
