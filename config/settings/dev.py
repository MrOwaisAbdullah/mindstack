"""Development settings: DEBUG on, SQLite, plain static storage."""
from config.logging import build_logging

from .base import *  # noqa: F401,F403

DEBUG = True

LOGGING = build_logging(debug=True)

# Manifest storage needs collectstatic; dev serves straight from static/.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

ALLOWED_HOSTS = list({*ALLOWED_HOSTS, "localhost", "127.0.0.1", "0.0.0.0"})  # noqa: F405

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# CSP is ENFORCED in dev, deliberately — dev used to default this to
# report-only, which is exactly what hid a release blocker for the whole
# project: `style-src 'self' 'nonce-…'` drops every `style="…"` attribute,
# so prod rendered unstyled while every local visual check looked fine.
# Dev must fail the same way prod does. base.py already defaults to
# enforcing and still honours `CSP_REPORT_ONLY=true` in .env for anyone
# who needs to inventory violations without the page falling apart.
