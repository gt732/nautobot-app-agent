"""Views for the Nautobot Agent app."""

import json
import logging
import time
import uuid
from typing import Any, cast
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import iri_to_uri
from django.utils.http import url_has_allowed_host_and_scheme
from nautobot.apps.ui import (
    BaseTextPanel,
    Breadcrumbs,
    Button,
    InstanceBreadcrumbItem,
    ObjectDetailContent,
    ObjectFieldsPanel,
    ObjectTextPanel,
    Panel,
    SectionChoices,
    Titles,
    ViewNameBreadcrumbItem,
)
from nautobot.apps.views import (
    GenericView,
    ObjectBulkDestroyViewMixin,
    ObjectDestroyViewMixin,
    ObjectDetailViewMixin,
    ObjectEditViewMixin,
    ObjectListViewMixin,
)
from nautobot.core.utils.requests import convert_querydict_to_dict, normalize_querydict
from nautobot.core.views.renderers import NautobotHTMLRenderer
from nautobot.core.views.utils import get_bulk_queryset_from_view
from rest_framework.response import Response

from nautobot_agent.chat import conversation, run_status
from nautobot_agent.chat import events as run_events
from nautobot_agent.chat import runs as chat_runs
from nautobot_agent.chat.errors import (
    BUSY_MESSAGE,
    RUN_STORAGE_UNAVAILABLE_MESSAGE,
    ConversationBusy,
)
from nautobot_agent.filters import AgentConversationFilterSet, AgentExecutionTraceFilterSet
from nautobot_agent.forms import (
    AgentConversationForm,
    AgentRunSubmissionForm,
    CanonicalUUIDField,
    LLMConfigurationForm,
    MCPServerForm,
    SkillForm,
)
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    LLMConfiguration,
    MCPServer,
    MCPTransport,
    NamedConstraintConflict,
    Skill,
)
from nautobot_agent.runtime.trace_inspector import SPAN_PAYLOAD_FIELDS, inspect_trace
from nautobot_agent.runtime.trace_payloads import json_encoder
from nautobot_agent.tables import (
    AgentConversationTable,
    AgentExecutionTraceTable,
    AllConversationsTable,
    LLMConfigurationTable,
    MCPServerTable,
    SkillTable,
)
from nautobot_agent.tasks import publish_run

logger = logging.getLogger(__name__)

UUID_LOOKUP = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
TRACE_PAYLOAD_FIELDS = {"request"}


def _json_chunks(value):
    """Coalesce JSON tokens into roughly 64 KiB UTF-8 chunks without buffering the entire body."""
    pieces = []
    size = 0
    for piece in json_encoder().iterencode(value):
        encoded = piece.encode("utf-8")
        pieces.append(encoded)
        size += len(encoded)
        if size >= 64 * 1024:
            yield b"".join(pieces)
            pieces.clear()
            size = 0
    if pieces:
        yield b"".join(pieces)


def _htmx_redirect(destination):
    """Return the non-3xx redirect required by Nautobot's shared HTMX handling."""
    response = HttpResponse(status=200)
    response["HX-Redirect"] = destination
    return response


def _span_uuid(value):
    """Parse a requested span identifier; malformed values are not found."""
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise Http404 from error


class AgentView(GenericView):
    """Render the Agent page for authenticated users."""

    def get(self, request, *args, **kwargs):
        """Render the Agent page from the conversation interface."""
        _active_principal(request)
        default_model = LLMConfiguration.objects.filter(is_default=True).values_list("model", flat=True).first()
        if default_model is None:
            request.session.pop(conversation.CONVERSATION_KEY, None)
            state, notice = conversation.restore_state(None)
        else:
            state, notice = conversation.page_state(request)
        cookie_name = conversation.DEPARTURE_COOKIE_NAME
        cookie_path = reverse("plugins:nautobot_agent:agent")
        context = {
            "state": state,
            "notice": notice,
            "llm_configured": default_model is not None,
            "default_model": default_model or "",
            "departure_cookie_name": cookie_name,
            "departure_user_id": request.user.pk,
            "departure_cookie_path": cookie_path,
            "server_now_ms": time.time_ns() // 1_000_000,
        }
        response = render(request, "nautobot_agent/agent.html", context)
        response["Cache-Control"] = "private, no-store"
        response.delete_cookie(cookie_name, path=cookie_path)
        return response


