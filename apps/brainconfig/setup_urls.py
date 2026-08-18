"""First-run wizard routes. Mounted at `/setup/` — top level on purpose:
it has to be reachable before an operator, an ops prefix or a brain
exists."""
from django.urls import path

from . import setup_views

app_name = "setup"

urlpatterns = [
    path("", setup_views.setup_home, name="home"),
    path("progress.json", setup_views.build_progress, name="progress"),
    path("verify.json", setup_views.verify_progress, name="verify-progress"),
    path("finish", setup_views.finish, name="finish"),
    path("<slug:slug>/", setup_views.step, name="step"),
]
