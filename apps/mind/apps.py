from django.apps import AppConfig


class MindConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mind"
    label = "mind"

    def ready(self) -> None:
        # Install the per-key throttle into core's hook registry.
        from apps.core import throttling
        from apps.mind.throttle import check

        throttling.register(check)
