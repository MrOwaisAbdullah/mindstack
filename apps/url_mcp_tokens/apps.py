from django.apps import AppConfig


class UrlMcpTokensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.url_mcp_tokens"
    label = "url_mcp_tokens"

    def ready(self) -> None:
        # BOTH registrations are required, and the second one is the easy
        # one to miss.
        #
        # `register` is how `/mcp/k/<token>/` authenticates at all: the
        # proxy calls `bearer.resolve`, which walks this list.
        #
        # `register_rehydrator` is how the identity survives the proxy →
        # FastMCP subprocess hop. The proxy strips the credential and
        # forwards only `(user_id, kind, pk)`; without a rehydrator for
        # this kind the subprocess rebuilds nothing, `ctx.credential` is
        # None, `tiers.tier_for_credential` reads that as unprofiled and
        # every caller lands on `public` — so `propose-feed` refuses
        # everyone and reads return an almost-empty brain, with no error
        # raised anywhere. `docs/DOCS-LAB-FINDINGS.md` §1 is that bug,
        # found on API keys, and it fails closed, which is why it went
        # unnoticed for so long.
        from apps.core.bearer import register, register_rehydrator
        from apps.url_mcp_tokens.auth_backend import (
            CREDENTIAL_KIND,
            authenticate_url_token,
        )
        from apps.url_mcp_tokens.services.listing import get_live_token_by_id

        register(CREDENTIAL_KIND, authenticate_url_token)
        register_rehydrator(CREDENTIAL_KIND, get_live_token_by_id)
