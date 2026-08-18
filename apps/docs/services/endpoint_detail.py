"""Endpoint detail-page bundle (10.4.3 / 10.4.4 / 10.4.5).

Reads `apps.core.registry` for the spec, walks the Pydantic JSON
schemas to build top-level field tables (with collapsible nested
sub-schemas per locked design choice 2(c)), and pre-renders the four
SDK snippets — curl, MCP `claude_desktop_config.json`, Python SDK,
JS SDK — so the template just dereferences strings.

Note on the curl-prefilled-key flow (10.4.4):

    The spec asks for a "prefilled key when authed" but secrets are
    SHA-256 hashed at rest — we never store the
    plaintext, so we literally cannot reconstruct one to inject. The
    pragmatic implementation: always show `mcpsk_<your_api_key>` as
    a placeholder + surface a contextual note about the user's key
    inventory (count of active keys + jump-to-manage link) when
    authed; anonymous viewers get a "Sign in and create a key" CTA.
    Same UX as Stripe's API reference (no secret leak path).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pygments import highlight as _pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name

log = logging.getLogger(__name__)

# Match the codehilite extension config used for markdown guides so
# the rendered output uses the same `.highlight` CSS hooks. `nowrap`
# would skip the wrapping <div>; we want it for the copy-button
# toolbar to anchor to.
_HTML_FORMATTER = HtmlFormatter(cssclass="highlight", nowrap=False)


def _highlight(source: str, lang: str) -> str:
    """Render `source` through Pygments and wrap with `data-lang` so
    the code-copy JS can show the language label. Falls back to a
    plain `<div class="highlight"><pre><code>...</code></pre></div>`
    wrapper when the lexer name is unknown — we still want the same
    surface treatment + copy button even without coloring."""
    try:
        lexer = get_lexer_by_name(lang, stripall=False)
    except Exception:
        from html import escape

        body = escape(source)
        return (
            f'<div class="highlight" data-lang="{lang}">'
            f'<pre><span></span><code>{body}</code></pre></div>'
        )
    html = _pygments_highlight(source, lexer, _HTML_FORMATTER).rstrip()
    return html.replace(
        '<div class="highlight">',
        f'<div class="highlight" data-lang="{lang}">',
        1,
    )


@dataclass
class SchemaField:
    name: str
    type_label: str
    required: bool
    default: Any
    description: str
    constraints: list[str] = field(default_factory=list)
    # When the field's type is a nested model, holds the resolved sub-
    # schema as a list of SchemaField rows for the collapsible expander.
    nested: list["SchemaField"] = field(default_factory=list)
    # JSON-schema enum values, when the field is an enum.
    enum: list[Any] = field(default_factory=list)

    @property
    def has_default(self) -> bool:
        return self.default is not None or self.default == 0 or self.default == ""

    @property
    def input_kind(self) -> str:
        """Pick a form input control for the try-it panel (G4).

        Returns one of: `text`, `number`, `boolean`, `select`, `json`.
        Anything that doesn't map to a primitive (arrays, objects,
        unions) gets a JSON textarea so the user can hand-write the
        payload — the alternative (a UI builder for arbitrary nested
        Pydantic models) is way out of scope for v1.
        """
        if self.enum:
            return "select"
        t = self.type_label
        if t == "string":
            return "text"
        if t in ("integer", "number"):
            return "number"
        if t == "boolean":
            return "boolean"
        return "json"


@dataclass
class EndpointDetailBundle:
    spec: object  # apps.core.registry.EndpointSpec
    input_fields: list[SchemaField] = field(default_factory=list)
    output_fields: list[SchemaField] = field(default_factory=list)
    sample_input_json: str = "{}"
    # Raw dict form of the sample input — fed to `{{ ...|json_script }}`
    # in the try-it template so Alpine's `x-data` doesn't have to inline
    # an HTML `<script>` block into an attribute (which breaks the parse).
    # The JSON string above is what the curl/Python/JS snippet renderers
    # consume; the dict is what the in-browser try-it panel parses.
    sample_input_dict: dict = field(default_factory=dict)
    # Pre-rendered SDK snippets — the template just emits these.
    curl_snippet: str = ""
    mcp_config_snippet: str = ""
    python_snippet: str = ""
    js_snippet: str = ""
    # Same snippets run through Pygments so the docs page matches
    # the `.highlight` styling + copy-button toolbar used by guides.
    curl_html: str = ""
    mcp_config_html: str = ""
    python_html: str = ""
    js_html: str = ""
    # Authed-user context for the curl tab.
    user_active_keys: int = 0
    is_anonymous: bool = True
    # FinalPolish F3 — deprecation banner state. The detail template
    # picks one of three banners based on this:
    #   "deprecated"      → yellow "Deprecated" banner (post deprecated_at,
    #                       pre sunset_at OR sunset never set).
    #   "sunset_upcoming" → red "Sunset on <date>" banner (sunset_at set,
    #                       in the future). Always rides ON TOP of the
    #                       deprecation banner — endpoints in this state
    #                       are still callable but going away soon.
    #   "sunset_past"     → red "Removed on <date>" banner (sunset_at in
    #                       the past — the REST view 410s every call).
    # Empty string when the endpoint isn't deprecated.
    deprecation_state: str = ""
    deprecation_message: str = ""
    # ISO strings so the template can render them without further
    # formatting work; the bundle keeps the raw datetimes off the
    # context to avoid pickling/SerializeError edge cases.
    deprecated_at_iso: str = ""
    sunset_at_iso: str = ""


def get_endpoint_detail(spec, request) -> EndpointDetailBundle:
    """Build the detail-page bundle for one EndpointSpec."""
    bundle = EndpointDetailBundle(spec=spec)

    try:
        in_schema = spec.input_model.model_json_schema()
        bundle.input_fields = _walk_schema(in_schema)
        bundle.sample_input_dict = _build_sample_input(in_schema)
        bundle.sample_input_json = (
            json.dumps(bundle.sample_input_dict, indent=2)
            if bundle.sample_input_dict
            else "{}"
        )
    except Exception:
        log.warning("docs: input schema render failed", exc_info=True)

    try:
        out_schema = spec.output_model.model_json_schema()
        bundle.output_fields = _walk_schema(out_schema)
    except Exception:
        log.warning("docs: output schema render failed", exc_info=True)

    # User context for curl tab.
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        bundle.is_anonymous = False
        try:
            from apps.api_keys import api as api_keys

            keys = api_keys.list_for_user(user)
            bundle.user_active_keys = sum(1 for k in keys if k.is_active)
        except Exception:
            log.warning("docs: api_keys lookup failed", exc_info=True)

    full_url = request.build_absolute_uri(f"/api/{spec.version}/{spec.slug}")
    bundle.curl_snippet = _render_curl(full_url, bundle.sample_input_json)
    bundle.mcp_config_snippet = _render_mcp_config(request)
    bundle.python_snippet = _render_python(full_url, bundle.sample_input_json)
    bundle.js_snippet = _render_js(full_url, bundle.sample_input_json)

    bundle.curl_html = _highlight(bundle.curl_snippet, "bash")
    bundle.mcp_config_html = _highlight(bundle.mcp_config_snippet, "json")
    bundle.python_html = _highlight(bundle.python_snippet, "python")
    bundle.js_html = _highlight(bundle.js_snippet, "javascript")

    # FinalPolish F3 — deprecation banner state. Walks the same is_*_at
    # predicates the REST view uses so the docs page agrees with the
    # actual gate behavior (yellow/banner mismatch would be confusing).
    from django.utils import timezone

    now = timezone.now()
    deprecated_at = getattr(spec, "deprecated_at", None)
    sunset_at = getattr(spec, "sunset_at", None)
    bundle.deprecation_message = getattr(spec, "deprecation_message", "") or ""
    if deprecated_at is not None:
        bundle.deprecated_at_iso = deprecated_at.isoformat()
    if sunset_at is not None:
        bundle.sunset_at_iso = sunset_at.isoformat()
        if now >= sunset_at:
            bundle.deprecation_state = "sunset_past"
        else:
            bundle.deprecation_state = "sunset_upcoming"
    elif deprecated_at is not None and now >= deprecated_at:
        bundle.deprecation_state = "deprecated"
    elif getattr(spec, "deprecated", False):
        # Legacy boolean flag with no date — show the chip-style banner
        # so the page reflects that the endpoint is on its way out, even
        # without dates set.
        bundle.deprecation_state = "deprecated"

    return bundle


# ---- Schema walking -------------------------------------------------------


def _walk_schema(schema: dict) -> list[SchemaField]:
    """Walk the top-level `properties` of a Pydantic JSON schema. Nested
    `object` types resolve their `$ref` against `$defs` and recurse for
    the collapsible expander."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    defs = schema.get("$defs") or schema.get("definitions") or {}

    out: list[SchemaField] = []
    for name, prop in properties.items():
        out.append(_field_from_property(name, prop, defs, required))
    return out


