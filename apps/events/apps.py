from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    label = "events"

    def ready(self) -> None:
        # Fill in the `apps.core` hook registries. Without this, every
        # `error_hook.record_error(...)` and `audit_hook.record(...)` call
        # site in the framework is a no-op — which is how they all shipped
        # until now, because the apps upstream expected to do this
        # (`apps.observability`, `apps.audit`) were never vendored.
        #
        # Imported inside ready(), not at module scope: apps.py is
        # executed during app-registry population, before models are
        # loadable.
        from apps.core import audit_hook, error_hook
        from apps.events import sinks

        error_hook.register(sinks.record_error)
        audit_hook.register(sinks.record_audit)
