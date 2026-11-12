"""Django urlpatterns declaration for nautobot_agent app."""

from django.db import router as database_router
from django.db import transaction
from django.urls import path
from django.views.decorators.csrf import csrf_protect
from nautobot.apps.urls import NautobotUIViewSetRouter

from nautobot_agent import views
from nautobot_agent.models import AgentRun

app_name = "nautobot_agent"


def _csrf_protected(view):
    """Apply shared CSRF enforcement before the native renderer handles errors."""
    view.csrf_exempt = False
    return csrf_protect(view)


def _run_submit_view():
    """Protect mutation while excluding this callback from ATOMIC_REQUESTS."""
    callback = _csrf_protected(views.AgentRunSubmitView.as_view())
    return transaction.non_atomic_requests(using=database_router.db_for_write(AgentRun))(callback)


urlpatterns = [
    path("agent/", views.AgentView.as_view(), name="agent"),
    path("agent/runs/", _run_submit_view(), name="agent_run_submit"),
    path(
        "agent/runs/<uuid:run_id>/events/",
        views.AgentRunEventsView.as_view(),
        name="agent_run_events",
    ),
    path(
        "agent/runs/<uuid:run_id>/cancel/",
        _csrf_protected(views.AgentRunCancelView.as_view()),
        name="agent_run_cancel",
    ),
    path("agent/state/", views.AgentStateView.as_view(), name="agent_state"),
    path(
        "conversation-history/",
        views.AgentConversationUIViewSet.as_view(
            {"get": "list"},
            basename="agentconversation",
            detail=False,
            suffix="List",
            http_method_names=("get", "head", "options"),
        ),
        name="agentconversation_list",
    ),
    path(
        "all-conversations/",
        views.AllConversationsUIViewSet.as_view(
            {"get": "list"},
            basename="agentconversation",
            detail=False,
            suffix="List",
            custom_view_additional_permissions=["nautobot_agent.viewall_agentconversation"],
            http_method_names=("get", "head", "options"),
        ),
        name="agentconversation_all",
    ),
    path(
        "all-conversations/delete/",
        _csrf_protected(
            views.AllConversationsUIViewSet.as_view(
                {"post": "bulk_destroy"},
                basename="agentconversation",
                detail=False,
                suffix="Bulk Delete",
                custom_view_additional_permissions=["nautobot_agent.viewall_agentconversation"],
                http_method_names=("post", "options"),
            )
        ),
        name="agentconversation_bulk_delete",
    ),
    path(
        "conversation-history/<uuid:pk>/",
        views.AgentConversationUIViewSet.as_view(
            {"get": "retrieve"},
            basename="agentconversation",
            detail=True,
            suffix="Detail",
            http_method_names=("get", "head", "options"),
        ),
        name="agentconversation",
    ),
    path(
        "conversation-history/<uuid:pk>/edit/",
        _csrf_protected(
            views.AgentConversationUIViewSet.as_view(
                {"get": "update", "post": "update"},
                basename="agentconversation",
                detail=True,
                suffix="Edit",
                http_method_names=("get", "post", "head", "options"),
            )
        ),
        name="agentconversation_edit",
    ),
    path(
        "conversation-history/<uuid:pk>/delete/",
        _csrf_protected(
            views.AgentConversationUIViewSet.as_view(
                {"get": "destroy", "post": "destroy"},
                basename="agentconversation",
                detail=True,
                suffix="Delete",
                http_method_names=("get", "post", "head", "options"),
            )
        ),
        name="agentconversation_delete",
    ),
    path(
        "conversation-history/<uuid:pk>/resume/",
        _csrf_protected(
            views.AgentConversationUIViewSet.as_view(
                {"post": "resume"},
                basename="agentconversation",
                detail=True,
                custom_view_base_action="view",
                http_method_names=("post", "options"),
            )
        ),
        name="agentconversation_resume",
    ),
    path(
        "traces/<uuid:pk>/payload/",
        views.AgentExecutionTraceUIViewSet.as_view(
            {"get": "payload"},
            basename="agentexecutiontrace",
            detail=True,
            custom_view_base_action="view",
            suffix="Payload",
            http_method_names=("get", "head"),
        ),
        name="agentexecutiontrace_payload",
    ),
]

router = NautobotUIViewSetRouter()
router.register("traces", views.AgentExecutionTraceUIViewSet, basename="agentexecutiontrace")
router.register("llm-configurations", views.LLMConfigurationUIViewSet, basename="llmconfiguration")
router.register("mcp-servers", views.MCPServerUIViewSet, basename="mcpserver")
router.register("skills", views.SkillUIViewSet, basename="skill")
for route in router.urls:
    # Enforce CSRF before native authorization/rendering on every admin viewset.
    if route.name and route.name.startswith(("skill", "llmconfiguration", "mcpserver")):
        route.callback = _csrf_protected(route.callback)
    urlpatterns.append(route)
