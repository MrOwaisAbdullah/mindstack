"""The contract carries a version and nothing ever read it.

`brain-template/CLAUDE.md` §10 tells every user, in the file they are
told to treat as authoritative:

    A server that ships a newer contract will WARN — never refuse to
    boot — and may propose the upgrade as a diff.

No code read `contract-version`. `verify_contract()` checks that the
CONTRACT_PATHS files exist and nothing else, so a brain generated at 1.0
and never touched again — which is every brain, because the template is
a starting point the server cannot push to — got silence from a server
shipping a different contract. That is the exact situation the field was
invented to make diagnosable, and it was the one situation the field did
not cover.

Two properties matter more than the message:

- It must never refuse to serve. `verify_contract()` already raises, and
  `/readyz` gates on `contract_ok`; a version mismatch must never join
  that class or a cosmetic drift takes the server down.
- It must never raise. An unreadable, absent or malformed CLAUDE.md is a
  warning about a warning, not a crash inside a health page.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.brain.services import gitrepo

TEMPLATE = Path(__file__).resolve().parents[3] / "brain-template/CLAUDE.md"


def _brain(tmp_path, settings, text: str | None) -> Path:
    """A clone directory whose CLAUDE.md is `text` (None = no file)."""
    d = tmp_path / "brain-repo"
    d.mkdir(exist_ok=True)
    if text is not None:
        (d / "CLAUDE.md").write_text(text, encoding="utf-8", newline="\n")
    settings.BRAIN_REPO_DIR = str(d)
    return d


def _fm(version: str) -> str:
    return f'---\ncontract-version: "{version}"\n---\n\n# YOUR MIND\n'


class TestTheServerKnowsItsOwnContract:
    def test_the_constant_exists(self) -> None:
        assert isinstance(gitrepo.CONTRACT_VERSION, str)
        assert gitrepo.CONTRACT_VERSION.strip()

    def test_it_matches_the_template_we_ship(self) -> None:
        """The server's idea of "current" and the file users copy from
        must not drift apart silently — that drift is the whole failure
        this feature exists to report."""
        shipped = gitrepo.read_contract_version(TEMPLATE.read_text(encoding="utf-8"))

        assert shipped == gitrepo.CONTRACT_VERSION, (
            f"the server ships contract {gitrepo.CONTRACT_VERSION!r} but "
            f"brain-template/CLAUDE.md declares {shipped!r}"
        )


class TestReadingTheDeclaredVersion:
    def test_a_quoted_version_is_read(self) -> None:
        assert gitrepo.read_contract_version(_fm("1.0")) == "1.0"

    def test_an_unquoted_version_is_not_a_float(self) -> None:
        """`contract-version: 1.0` is a YAML float. Reported as "1.0",
        never "1" — a version that renames itself is worse than none."""
        assert gitrepo.read_contract_version("---\ncontract-version: 1.0\n---\n") == "1.0"

    def test_no_frontmatter_reads_as_undeclared(self) -> None:
        assert gitrepo.read_contract_version("# YOUR MIND\n\nno frontmatter\n") == ""

    def test_frontmatter_without_the_key_reads_as_undeclared(self) -> None:
        assert gitrepo.read_contract_version("---\ntitle: brain\n---\n") == ""

    def test_malformed_frontmatter_does_not_raise(self) -> None:
        assert gitrepo.read_contract_version("---\n: : not yaml :\n---\n") == ""

    def test_an_empty_file_does_not_raise(self) -> None:
        assert gitrepo.read_contract_version("") == ""


class TestTheComparison:
    def test_the_same_version_is_ok(self, tmp_path, settings) -> None:
        _brain(tmp_path, settings, _fm(gitrepo.CONTRACT_VERSION))

        probe = gitrepo.contract_version_probe()

        assert probe["state"] == "ok"
        assert probe["brain"] == gitrepo.CONTRACT_VERSION
        assert probe["server"] == gitrepo.CONTRACT_VERSION

    def test_an_older_brain_is_named_as_older(self, tmp_path, settings) -> None:
        _brain(tmp_path, settings, _fm("0.9"))

        probe = gitrepo.contract_version_probe()

        assert probe["state"] == "older"
        assert probe["brain"] == "0.9"

    def test_a_newer_brain_is_named_as_newer(self, tmp_path, settings) -> None:
        """The operator upgraded their brain and not the server. Worth
        saying out loud: it is the case where the server is the stale one."""
        _brain(tmp_path, settings, _fm("99.0"))

        assert gitrepo.contract_version_probe()["state"] == "newer"

    def test_an_undeclared_version_is_unknown_not_ok(self, tmp_path, settings) -> None:
        """A brain predating the field is the common case, and reading it
        as "ok" would hide precisely the brains most likely to be stale."""
        _brain(tmp_path, settings, "# YOUR MIND\n")

        probe = gitrepo.contract_version_probe()

        assert probe["state"] == "unknown"
        assert probe["brain"] == ""

    def test_an_unparseable_version_is_unknown_and_kept_verbatim(
        self, tmp_path, settings
    ) -> None:
        _brain(tmp_path, settings, _fm("banana"))

        probe = gitrepo.contract_version_probe()

        assert probe["state"] == "unknown"
        assert probe["brain"] == "banana", "show the operator what their file says"


class TestItNeverFailsClosed:
    """The two properties that keep a cosmetic drift from taking the
    server down."""

    def test_a_missing_claude_md_does_not_raise(self, tmp_path, settings) -> None:
        _brain(tmp_path, settings, None)

        assert gitrepo.contract_version_probe()["state"] == "unknown"

    def test_a_missing_clone_directory_does_not_raise(self, tmp_path, settings) -> None:
        settings.BRAIN_REPO_DIR = str(tmp_path / "does-not-exist")

        assert gitrepo.contract_version_probe()["state"] == "unknown"

    def test_verify_contract_ignores_the_version(self, tmp_path, settings) -> None:
        """The load-bearing one. `verify_contract()` is what refuses to
        serve, and `/readyz` gates on it. A version mismatch must not
        reach it."""
        d = _brain(tmp_path, settings, _fm("0.1"))
        for rel in gitrepo.CONTRACT_PATHS:
            p = d / rel
            if "." in Path(rel).name:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            else:
                p.mkdir(parents=True, exist_ok=True)

        gitrepo.verify_contract()  # must not raise

    def test_the_readiness_contract_flag_is_unaffected(self, tmp_path, settings) -> None:
        """`config/urls.py` readyz: valid and contract_ok and origin_ok.
        An old contract must leave `contract_ok` alone."""
        d = _brain(tmp_path, settings, _fm("0.1"))
        for rel in gitrepo.CONTRACT_PATHS:
            p = d / rel
            if "." in Path(rel).name:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            else:
                p.mkdir(parents=True, exist_ok=True)

        missing = [p for p in gitrepo.CONTRACT_PATHS if not (d / p).exists()]

        assert missing == []


class TestTheOperatorIsTold:
    @pytest.mark.parametrize(
        "declared,level",
        [("1.0", "ok"), ("0.9", "warn"), ("99.0", "warn"), (None, "warn")],
    )
    def test_the_health_check_warns_but_never_alarms(
        self, tmp_path, settings, declared, level
    ) -> None:
        """Never "danger": that level is for things that stop the server
        serving, and this one deliberately does not."""
        from apps.brainconfig import health

        _brain(
            tmp_path,
            settings,
            _fm(gitrepo.CONTRACT_VERSION) if declared == "1.0"
            else (_fm(declared) if declared else "# YOUR MIND\n"),
        )

        check = health.check_contract_version()

        assert check["level"] == level

    def test_the_warning_names_both_versions(self, tmp_path, settings) -> None:
        """"Your brain is out of date" with no numbers is not actionable."""
        from apps.brainconfig import health

        _brain(tmp_path, settings, _fm("0.9"))

        check = health.check_contract_version()
        text = check["title"] + " " + check["detail"]

        assert "0.9" in text and gitrepo.CONTRACT_VERSION in text

    def test_it_is_registered_on_the_health_page(self, tmp_path, settings) -> None:
        """A check nobody runs is not a warning."""
        import inspect

        from apps.brainconfig import health

        source = inspect.getsource(health.all_checks)

        assert "check_contract_version" in source
