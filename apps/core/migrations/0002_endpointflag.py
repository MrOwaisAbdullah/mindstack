"""EndpointFlag — runtime endpoint enable/disable (10.2.1.11).

DB-backed per-endpoint flag with Redis caching on the hot path.
Operators flip via the admin Endpoints page; `make_endpoint_view`
short-circuits to 503 when the row says disabled.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EndpointFlag",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("slug", models.CharField(max_length=64, unique=True)),
                ("disabled", models.BooleanField(db_index=True, default=False)),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Free-form note shown to operators on the endpoints list.",
                        max_length=200,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "core_endpoint_flag",
            },
        ),
    ]
