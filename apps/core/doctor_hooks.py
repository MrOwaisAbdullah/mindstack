"""Doctor-probe hook registry.

apps.core can't import apps.<feature> (Contract 1). Same dispatch
pattern as `apps.core.jobs_hook` / `apps.core.bearer` / etc.: a
feature app calls `register_probe(...)` from its `AppConfig.ready()`,
and `apps.core.management.commands.doctor` iterates whatever the
registry holds without knowing where the probes come from.

The probe callable returns a `DoctorRow` (defined in the doctor
command module). The feature-app side is welcome to import DoctorRow
from there — features depending on core is the allowed direction.
"""
from __future__ import annotations

from typing import Any, Callable

# Forward typing — `DoctorRow` lives in
# apps.core.management.commands.doctor and we deliberately don't
# import it here to keep this file dependency-free at boot time.
ProbeFn = Callable[[], Any]

_external_probes: list[ProbeFn] = []


def register_probe(fn: ProbeFn) -> None:
    """Install an external probe. Called from an AppConfig.ready().

    Idempotent at the function level: registering the same callable
    twice still appends — `ready()` runs once per process so duplicates
    only happen in pathological test setups.
    """
    _external_probes.append(fn)


def get_registered_probes() -> list[ProbeFn]:
    """Return a copy of the current probe list."""
    return list(_external_probes)


def reset_for_tests() -> None:
    """Test helper. Clears the registry so tests can isolate."""
    _external_probes.clear()


__all__ = ["ProbeFn", "get_registered_probes", "register_probe", "reset_for_tests"]
