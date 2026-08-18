"""Ops chat views (M3.3). Pages are classic sync views; the send path is
an ASYNC view streaming SSE — in-process under ASGI, no worker plumbing
(PLAN.md §7: the queue is for feeds only)."""
from __future__ import annotations

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseForbidden, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.brainconfig.nav import ops_context

from .models import ChatMessage, ChatSession

# Sessions accumulate forever (a test bench is used daily); page like the
# brain browser instead of silently truncating at an arbitrary slice.
CHAT_PAGE_SIZE = 50


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def chat_home(request):
    if request.method == "POST":
        tier = request.POST.get("tier", "")
        if tier not in dict(ChatSession.TIERS):
            tier = "agents-only"
        session = ChatSession.objects.create(tier=tier)
        return redirect("brainconfig:chat-session", pk=session.pk)
    # get_page clamps junk and out-of-range values instead of 404ing.
    paginator = Paginator(ChatSession.objects.all(), CHAT_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "ops/chat.html",
        {
            "sessions": list(page_obj),
            "page_obj": page_obj,
            "tiers": [t for t, _ in ChatSession.TIERS],
            **ops_context(request),
        },
    )


@staff_member_required(login_url="login")
@require_http_methods(["GET", "POST"])
def chat_session(request, pk: int):
    session = ChatSession.objects.filter(pk=pk).first()
    if session is None:
        raise Http404
    if request.method == "POST" and request.POST.get("action") == "delete":
        session.delete()
        return redirect("brainconfig:chat")
    return render(
        request,
        "ops/chat_session.html",
        {
            "session": session,
            "chat_messages": list(session.messages.all()),
            **ops_context(request),
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chat_send(request, pk: int):
    """POST {message} → text/event-stream of delta/done/error events.

    Async on purpose: the reader turn runs in-process and tokens stream
    to the browser as they arrive (M3.1). Session auth is checked with
    the async user accessor; staff only, like every ops surface.
    """
    user = await request.auser()
    if not (user.is_authenticated and user.is_staff):
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponseForbidden()
    session = await ChatSession.objects.filter(pk=pk).afirst()
    if session is None:
        raise Http404
    text = request.POST.get("message", "")

    async def _events():
        from apps.reader.services import chat

        try:
            async for kind, payload in chat.run_turn(session, text):
                if kind == "delta":
                    yield _sse("delta", {"text": payload})
                else:
                    yield _sse(kind, payload)
        except Exception:  # never leak a traceback into the stream
            import logging

            logging.getLogger(__name__).exception("chat stream failed")
            yield _sse("error", {"message": "internal error — see server logs"})

    resp = StreamingHttpResponse(_events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # proxies must not buffer the stream
    return resp


@staff_member_required(login_url="login")
@require_http_methods(["GET"])
def chat_message(request, pk: int):
    """Sources + metadata for one message (the panel fetches this after
    `done` so a refresh and the live view render identically)."""
    from django.http import JsonResponse

    m = ChatMessage.objects.filter(pk=pk).select_related("sdk_operation").first()
    if m is None:
        raise Http404
    op = m.sdk_operation
    return JsonResponse(
        {
            "id": m.pk,
            "role": m.role,
            "sources": m.sources,
            "error": m.error,
            "tokens": {
                "input": op.input_tokens if op else None,
                "output": op.output_tokens if op else None,
            },
            "model": op.model if op else "",
            "duration_ms": op.duration_ms if op else None,
        }
    )