def _run_json(payload: dict, status: int = 200) -> JsonResponse:
    """Return private run data that must not be cached by browsers or intermediaries."""
    response = JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False, "separators": (",", ":")})
    response["Cache-Control"] = "private, no-store"
    return response


def _not_found(message: str) -> JsonResponse:
    """Return a private JSON not-found response."""
    return _run_json({"error": message}, 404)


def _active_principal(request) -> None:
    """Require an active authenticated principal for every internal run endpoint."""
    if not request.user.is_authenticated or not request.user.is_active:
        raise PermissionDenied


def _json_object(request) -> dict | None:
    """Decode a bounded-depth JSON object, rejecting malformed and non-object payloads."""
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_uuid(value: str | None) -> uuid.UUID | None:
    """Parse a canonical UUID string while rejecting non-string selectors."""
    try:
        return CanonicalUUIDField().clean(value)
    except ValidationError:
        return None


def _submission_form(request) -> tuple[AgentRunSubmissionForm | None, JsonResponse | None]:
    """Return a validated submission form or its stable client-error response."""
    payload = _json_object(request)
    if payload is None:
        return None, _run_json({"error": "Invalid request body."}, 400)
    form = AgentRunSubmissionForm(payload)
    if not form.is_valid():
        return None, _run_json({"error": "Invalid request fields.", "details": form.errors.get_json_data()}, 400)
    return form, None


def _state_selector(request) -> tuple[str | None, str | None, JsonResponse | None]:
    """Require one state selector and return its original type and value."""
    conversations = request.GET.getlist("conversation")
    requests = request.GET.getlist("client_request")
    if len(conversations) + len(requests) != 1:
        return None, None, _run_json({"error": "Provide exactly one state selector."}, 400)
    if conversations:
        return "conversation", conversations[0], None
    return "client_request", requests[0], None


class AgentRunSubmitView(GenericView):
    """Admit a durable run and return its stable identity without executing a graph."""

    def post(self, request, *args, **kwargs):  # pylint: disable=too-many-return-statements
        """Normalize input, commit admission, and report the resolved publication outcome."""
        _active_principal(request)
        form, error = _submission_form(request)
        if error:
            return error
        form = cast(AgentRunSubmissionForm, form)
        try:
            result = chat_runs.submit_run(
                request.user,
                form.cleaned_data["client_request_id"],
                form.cleaned_data["message"],
                form.cleaned_data["conversation_id"],
                publish_run,
            )
        except (DatabaseError, ImproperlyConfigured):
            return _run_json({"error": RUN_STORAGE_UNAVAILABLE_MESSAGE}, 503)
        if result.kind == chat_runs.AdmissionKind.CONVERSATION_NOT_FOUND:
            return _not_found("Conversation not found.")
        if result.kind == chat_runs.AdmissionKind.USER_LIMIT:
            return _run_json(
                {
                    "error": "You already have two active runs. Wait for one to finish or cancel it before sending.",
                    "code": "user_run_limit",
                },
                409,
            )
        if result.kind == chat_runs.AdmissionKind.BUSY:
            return _run_json(
                {
                    "error": "Conversation already has an active run.",
                    "blocking_run": run_status.run_summary(cast(AgentRun, result.blocker)),
                },
                409,
            )
        run = cast(AgentRun, result.run)
        duplicate = result.kind == chat_runs.AdmissionKind.DUPLICATE
        summary = run_status.run_summary(run)
        if run.status == AgentRunStatus.DISPATCH_FAILED and not duplicate:
            return _run_json(summary, 503)
        conversation.select(request.session, run.conversation_id)
        return _run_json(summary, 200 if duplicate else 202)


