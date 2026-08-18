"""ASGI entry point. uvicorn (dev) or gunicorn+uvicorn-workers (prod) load
`application` for HTTP + websocket."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
