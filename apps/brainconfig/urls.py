"""Ops UI urlconf — everything mounted under the admin-panel prefix.

Settings lives in this app; dashboard + brain browser views live in
apps.brain (they are views over the Entity index) but route through here
so the whole ops surface shares one prefix and one nav.
"""
from django.conf import settings
from django.urls import path

from apps.brain import ops_views
from apps.events import ops_views as events_ops
from apps.feeds import ops_views as feeds_ops
from apps.mind import ops_views as mind_ops
from apps.reader import ops_views as reader_ops
from apps.url_mcp_tokens import ops_views as url_token_ops

from . import health_views, styleguide_views, views

app_name = "brainconfig"

urlpatterns = [
    path("", ops_views.dashboard, name="dashboard"),
    # Shared data source for the brain visuals (rings now; explorer,
    # activity overlay and timeline in M3.5.3-.5).
    path("graph.json", ops_views.graph_json, name="graph-json"),
    path("graph/", ops_views.graph_explorer, name="graph"),
    path("timeline/", ops_views.timeline, name="timeline"),
    # Cursor over the read log — drives the live activity overlay.
    path("activity.json", events_ops.activity_json, name="activity-json"),
    path("brain/", ops_views.browser, name="browser"),
    path("brain/<str:entity_id>/", ops_views.entity_detail, name="entity"),
    path("feeds/", feeds_ops.queue, name="feeds"),
    path("feeds/<int:pk>/", feeds_ops.feed_detail, name="feed-detail"),
    path("tasks/", events_ops.tasks, name="tasks"),
    path("logs/", events_ops.logs, name="logs"),
    path("chat/", reader_ops.chat_home, name="chat"),
    path("chat/<int:pk>/", reader_ops.chat_session, name="chat-session"),
    path("chat/<int:pk>/send", reader_ops.chat_send, name="chat-send"),
    path("chat/message/<int:pk>/", reader_ops.chat_message, name="chat-message"),
    # API keys + their tier/rate-limit profiles. Lives in apps.mind because
    # that app owns Consumer; routed here so the ops surface keeps one
    # prefix and one nav.
    path("consumers/", mind_ops.consumers_page, name="consumers"),
    # The `/mcp/k/<token>/` credentials — claude.ai web + mobile. Its own
    # page, not a tab on API keys: the secret is a URL pasted into someone
    # else's product, which is a different thing to reason about than a
    # bearer key you hold.
    path("connectors/", url_token_ops.connectors_page, name="connectors"),
    path("settings/", views.settings_page, name="settings"),
    # Setup health + the repair actions (M5.3). Separate page rather than a
    # Settings tab: these are *actions on the world* (re-clone, rotate a
    # key), not values in a form.
    path("health/", health_views.health_page, name="health"),
    path("health/jobs.json", health_views.jobs_json, name="health-jobs"),
    path("health/webhook-secret", health_views.reveal_webhook, name="webhook-secret"),
]

# Living style guide for the component layer (M5.6.1). Development only:
# it is a review aid for the design system, not product surface, so it is
# never routed on a deployment. The view re-checks DEBUG itself.
if settings.DEBUG:
    urlpatterns += [
        path("styleguide/", styleguide_views.styleguide, name="styleguide"),
    ]
