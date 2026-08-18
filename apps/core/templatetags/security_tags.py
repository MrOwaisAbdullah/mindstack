"""Template tag library for security primitives.

Exposes `{% csp_nonce %}` for inline `<script>` / `<style>` blocks:

    {% load security_tags %}
    <script nonce="{% csp_nonce %}">
      // your inline JS
    </script>

The nonce is minted per request by `SecurityHeadersMiddleware` and read
off `request.csp_nonce`. The CSP `script-src` / `style-src` directives
the same middleware stamps onto the response carry the same nonce as a
`'nonce-XXX'` source, so the browser only executes inline tags the
server intentionally produced.

The matching context processor `config.context_processors.csp_nonce`
exposes the value as the template variable `{{ csp_nonce }}` for any
case where the tag form is awkward (e.g. inside an attribute that's
itself rendered in a `{% block %}`).
"""
from __future__ import annotations

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def csp_nonce(context) -> str:
    """Return the per-request CSP nonce for the current response.

    Empty string when there's no request in the template context (e.g.
    a unit test that renders a template without `RequestContext`) — the
    template still renders; the inline tag just won't match the CSP
    header for that response.
    """
    request = context.get("request")
    if request is None:
        return ""
    return getattr(request, "csp_nonce", "") or ""