def _field_from_property(
    name: str, prop: dict, defs: dict, required: set
) -> SchemaField:
    resolved = _resolve_ref(prop, defs)
    type_label, nested = _type_label_and_nested(resolved, defs)
    return SchemaField(
        name=name,
        type_label=type_label,
        required=name in required,
        default=resolved.get("default"),
        description=resolved.get("description") or prop.get("description") or "",
        constraints=_collect_constraints(resolved),
        nested=nested,
        enum=list(resolved.get("enum") or []),
    )


def _resolve_ref(prop: dict, defs: dict) -> dict:
    """If `prop` is a `$ref` pointer, return the resolved definition;
    else return `prop` unchanged. Handles one level — chained refs aren't
    expected in Pydantic-generated schemas."""
    ref = prop.get("$ref")
    if not ref:
        return prop
    # Format: "#/$defs/Foo" or "#/definitions/Foo"
    name = ref.rsplit("/", 1)[-1]
    return defs.get(name, prop)


def _type_label_and_nested(
    prop: dict, defs: dict
) -> tuple[str, list[SchemaField]]:
    """Return a human type label + (optional) nested SchemaField list
    for the collapsible expander when the type resolves to an object."""
    # Union types via anyOf (most commonly Optional → ["X", "null"]).
    if "anyOf" in prop:
        labels = [_type_label_and_nested(a, defs)[0] for a in prop["anyOf"]]
        # Promote nested expansion if any branch is an object.
        nested: list[SchemaField] = []
        for branch in prop["anyOf"]:
            resolved = _resolve_ref(branch, defs)
            if resolved.get("type") == "object" and resolved.get("properties"):
                nested = _walk_object(resolved, defs)
                break
        return " | ".join(labels), nested

    t = prop.get("type")
    if t == "array":
        items = prop.get("items") or {}
        inner_label, _ = _type_label_and_nested(items, defs)
        return f"array<{inner_label}>", []
    if t == "object" or prop.get("properties"):
        # Inline object — render a sub-table.
        title = prop.get("title") or "object"
        nested = _walk_object(prop, defs)
        return f"object<{title}>", nested
    if t == "null":
        return "null", []
    if isinstance(t, list):
        return " | ".join(t), []
    if t:
        return t, []
    return "any", []


