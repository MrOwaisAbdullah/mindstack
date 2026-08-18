"""AppConfig for the public docs surface."""
from __future__ import annotations

from django.apps import AppConfig


class DocsConfig(AppConfig):
    name = "apps.docs"
    label = "docs"
    verbose_name = "Docs"
