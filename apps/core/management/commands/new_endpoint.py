"""`manage.py new_endpoint <slug>` — scaffold a new endpoint app.

Forwarded from CLAUDE.md's recipe. Generates the standard endpoint-app
skeleton under `apps/app_endpoints/<slug>/`:

    apps/app_endpoints/<slug>/
      __init__.py
      apps.py        # AppConfig with label="endpoints_<slug>"
      endpoints.py   # @endpoint class with Input + Output stubs
      tests.py       # one call_endpoint() test

Then prints the "add `apps.app_endpoints.<slug>` to LOCAL_APPS" hint
so the contributor doesn't forget step 3 of the recipe.

The scaffold is intentionally minimal — it's a starting point, not a
prescription. Authors edit Input / Output / run() to fit their case.
The CLAUDE.md pattern library shows the canonical shapes for the three
common patterns (sync / async).
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
_RESERVED_SLUGS = {"_catalog", "_openapi.json", "_health", "_jobs"}


class Command(BaseCommand):
    help = "Scaffold a new endpoint app under apps/app_endpoints/<slug>/."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "slug",
            help=(
                "Slug for the new endpoint. Must match ^[a-z][a-z0-9-]{0,49}$ "
                "(lowercase ASCII + digits + hyphens, starts with a letter, "
                "<=50 chars)."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing files in the target directory.",
        )

    def handle(self, *args: object, slug: str, force: bool, **options: object) -> None:
        if not _SLUG_RE.fullmatch(slug):
            raise CommandError(
                f"Invalid slug {slug!r}. Must match {_SLUG_RE.pattern} "
                f"(lowercase ASCII, digits, hyphens; start with a letter; <=50 chars)."
            )
        if slug in _RESERVED_SLUGS:
            raise CommandError(
                f"Slug {slug!r} is reserved for built-in routes "
                "(_catalog, _openapi.json, _health, _jobs)."
            )

        target = Path(settings.BASE_DIR) / "apps" / "app_endpoints" / slug.replace("-", "_")
        if target.exists() and any(target.iterdir()) and not force:
            raise CommandError(
                f"Target {target} already exists and is non-empty. "
                "Pass --force to overwrite."
            )
        target.mkdir(parents=True, exist_ok=True)

        files = _render_files(slug)
        for name, content in files.items():
            path = target / name
            path.write_text(content, encoding="utf-8")
            self.stdout.write(f"  wrote {path.relative_to(settings.BASE_DIR)}")

        dotted = f"apps.app_endpoints.{slug.replace('-', '_')}"
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Scaffolded endpoint {slug!r} at {target.relative_to(settings.BASE_DIR)}/")
        )
        self.stdout.write("")
        self.stdout.write("Next steps:")
        self.stdout.write(
            f"  1. Add {dotted!r} to LOCAL_APPS in config/settings/base.py"
        )
        self.stdout.write("  2. Edit endpoints.py — write Input/Output/run()")
        self.stdout.write("  3. Edit tests.py — replace the stub assertion with a real one")
        self.stdout.write("  4. manage.py validate_endpoints")
        self.stdout.write("  5. pytest apps/app_endpoints/{slug}/".format(slug=slug.replace("-", "_")))


def _render_files(slug: str) -> dict[str, str]:
    """Return {filename: content} for the scaffold."""
    snake = slug.replace("-", "_")
    label = f"endpoints_{snake}"
    cls_name = "".join(part.capitalize() for part in snake.split("_"))
    return {
        "__init__.py": f'default_app_config = "apps.app_endpoints.{snake}.apps.{cls_name}Config"\n',
        "apps.py": _APPS_TEMPLATE.format(snake=snake, cls=cls_name),
        "endpoints.py": _ENDPOINTS_TEMPLATE.format(
            slug=slug, snake=snake, cls=cls_name
        ),
        "tests.py": _TESTS_TEMPLATE.format(snake=snake, cls=cls_name),
    }


_APPS_TEMPLATE = '''from django.apps import AppConfig


class {cls}Config(AppConfig):
    name = "apps.app_endpoints.{snake}"
    label = "endpoints_{snake}"
'''


_ENDPOINTS_TEMPLATE = '''"""`{slug}` endpoint.

Scaffolded by `manage.py new_endpoint {slug}`. Edit Input / Output / run()
to fit your case. See CLAUDE.md for the 3-pattern reference library.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from apps.core.ctx import Ctx
from apps.core.registry import Endpoint, endpoint


@endpoint(
    slug="{slug}",
    description="TODO: one-line description that shows in /docs/ + MCP listing.",
    # Tag groups the endpoint on the /docs/ catalog page. Use "examples"
    # for reference endpoints, or your own grouping ("billing", "search", ...).
    tags=(),
)
class {cls}(Endpoint):
    """TODO: docstring shown in the registry + docs page + MCP tool listing."""

    class Input(BaseModel):
        # TODO: replace with your real input fields. Pydantic Field()
        # constraints (max_length, ge, le, pattern, description) auto-render
        # as chips in the /docs/ schema table.
        name: str = Field(default="world", description="Who to greet.", max_length=200)

    class Output(BaseModel):
        # TODO: replace with your real output shape.
        greeting: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        # TODO: implement. See CLAUDE.md for the 3 pattern shapes:
        #   - sync (this scaffold's default)
        #   - sync + safe_request for outbound HTTP
        return self.Output(greeting=f"Hello, {{inp.name}}")
'''


_TESTS_TEMPLATE = '''"""Tests for the `{snake}` endpoint."""
from __future__ import annotations

import asyncio

from apps.app_endpoints.{snake}.endpoints import {cls}
from apps.core.testing import call_endpoint


def test_{snake}_default() -> None:
    # TODO: replace with a real assertion once you've implemented run().
    out = asyncio.run(call_endpoint({cls}, {{}}))
    assert out is not None
'''