def _walk_object(schema: dict, defs: dict) -> list[SchemaField]:
    """Recurse into an `object`-typed schema's `properties`."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: list[SchemaField] = []
    for name, prop in properties.items():
        out.append(_field_from_property(name, prop, defs, required))
    return out


_CONSTRAINT_KEYS = (
    "maxLength",
    "minLength",
    "maximum",
    "minimum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "multipleOf",
    "pattern",
)


def _collect_constraints(prop: dict) -> list[str]:
    """Surface JSON-schema validation hints as `key=value` strings."""
    out: list[str] = []
    for k in _CONSTRAINT_KEYS:
        if k in prop:
            out.append(f"{k}={prop[k]}")
    if "enum" in prop:
        out.append(f"enum={prop['enum']}")
    if "format" in prop:
        out.append(f"format={prop['format']}")
    return out


# ---- Sample-input + snippet rendering -------------------------------------


def _build_sample_input(schema: dict) -> dict[str, Any]:
    """Construct a sample input dict using Pydantic defaults where set
    + zero-values for required-no-default fields. Caller pretty-prints
    when a string form is needed (curl/SDK snippets); the dict form
    feeds `json_script` in the try-it template."""
    sample: dict[str, Any] = {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    for name, prop in properties.items():
        if "default" in prop and prop["default"] is not None:
            sample[name] = prop["default"]
        elif name in required:
            sample[name] = _zero_value_for(prop)

    return sample


def _zero_value_for(prop: dict) -> Any:
    """Pick a sensible placeholder for a required field with no default."""
    t = prop.get("type")
    if t == "string":
        return prop.get("examples", [""])[0] if "examples" in prop else ""
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    return None


def _render_curl(full_url: str, body_json: str) -> str:
    return (
        f'curl -X POST {full_url} \\\n'
        f'  -H "Authorization: Bearer mcpsk_<your_api_key>" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d '{body_json}'"
    )


def _render_python(full_url: str, body_json: str) -> str:
    return (
        "import requests\n\n"
        f'r = requests.post(\n'
        f'    "{full_url}",\n'
        f'    headers={{"Authorization": "Bearer mcpsk_<your_api_key>"}},\n'
        f"    json={body_json},\n"
        f")\n"
        f"r.raise_for_status()\n"
        f"print(r.json())"
    )


def _render_js(full_url: str, body_json: str) -> str:
    return (
        f'const r = await fetch("{full_url}", {{\n'
        f'  method: "POST",\n'
        f"  headers: {{\n"
        f'    "Authorization": "Bearer mcpsk_<your_api_key>",\n'
        f'    "Content-Type": "application/json",\n'
        f"  }},\n"
        f"  body: JSON.stringify({body_json}),\n"
        f"}});\n"
        f"if (!r.ok) throw new Error(`HTTP ${{r.status}}`);\n"
        f"console.log(await r.json());"
    )


def _render_mcp_config(request) -> str:
    """Snippet to drop into Claude Desktop's `claude_desktop_config.json`.

    Claude Desktop natively speaks **stdio**, so a remote HTTP server
    needs the `mcp-remote` shim — which is exactly what the MCP setup
    guide prescribes. This used to emit a `{"url", "transport", "auth"}`
    object instead: a shape Claude Desktop does not accept, so the
    copy-pasted config silently produced no tools and sent the reader
    hunting for a key problem that wasn't there.
    """
    base = request.build_absolute_uri("/mcp/").rstrip("/") + "/"
    config = {
        "mcpServers": {
            "brainoutside": {
                "command": "npx",
                "args": [
                    "-y",
                    "mcp-remote",
                    base,
                    "--header",
                    "Authorization: Bearer mcpsk_<your_api_key>",
                ],
            }
        }
    }
    return json.dumps(config, indent=2)


__all__ = ["EndpointDetailBundle", "SchemaField", "get_endpoint_detail"]