class AgentRunEventsView(GenericView):
    """Read one bounded, nonblocking page of disposable events for an owned run."""

    def get(self, request, run_id, *args, **kwargs):  # pylint: disable=too-many-return-statements
        """Return bounded delivery alongside the database-owned status."""
        _active_principal(request)
        try:
            run = run_status.reconcile_owned_run(request.user, pk=run_id)
        except DatabaseError:
            return _run_json({"error": RUN_STORAGE_UNAVAILABLE_MESSAGE}, 503)
        if run is None:
            return _not_found("Run not found.")
        if request.GET.get("status_only") == "1":
            return _run_json(run_status.run_summary(run))
        after_values = request.GET.getlist("after")
        after = after_values[0] if len(after_values) == 1 else "0-0"
        if len(after_values) > 1:
            return _run_json({"error": "Invalid event cursor."}, 400)
        try:
            page = run_events.read_run_events(run.pk, after)  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.
        except run_events.InvalidCursor:
            return _run_json({"error": "Invalid event cursor."}, 400)
        except run_events.EventReadUnavailable:
            return _run_json({"error": "Live events are temporarily unavailable."}, 503)
        return _run_json(run_status.delivery_payload(run, page))


class AgentRunCancelView(GenericView):
    """Request safe queued cancellation or cooperative running shutdown."""

    def post(self, request, run_id, *args, **kwargs):
        """Apply owner-only cancellation behind Django's standard CSRF middleware."""
        _active_principal(request)
        try:
            run = chat_runs.request_cancel(run_id, request.user)
        except DatabaseError:
            return _run_json({"error": RUN_STORAGE_UNAVAILABLE_MESSAGE}, 503)
        if run is None:
            return _not_found("Run not found.")
        summary = run_status.run_summary(run)
        return _run_json(summary, 202 if run.status == AgentRunStatus.RUNNING else 200)


class AgentStateView(GenericView):
    """Restore model-free rich state or resolve an owned client request identity."""

    def get(self, request, *args, **kwargs):  # pylint: disable=too-many-return-statements
        """Resolve exactly one owned selector without constructing an executable graph."""
        _active_principal(request)
        selector, value, error = _state_selector(request)
        if error:
            return error
        if selector == "conversation":
            try:
                state, notice = conversation.conversation_state(request, cast(str, value))
            except ValidationError:
                return _run_json({"error": "Invalid conversation UUID."}, 400)
            except Http404:
                return _not_found("Conversation not found.")
            except DatabaseError:
                return _run_json({"error": "Conversation state is unavailable."}, 503)
            if notice:
                return _run_json({"error": "Conversation state is unavailable."}, 503)
            return _run_json(state)
        request_id = _canonical_uuid(value)
        if request_id is None:
            return _run_json({"error": "Invalid client_request UUID."}, 400)
        try:
            run = run_status.reconcile_owned_run(request.user, client_request_id=request_id)
        except DatabaseError:
            return _run_json({"error": RUN_STORAGE_UNAVAILABLE_MESSAGE}, 503)
        if run is None:
            return _run_json({"run_id": None})
        return _run_json(run_status.run_summary(run))


class _OwnerResumeButton(Button):
    """Render the Resume action only for the conversation owner."""

    def should_render(self, context):
        """Keep the resume control hidden for conversations owned by another user."""
        return super().should_render(context) and context["object"].user_id == context["request"].user.pk

    def get_extra_context(self, context):
        """Use the reversed resume URL as the HTMX POST target."""
        extra_context = super().get_extra_context(context)
        extra_context["attributes"] = {
            "hx-post": extra_context["link"],
            "hx-swap": "none",
            "hx-params": "none",
        }
        return extra_context


class AgentConversationUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    ObjectDetailViewMixin,
    ObjectListViewMixin,
    ObjectEditViewMixin,
    ObjectDestroyViewMixin,
):
    """Native conversation list, detail, rename, delete, and owner-only resume views."""

    queryset = AgentConversation.objects.all()
    table_class = AgentConversationTable
    filterset_class = AgentConversationFilterSet
    form_class = AgentConversationForm
    action_buttons = ()
    show_all_conversations = False
    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                section=SectionChoices.LEFT_HALF,
                weight=100,
                fields=["user", "title", "created_at", "last_activity_at"],
            ),
            Panel(
                section=SectionChoices.FULL_WIDTH,
                weight=200,
                label="Conversation",
                body_content_template_path="nautobot_agent/inc/agentconversation_transcript.html",
            ),
        ),
        extra_buttons=(
            _OwnerResumeButton(
                label="Resume",
                weight=100,
                link_name="plugins:nautobot_agent:agentconversation_resume",
            ),
        ),
    )
    breadcrumbs = Breadcrumbs(
        items={
            "retrieve": [
                ViewNameBreadcrumbItem(
                    view_name="plugins:nautobot_agent:agentconversation_list",
                    label="Conversation History",
                ),
                InstanceBreadcrumbItem(),
            ],
        },
    )
    view_titles = Titles(
        titles={
            "list": "Conversation History",
            "retrieve": "{{ object }}",
        }
    )

    def get_queryset(self):
        """Apply owner-safe visibility to reads while preserving native mutation grants."""
        queryset = super().get_queryset().select_related("user")
        if self.action == "resume" or (self.action == "list" and not self.show_all_conversations):
            queryset = queryset.owner_history(self.request.user)
        elif self.action not in ("update", "destroy"):
            queryset = queryset.readable_by(self.request.user)
        if self.action == "list":
            queryset = queryset.with_trace_counts(self.request.user)
        return queryset

    def get_extra_context(self, request, instance=None):
        """Supply the read-only transcript to the detail panels."""
        context = super().get_extra_context(request, instance)
        if self.action == "list" and not self.show_all_conversations:
            permissions = NautobotHTMLRenderer().construct_user_permissions(request, AgentConversation)
            permissions["delete"] = False
            context["permissions"] = permissions
        if self.action == "retrieve" and instance is not None:
            context["transcript"] = conversation.transcript(instance)
        return context

    def retrieve(self, request, *args, **kwargs):
        """Prevent browser storage of the saved conversation transcript."""
        response = super().retrieve(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def list(self, request, *args, **kwargs):
        """Reject native export requests that could expose unapproved model fields."""
        if "export" in request.GET:
            raise Http404
        return super().list(request, *args, **kwargs)

    def resume(self, request, *args, **kwargs):
        """Resume a native owner-resolved row and return a non-redirect HTMX response."""
        target = self.get_object()
        if LLMConfiguration.objects.filter(is_default=True).exists():
            destination = reverse("plugins:nautobot_agent:agent") + f"?conversation={target.pk}"
        else:
            messages.error(request, "Configure a default language model before resuming a conversation.")
            destination = reverse("plugins:nautobot_agent:agentconversation_list")
        if request.headers.get("HX-Request") == "true":
            return _htmx_redirect(destination)
        return redirect(destination)

    def _process_destroy_form(self, form):
        """Delete through the reservation-aware lifecycle and retain native messages and return URLs."""
        del form
        request = self.request
        obj = self.obj
        history_url = reverse("plugins:nautobot_agent:agentconversation_list")
        try:
            conversation.delete(request, obj.pk)
        except ConversationBusy:
            messages.error(request, BUSY_MESSAGE)
        else:
            msg = f"Deleted {self.get_queryset().model._meta.verbose_name} {obj}"
            self.logger.info("Deleted conversation %s", obj.pk)
            messages.success(request, msg)
            self.success_url = self.get_return_url(
                request,
                default_return_url="plugins:nautobot_agent:agentconversation_list",
            )
            return
        self.success_url = history_url

    def form_valid(self, form):
        """Use a non-3xx HTMX redirect after a valid deletion confirmation."""
        response = super().form_valid(form)
        if (
            self.action == "destroy"
            and self.request.headers.get("HX-Request") == "true"
            and response.status_code == 302
        ):
            return _htmx_redirect(self.success_url)
        return response


class AllConversationsUIViewSet(AgentConversationUIViewSet, ObjectBulkDestroyViewMixin):  # pylint: disable=abstract-method
    """Reuse native history rendering with explicit cross-user read access."""

    show_all_conversations = True
    table_class = AllConversationsTable
    view_titles = Titles(titles={"list": "All Conversations"})
    pk_list: list[str]
    key_params: dict[str, Any]

    def get_return_url(self, request, obj=None, default_return_url=None):
        """Keep bulk selection, confirmation cancel, and completion on All Conversations."""
        if self.action != "bulk_destroy":
            return super().get_return_url(request, obj, default_return_url)

        list_url = reverse("plugins:nautobot_agent:agentconversation_all")
        supplied_url = request.GET.get("return_url") or request.POST.get("return_url")
        if supplied_url and url_has_allowed_host_and_scheme(
            supplied_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            supplied_parts = urlsplit(supplied_url)
            if supplied_parts.path == urlsplit(list_url).path:
                return iri_to_uri(supplied_url)

        query = request.GET.copy()
        query.pop("return_url", None)
        return f"{list_url}?{query.urlencode()}" if query else list_url

    def _get_bulk_delete_queryset(self, request):
        """Apply both native delete selection and the viewset's authorized view scope."""
        queryset = get_bulk_queryset_from_view(user=request.user, action="delete", **self.key_params)
        return queryset.filter(pk__in=self.get_queryset().values("pk"))

    def perform_bulk_destroy(self, request, **kwargs):  # pylint:disable=arguments-renamed
        """Retain native confirmation orchestration; 3.1's resolver bypasses the view's queryset scope."""
        queryset = self.get_queryset()
        model = queryset.model
        self.pk_list = list(request.POST.getlist("pk"))
        delete_all = bool(request.POST.get("_all"))
        saved_view_id = request.GET.get("saved_view", "")
        self.key_params = {
            "content_type": ContentType.objects.get_for_model(model),
            "delete_all": delete_all,
            "filter_query_params": convert_querydict_to_dict(request.GET),
            "pk_list": self.pk_list,
            "saved_view_id": saved_view_id,
        }
        queryset = self._get_bulk_delete_queryset(request)

        if "_confirm" in request.POST:
            form_class = self.get_form_class(**kwargs)
            form = form_class(request.POST, initial=normalize_querydict(request.GET, form_class=form_class))
            if form.is_valid():
                return self.send_bulk_delete_objects_to_job(request)
            return self.form_invalid(form)

        table = None
        if not delete_all:
            table_class = self.get_table_class()
            table = table_class(queryset, orderable=False)
            if not table.rows:
                messages.warning(
                    request,
                    f"No {queryset.model._meta.verbose_name_plural} were selected for deletion.",
                )
                return redirect(self.get_return_url(request))
            if "actions" in table.columns:
                table.columns.hide("actions")

        return Response(
            {
                "table": table,
                "total_objs_to_delete": queryset.count(),
                "delete_all": delete_all,
            }
        )

    def send_bulk_delete_objects_to_job(self, request):
        """Run native-confirmed candidates through guarded deletion instead of enqueuing Nautobot's generic Job."""
        candidates = self._get_bulk_delete_queryset(request)
        candidate_ids = list(candidates.values_list("pk", flat=True))
        deleted_count, skipped_messages = conversation.delete_many(request, candidate_ids)
        skipped_count = len(candidate_ids) - deleted_count

        result_message = (
            f"Deleted {deleted_count} conversations; skipped {skipped_count} busy or unavailable conversations."
        )
        if skipped_messages:
            result_message += " " + " ".join(skipped_messages)
        self.logger.info(result_message)
        messages.success(request, result_message)
        destination = self.get_return_url(request)
        if request.headers.get("HX-Request") == "true":
            return _htmx_redirect(destination)
        return redirect(destination)


class AgentExecutionTraceUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    ObjectDetailViewMixin,
    ObjectListViewMixin,
):
    """Native read-only trace summary and protected detail inspector."""

    action_map: dict[str, str]
    queryset = AgentExecutionTrace.objects.select_related("conversation__user").defer("request")
    table_class = AgentExecutionTraceTable
    filterset_class = AgentExecutionTraceFilterSet
    lookup_value_regex = UUID_LOOKUP
    action_buttons = ()
    breadcrumbs = Breadcrumbs(
        items={
            "retrieve": [
                ViewNameBreadcrumbItem(view_name="plugins:nautobot_agent:agentexecutiontrace_list", label="Traces"),
                InstanceBreadcrumbItem(
                    instance_key="trace_conversation",
                    should_render=lambda context: context.get("trace_conversation") is not None,
                ),
                InstanceBreadcrumbItem(
                    instance_key=None,
                    label=lambda context: (
                        f"Turn {context['position']['number']}" if context.get("position") else str(context["trace"].pk)
                    ),
                ),
            ],
        },
    )
    view_titles = Titles(titles={"retrieve": "Agent Trace"})

    def dispatch(self, request, *args, **kwargs):
        """Apply payload privacy even to native lookup and pre-dispatch permission errors."""
        payload_route = self.action_map.get("get") == "payload"
        try:
            response = super().dispatch(request, *args, **kwargs)
        except Exception as error:
            if not payload_route:
                raise
            if isinstance(error, Http404):
                response = HttpResponse(status=404)
            elif isinstance(error, PermissionDenied):
                response = HttpResponse(status=403)
            else:
                logger.error("Trace JSON payload request failed (%s).", type(error).__name__)
                response = HttpResponse(status=500)
        if payload_route:
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
        return response

    def handle_exception(self, exc):
        """Prevent trace detail and payload errors from entering shared caches."""
        response = super().handle_exception(exc)
        if self.action_map.get("get") == "payload":
            # Native detail error rendering repeats get_object(), which can raise after dispatch.
            response = HttpResponse(status=response.status_code, headers=dict(response.headers))
        if getattr(self, "action", None) == "retrieve":
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
        return response

    def get_queryset(self):
        """Add global turn ranks and list-only summary projections while retaining deferred payloads."""
        queryset = super().get_queryset()
        if self.action == "list":
            return queryset.with_summary_projections(self.request.user)
        if self.action == "retrieve":
            return queryset.with_turn_number()
        return queryset

    def payload(self, request, *args, **kwargs):
        """Load only the authorized ORM value, then stream JSON encoding.

        Streaming avoids another complete response string; JSONField retrieval still loads its value
        into process memory and this action does not promise a memory bound.
        """
        trace = self.get_object()
        if not AgentExecutionTrace.full_inspection_allowed(request.user):
            raise PermissionDenied

        params = request.GET
        if set(params) - {"field", "span", "download"} or any(len(params.getlist(name)) != 1 for name in params):
            raise Http404
        field = params.get("field")
        download = params.get("download")
        if download not in (None, "1"):
            raise Http404
        if field in TRACE_PAYLOAD_FIELDS and "span" not in params:
            value = getattr(trace, field)
            filename = f"trace-{trace.pk}-{field}"
        elif field in SPAN_PAYLOAD_FIELDS and "span" in params:
            span_id = _span_uuid(params["span"])
            value = (
                AgentExecutionSpan.objects.filter(trace_id=trace.pk, run_id=span_id)
                .values_list(SPAN_PAYLOAD_FIELDS[field], flat=True)
                .first()
            )
            if value is None:
                raise Http404
            filename = f"trace-{trace.pk}-span-{span_id}-{field}"
        else:
            raise Http404
        # JsonResponse would eagerly materialize the entire response string.
        response = StreamingHttpResponse(  # pylint: disable=http-response-with-content-type-json
            _json_chunks(value), content_type="application/json"
        )
        response["Content-Disposition"] = f'{"attachment" if download == "1" else "inline"}; filename="{filename}.json"'
        return response

    def retrieve(self, request, *args, **kwargs):
        """Render a summary or the authorized full inspector from the native trace object."""
        trace = self.get_object()

        full_access = AgentExecutionTrace.full_inspection_allowed(request.user)
        if not full_access and "span" in request.GET:
            raise PermissionDenied
        run_status.reconcile_trace_runs([trace])
        position = trace.conversation_position(AgentExecutionTrace.objects.restrict(request.user, "view"))
        trace_conversation = (
            AgentConversation.objects.readable_by(request.user).filter(pk=trace.conversation_id).first()
        )
        context = {
            "object": trace,
            "trace": trace,
            "position": position,
            "trace_conversation": trace_conversation,
            "full_access": full_access,
            "include_tree": request.headers.get("HX-Request") != "true",
        }
        if full_access:
            span_value = request.GET.get("span")
            span_id = None if span_value is None else _span_uuid(span_value)
            if len(request.GET.getlist("span")) > 1:
                raise Http404
            try:
                context.update(inspect_trace(trace, span_id, include_tree=context["include_tree"]))
            except LookupError as error:
                raise Http404 from error
        response = Response({"template": "nautobot_agent/agentexecutiontrace_retrieve.html", **context})
        response["Cache-Control"] = "private, no-store"
        return response

    def list(self, request, *args, **kwargs):
        """Reject native exports so no unapproved trace fields can be returned."""
        if "export" in request.GET:
            raise Http404
        return super().list(request, *args, **kwargs)


class NamedConstraintFormMixin:  # pylint: disable=too-few-public-methods
    """Render known named-constraint conflicts through the native bound form."""

    def _process_create_or_update_form(self, form):
        try:
            # Native edit mixins supply this cooperative hook.
            return super()._process_create_or_update_form(form)  # ty: ignore[unresolved-attribute]
        except NamedConstraintConflict as error:
            form.add_error(None, error)
            raise


# CRUD-only composition deliberately omits the native abstract bulk-action hooks.
class LLMConfigurationUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    NamedConstraintFormMixin,
    ObjectDetailViewMixin,
    ObjectListViewMixin,
    ObjectEditViewMixin,
    ObjectDestroyViewMixin,
):
    """Native list, detail, create, update, and delete views for LLM configurations."""

    def _process_create_or_update_form(self, form):
        """Treat provider configuration as deployment-credential access, including ambient credentials."""
        if not self.request.user.is_superuser:
            raise PermissionDenied("Only superusers may save LLM configurations.")
        return super()._process_create_or_update_form(form)

    queryset = LLMConfiguration.objects.all()
    form_class = LLMConfigurationForm
    table_class = LLMConfigurationTable
    lookup_value_regex = UUID_LOOKUP
    action_buttons = ("add",)
    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                section=SectionChoices.LEFT_HALF,
                weight=100,
                fields=["name", "model", "credential_profile", "parameters", "is_default", "is_scope_classifier"],
            ),
        ),
    )


class MCPServerUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    NamedConstraintFormMixin,
    ObjectDetailViewMixin,
    ObjectListViewMixin,
    ObjectEditViewMixin,
    ObjectDestroyViewMixin,
):
    """Native list, detail, create, update, and delete views for MCP servers."""

    def _process_create_or_update_form(self, form):
        """Protect both the original and submitted worker/credential trust boundaries."""
        if not self.request.user.is_superuser:
            # Query parameters can override form initial values; reload the persisted configuration.
            original = self.get_object()
            if (
                original.transport == MCPTransport.STDIO
                or form.instance.transport == MCPTransport.STDIO
                or original.credential_profile
                or form.instance.credential_profile
            ):
                raise PermissionDenied("Only superusers may save stdio or credential-bearing MCP servers.")
        return super()._process_create_or_update_form(form)

    queryset = MCPServer.objects.all()
    form_class = MCPServerForm
    table_class = MCPServerTable
    lookup_value_regex = UUID_LOOKUP
    action_buttons = ("add",)
    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                section=SectionChoices.LEFT_HALF,
                weight=100,
                fields=[
                    "name",
                    "transport",
                    "url",
                    "launch",
                    "credential_profile",
                    "enabled",
                ],
            ),
        ),
    )


class SkillUIViewSet(  # pylint: disable=too-many-ancestors,abstract-method
    NamedConstraintFormMixin,
    ObjectDetailViewMixin,
    ObjectListViewMixin,
    ObjectEditViewMixin,
    ObjectDestroyViewMixin,
):
    """Native list, detail, create, update, and delete views for skills."""

    queryset = Skill.objects.all().defer("instructions")
    form_class = SkillForm
    table_class = SkillTable
    lookup_value_regex = UUID_LOOKUP
    action_buttons = ("add",)
    view_titles = Titles(titles={"list": "Skills"})
    object_detail_content = ObjectDetailContent(
        panels=(
            ObjectFieldsPanel(
                section=SectionChoices.LEFT_HALF,
                weight=100,
                fields=["name", "identifier", "description", "enabled", "last_updated"],
            ),
            ObjectTextPanel(
                label="Instructions",
                section=SectionChoices.FULL_WIDTH,
                weight=200,
                object_field="instructions",
                render_as=BaseTextPanel.RenderOptions.CODE,
            ),
        ),
    )
