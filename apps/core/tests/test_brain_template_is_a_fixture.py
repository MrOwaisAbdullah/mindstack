"""`brain-template/` is a fixture; the published repo is canonical.

Decided 2026-08-08 (LAUNCH.md §2). The two copies have never had a sync
mechanism and drifted once already: the blocked-scripts section landed
in-tree and never reached the published repo, so a brain generated from
the template had nowhere to declare a setting the server's own warning
told its owner to go and edit.

The decision is public-canonical, in-tree-as-fixture, and a line on the
release checklist — not cross-repo CI, which is more machinery than one
file's drift is worth at this scale.

What stops "fixture" from being a label on a directory nobody checks is
this file. The copy in the tree has to be a brain the server would
actually agree to serve, tested against the same `CONTRACT_PATHS` tuple
`verify_contract()` refuses on — otherwise the thing it exists to
represent is exactly the thing it is not checked for.

It also must stay inert: the engine serves from the operator's clone,
never from this directory. `brain-template/` being importable as a
fallback would turn "this repo is NOT the brain" into a lie that works
in development and fails in production.
"""
from __future__ import annotations

import ast
from pathlib import Path

from apps.brain.services.gitrepo import CONTRACT_PATHS

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "brain-template"


class TestTheFixtureIsAServableBrain:
    def test_it_exists(self) -> None:
        assert FIXTURE.is_dir(), (
            "brain-template/ is the fixture the contract tests read; "
            "removing it silently disarms them"
        )

    def test_every_contract_path_is_present(self) -> None:
        """The same check `verify_contract()` would refuse on."""
        missing = [p for p in CONTRACT_PATHS if not (FIXTURE / p).exists()]

        assert missing == [], (
            "brain-template/ is missing contract paths, so the server would "
            f"refuse to serve a brain generated from it: {', '.join(missing)}"
        )

    def test_the_directory_kinds_match_the_contract(self) -> None:
        """A file where the contract wants a directory passes an
        `.exists()` check and breaks everything downstream of it."""
        for rel in CONTRACT_PATHS:
            target = FIXTURE / rel
            if Path(rel).suffix:
                assert target.is_file(), f"{rel} should be a file"
            else:
                assert target.is_dir(), f"{rel} should be a directory"


def _runtime_string_literals(path: Path) -> list[str]:
    """Every string constant in `path` that is not a docstring.

    Deliberately not a text scan. The first version of this guard grepped
    the source and fired on a *comment* in gitrepo.py explaining that a
    test pins the constant to the template — prose about the fixture is
    not a reference to it, and a guard that cannot tell the difference
    gets silenced rather than obeyed. Comments never reach the AST, so
    only docstrings need excluding.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestTheEngineNeverServesFromIt:
    def test_no_runtime_module_reaches_for_the_fixture(self) -> None:
        """Tests may read it. Engine code may not: the brain is the
        operator's clone, and a fallback to this directory would work in
        development and serve a stranger an empty brain in production."""
        offenders = []
        for path in (REPO_ROOT / "apps").rglob("*.py"):
            if "tests" in path.parts:
                continue
            if any("brain-template" in s for s in _runtime_string_literals(path)):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())

        assert offenders == [], (
            "engine code reached for the bundled template: " + ", ".join(offenders)
        )
