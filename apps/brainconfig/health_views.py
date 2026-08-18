"""`/ops/health/` — what's wrong, and the buttons that fix it."""
from __future__ import annotations

import secrets

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.http import require_http_methods, require_POST

from apps.brain.services import gitcreds, gitrepo
from apps.brainconfig import health, jobs, maintenance, services as cfg
from apps.brainconfig.nav import ops_context


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def health_page(request):
    if request.method == "POST":
        return _act(request)

    checks = health.all_checks(request)
    problems = health.problems(checks)
    return render(request, "ops/health.html", {
        "checks": checks,
        "problems": problems,
        "passing": [c for c in checks if c["level"] not in ("danger", "warn")],
        "n_danger": sum(1 for c in problems if c["level"] == "danger"),
        "n_warn": sum(1 for c in problems if c["level"] == "warn"),
        "jobs": jobs.active(),
        "runnable": maintenance.RUNNABLE,
        "repo_url": gitrepo.configured_url(),
        "webhook_url": request.build_absolute_uri("/webhooks/github"),
        "webhook_set": bool(cfg.get("GITHUB_WEBHOOK_SECRET")),
        "deploy_key": gitcreds.public_key(),
        "git_status": gitcreds.status(),
        **ops_context(request),
    })


def _act(request):
    action = request.POST.get("action", "")

    # Slow, failure-prone work goes to the worker so a request timeout can
    # never leave the UI unable to say what happened.
    spec = maintenance.RUNNABLE.get(action)
    if spec is not None:
        started, message = jobs.enqueue(spec)
        (messages.success if started else messages.error)(request, message)
        return redirect(request.path)

    if action == "generate_webhook_secret":
        cfg.set_value("GITHUB_WEBHOOK_SECRET", secrets.token_hex(32), actor=request.user)
        messages.success(request, "Webhook secret generated — copy it into GitHub.")
    elif action == "reveal_webhook_secret":
        request.session["reveal_webhook"] = True
    elif action == "rotate_deploy_key":
        from apps.brainconfig import setup_state

        gitcreds.generate_keypair(actor=request.user)
        setup_state.clear_read_verified()
        messages.warning(
            request,
            "New deploy key generated. The previous one stops working now — "
            "replace it in GitHub, then Verify.",
        )
    elif action == "ack_backup":
        health.acknowledge_backup()
        messages.success(request, "Noted. Ask again by clearing it in the admin.")
    elif action == "clear_job":
        jobs.clear(request.POST.get("job", ""))
    else:
        messages.error(request, f"Unknown action {action!r}.")
    return redirect(request.path)


@staff_member_required(login_url="login")
def jobs_json(request):
    """Polled while a repair job runs, so buttons can stay disabled."""
    return JsonResponse({
        "jobs": {
            j["spec"].name: {k: v for k, v in j.items() if k != "spec"}
            for j in jobs.active()
        },
        "any_running": any(j.get("state") == "running" for j in jobs.active()),
    })


@staff_member_required(login_url="login")
@require_POST
def reveal_webhook(request):
    """The webhook secret is write-only everywhere else; GitHub needs it once."""
    return JsonResponse({"secret": cfg.get("GITHUB_WEBHOOK_SECRET")})
