"""Every env var this project declares is one it actually reads.

`config/settings/env.py` is the typed loader, and it arrived from the
template carrying a SaaS product's whole surface: Stripe keys, allauth
Google/GitHub client IDs, Postmark and Resend tokens, OAuth token
lifetimes, a billing mode, three feature flags, a payment-provider
driver, a webhook-callback reaper timeout, a demo transcriber's webhook
secret. Thirty-three declarations that nothing anywhere read.

That is not harmless in a repo people are about to read. A declared
setting is a promise: someone self-hosting this sets `EMAIL_HOST` and
reasonably expects mail over SMTP, when `base.py` pins the backend to
console and always did. And "why does a self-hosted memory server want
my Stripe secret key?" is a fair question to ask of a project asking for
your API credentials.

Two directions, both asserted:

- nothing is declared that nothing reads;
- nothing is documented in `.env.example` that is not a real variable —
  the direction that actively misleads, and it had two live cases
  (`BRAIN_GIT_WRITE_PAT_FILE` for `BRAIN_GIT_WRITE_PAT_PATH`,
  `SECURITY_TXT_EXPIRES` for `SECURITY_TXT_EXPIRES_DAYS`).

The reverse of the second — every variable documented — is deliberately
NOT asserted. Container paths, cache-key namespacing and the MCP
loopback address are set by compose and belong nowhere near an
operator's `.env`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_PY = REPO_ROOT / "config/settings/env.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Where a declared setting may legitimately be consumed.
_SEARCH_ROOTS = ("apps", "config", "docker", "templates")
_SEARCH_SUFFIXES = {".py", ".html", ".sh", ".yml", ".yaml", ".md", ".txt"}
_EXTRA_FILES = ("docker-compose.yml", "docker-compose.local.yml", "manage.py", ".env.example")

#: Consumed outside the typed loader entirely — `prod.py` reads these
#: through `os.environ` on purpose (they configure the settings module
#: that builds the loader), and compose owns the database password.
_NON_PYDANTIC = {"CSRF_TRUSTED_ORIGINS", "SECURE_SSL_REDIRECT_ENABLED", "POSTGRES_PASSWORD"}

#: Names the template left behind. Listed explicitly so re-importing any
#: of them is a test failure with a reason attached, not a silent regrowth.
_FORBIDDEN = (
    "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET",
    "ALLAUTH_GOOGLE_CLIENT_ID", "ALLAUTH_GITHUB_CLIENT_ID",
    "POSTMARK_API_TOKEN", "RESEND_API_KEY", "BILLING_MODE",
    "FEATURE_BILLING_ENABLED", "PAYMENT_PROVIDER_DRIVER",
    "WEBHOOK_CALLBACK_TIMEOUT_SECONDS", "DEMO_TRANSCRIBER_WEBHOOK_SECRET",
)


def _declared() -> list[str]:
    return sorted(set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]{2,})\s*:", ENV_PY.read_text(encoding="utf-8"), re.M)))


def _documented() -> list[str]:
    return sorted(set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)))


def _corpus() -> dict[Path, str]:
    files: list[Path] = []
    for root in _SEARCH_ROOTS:
        d = REPO_ROOT / root
        if d.is_dir():
            files += [
                p for p in d.rglob("*")
                if p.is_file()
                and p.suffix in _SEARCH_SUFFIXES
                and "__pycache__" not in p.parts
                # Production code only. This file names every variable it
                # forbids, and counting itself as a reader let twelve dead
                # declarations pass on the strength of the guard against
                # them.
                and "tests" not in p.parts
            ]
    files += [REPO_ROOT / name for name in _EXTRA_FILES]
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in files if p.exists()}


CORPUS = _corpus()


def _readers(name: str) -> list[str]:
    """Files that CONSUME the setting.

    `.env.example` is excluded: documenting a variable is not reading it,
    and counting it would have let thirteen of the dead declarations pass
    on the strength of the file that advertised them.
    """
    return [
        p.relative_to(REPO_ROOT).as_posix()
        for p, text in CORPUS.items()
        if p not in (ENV_PY, ENV_EXAMPLE) and name in text
    ]


@pytest.mark.parametrize("name", _declared())
def test_every_declared_setting_is_read_somewhere(name: str) -> None:
    assert _readers(name), (
        f"{name} is declared in env.py and read nowhere. A declared setting "
        "is a promise that setting it does something."
    )


@pytest.mark.parametrize("name", _documented())
def test_every_documented_variable_is_real(name: str) -> None:
    if name in _NON_PYDANTIC:
        return
    assert name in _declared(), (
        f".env.example documents {name}, which the app never reads. An "
        "operator who sets it gets silence."
    )


@pytest.mark.parametrize("name", _FORBIDDEN)
def test_the_saas_boilerplate_stays_gone(name: str) -> None:
    hits = [p.relative_to(REPO_ROOT).as_posix() for p, text in CORPUS.items() if name in text]
    assert not hits, f"{name} is back in {hits} — this project has no such feature"


def test_the_loader_still_loads() -> None:
    """A deletion that breaks a validator would fail at boot, not here,
    so pull the model in and instantiate it."""
    from config.settings.env import Settings

    settings = Settings()

    assert settings.APP_NAME
    # Spot-check one survivor from each section the removal touched.
    assert settings.EMAIL_BACKEND_DRIVER == "console"
    assert settings.STORAGE_DRIVER == "filesystem"
    assert settings.MCP_OAUTH_DCR_MODE in ("anonymous", "iat_required", "disabled")
