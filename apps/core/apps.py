from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        # every installed app may declare endpoints in an
        # `endpoints` module (or subpackage). Importing it triggers the
        # `@endpoint` decorators and populates `apps.core.registry.registry`.
        # Endpoint apps under `apps/app_endpoints/<slug>/endpoints.py` are the
        # primary consumers; framework apps may register internal endpoints
        # the same way.
        autodiscover_modules("endpoints")
        # every installed app may declare event subscribers in
        # an `events` module. Importing it triggers `@subscribe(...)` so the
        # bus knows about every handler at boot time.
        autodiscover_modules("events")
        # wire the user_login_failed signal subscriber that
        # bumps the admin-login fail counter. Importing the module is
        # enough; @receiver registers on import.
        from apps.core.security import auth_signals  # noqa: F401
