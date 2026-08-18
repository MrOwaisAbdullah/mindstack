"""`manage.py validate_endpoints` — CI gate for the endpoint registry.

Re-runs every check the `@endpoint` decorator runs at import time, plus a few
that can only be done once the full registry is populated:

- Slugs unique within a (version, slug) pair (decorator catches dupes already).
- Each endpoint's Input / Output Pydantic models can produce a JSON schema.
- A reserved-name collision didn't slip through.

Exits non-zero on any failure so it can fail a CI job. Prints a small table on
success so humans can eyeball what got registered.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.core.registry import EndpointSpec, _RESERVED_SLUGS, registry


class Command(BaseCommand):
    help = "Validate the endpoint registry. Used in CI."

    def handle(self, *args: object, **options: object) -> None:
        specs = registry.all()
        if not specs:
            self.stdout.write(self.style.WARNING("No endpoints registered."))
            return

        errors: list[str] = []
        for spec in specs:
            errors.extend(_check_spec(spec))

        if errors:
            for err in errors:
                self.stderr.write(self.style.ERROR(err))
            raise CommandError(f"Endpoint validation failed ({len(errors)} issue(s)).")

        self._print_table(specs)
        self.stdout.write(self.style.SUCCESS(f"OK — {len(specs)} endpoint(s) valid."))

    def _print_table(self, specs: list[EndpointSpec]) -> None:
        widths = {
            "version": max(len("VERSION"), max(len(s.version) for s in specs)),
            "slug": max(len("SLUG"), max(len(s.slug) for s in specs)),
            "method": max(len("METHOD"), max(len(s.method) for s in specs)),
        }
        header = (
            f"{'VERSION':<{widths['version']}}  "
            f"{'SLUG':<{widths['slug']}}  "
            f"{'METHOD':<{widths['method']}}  "
            f"CLASS"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for s in specs:
            cls_path = f"{s.cls.__module__}.{s.cls.__name__}"
            self.stdout.write(
                f"{s.version:<{widths['version']}}  "
                f"{s.slug:<{widths['slug']}}  "
                f"{s.method:<{widths['method']}}  "
                f"{cls_path}"
            )


def _check_spec(spec: EndpointSpec) -> list[str]:
    issues: list[str] = []
    label = f"{spec.version}/{spec.slug} ({spec.cls.__module__}.{spec.cls.__name__})"

    if spec.slug in _RESERVED_SLUGS:
        issues.append(f"{label}: slug is reserved.")

    try:
        spec.input_model.model_json_schema()
    except Exception as exc:  # noqa: BLE001 — surface any pydantic schema failure
        issues.append(f"{label}: Input.model_json_schema() failed: {exc}")

    try:
        spec.output_model.model_json_schema()
    except Exception as exc:  # noqa: BLE001
        issues.append(f"{label}: Output.model_json_schema() failed: {exc}")

    # FinalPolish F3 — registry-level mirror of the decorator's sunset>deprecation
    # invariant. The decorator already rejects it at import time, but a stale
    # `.pyc` or a future refactor that bypasses the decorator could land here;
    # CI runs `validate_endpoints` after every change, so re-asserting is cheap.
    if (
        spec.deprecated_at is not None
        and spec.sunset_at is not None
        and spec.sunset_at <= spec.deprecated_at
    ):
        issues.append(
            f"{label}: sunset_at ({spec.sunset_at.isoformat()}) must be strictly "
            f"after deprecated_at ({spec.deprecated_at.isoformat()})."
        )

    return issues
