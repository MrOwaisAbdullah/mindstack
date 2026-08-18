from django.apps import AppConfig


class ApiKeysConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.api_keys"
    label = "api_keys"

    def ready(self) -> None:
        # register the API-key bearer resolver so EndpointView
        # can authenticate `Authorization: Bearer mcpsk_...` without
        # apps.core importing this app (Contract 1).
        from apps.api_keys.auth_backend import authenticate_token
        from apps.api_keys.services.listing import get_live_key_by_id
        from apps.core.bearer import register, register_rehydrator

        register("api_key", authenticate_token)
        # …and the matching rehydrator, so the MCP subprocess can rebuild
        # this credential from the `(kind, pk)` the proxy forwards. Without
        # it the subprocess runs credential-less and every tier check
        # falls to `public`.
        register_rehydrator("api_key", get_live_key_by_id)
