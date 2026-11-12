"""Production agent initialization and context-management regressions."""

import json
from typing import Any, ClassVar
from unittest.mock import PropertyMock, patch

from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.urls import reverse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.chat.history import transcript_entries
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import AgentConversation, AgentRun, AgentRunStatus, Skill, TraceCaptureState
from nautobot_agent.runtime.agent import AgentRuntime
from nautobot_agent.runtime.checkpointer import thread_config
from nautobot_agent.runtime.errors import ModelFailure
from nautobot_agent.runtime.scope import project_scope_history
from nautobot_agent.sources.backends import SourceCatalog
from nautobot_agent.tests.support.chat import (
    MAIN_TOOLS,
    ScriptedToolModel,
    create_configuration,
    enable_agent_settings,
    override_agent_settings,
    page_json,
    parse_run_events,
    post_run_message,
    provider_usage,
    run_dependencies,
    runtime_graph,
    select_conversation,
    tool_call_message,
)
from nautobot_agent.tests.support.graph import SourceGraphTestCase
from nautobot_agent.tests.support.mcp import Host
from nautobot_agent.tests.support.sources import APP_MODELS_LINES, MODELS_LINES, create_app_tree, patched_app_discovery


class SourceContextManagementTestCase(SourceGraphTestCase):  # pylint: disable=too-many-ancestors,nb-incorrect-base-class
    """Source tools use executor connections; HTTP regressions require outermost durable admission."""

    def test_missing_and_invalid_profile_boundaries_fail_before_source_or_mcp_assembly(self):
        create_configuration()
        invalid_profiles = (
            None,
            "",
            [],
            {},
            {"other": 128000},
            {"max_input_tokens": 0},
            {"max_input_tokens": -1},
            {"max_input_tokens": True},
            {"max_input_tokens": False},
            {"max_input_tokens": 128000.0},
            {"max_input_tokens": "128000"},
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                model = ScriptedToolModel(responses=[])
                model.profile = profile  # ty: ignore[invalid-assignment]  # Deliberately invalid provider metadata.
                runtime = AgentRuntime(
                    saver=InMemorySaver(),
                    model_factory=lambda _configuration, model=model: model,
                    sources=SourceCatalog(),
                )
                turn = MCPTurn(host=Host())
                self.addCleanup(turn.close)
                with (
                    patch(
                        "nautobot_agent.runtime.agent.SourceBackend",
                        side_effect=AssertionError("source assembly must not run"),
                    ),
                    patch.object(
                        runtime.mcp_tool_cache,
                        "get",
                        side_effect=AssertionError("MCP assembly must not run"),
                    ),
                    self.assertRaises(ModelFailure) as caught,
                ):
                    runtime.graph(self.user, mcp_turn=turn)
                self.assertEqual(caught.exception.code, "model_context_limit_missing")
                self.assertEqual(model.requests, [])

    def test_provider_refusal_fails_safely_and_conversation_can_continue(self):
        secret = "PROVIDER-REFUSAL-SENTINEL"
        graph = self.start_graph(
            [
                AIMessage(content=[{"type": "refusal", "refusal": secret}]),
                AIMessage(content="The next request completed."),
            ]
        )

        events = parse_run_events(self.send_response("First request"))

        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("done", [name for name, _payload in events])
        self.assertNotIn(secret, json.dumps(events))
        conversation = AgentConversation.objects.get(pk=self.client.session[CONVERSATION_KEY])
        run = conversation.runs.get()
        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertEqual(run.error_code, "model_refused")
        self.assertFalse(conversation.runs.active().exists())
        retained = self.saved_messages(graph)
        self.assertFalse(any(isinstance(message, AIMessage) for message in retained))
        self.assertEqual(self.send_message("Second request"), ["The next request completed.", "done"])
        self.assertNotIn(secret, self.scripted_model.request_text())
        self.scripted_model.responses = [
            tool_call_message(
                "task",
                {"description": "Inspect the request.", "subagent_type": "general-purpose"},
                "refused-delegate",
            ),
            AIMessage(content=[{"type": "refusal", "refusal": "Provider safety refusal."}]),
            AIMessage(content="Continued without the delegate result."),
        ]
        state, _config = self.invoke(graph)
        result = self.tool_results(state["messages"])["refused-delegate"]
        self.assertIn("model refused", result.text)
        self.assertEqual(state["messages"][-1].text, "Continued without the delegate result.")

    COMPACTION_SCRIPT_READS = ("/nautobot/__init__.py", "/nautobot/models.py", "/nautobot/core/views.py")

    FORCED_DEFAULTS: ClassVar[dict[str, Any]] = {
        "trigger": ("messages", 6),
        "keep": ("messages", 2),
        "truncate_args_settings": {"trigger": ("messages", 40), "keep": ("messages", 40)},
    }

    def compaction_script(self, after_summary):
        """Return three source reads, the summarizer's answer, then the given call and a final answer."""
        reads = [
            tool_call_message("read_file", {"file_path": path, "limit": 5}, f"early-{index}")
            for index, path in enumerate(self.COMPACTION_SCRIPT_READS)
        ]
        return [
            *reads,
            AIMessage(content="SUMMARY-OF-EARLIER-SOURCE-READS"),
            after_summary,
            AIMessage(content="Done."),
        ]

    def test_forced_production_summarization_has_no_recovery_pointer_and_source_stays_readable(self):
        self.enterContext(
            patch("nautobot_agent.runtime.agent.compute_summarization_defaults", return_value=self.FORCED_DEFAULTS)
        )
        after = tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 2}, "after-summary")
        graph = self.start_graph(self.compaction_script(after))

        state, config = self.invoke(graph)

        self.assertEqual(len(self.scripted_model.calls_with_tools(None)), 1)
        main_requests = self.scripted_model.calls_with_tools(MAIN_TOOLS)
        self.assertEqual(len(main_requests), 5)
        compacted = main_requests[3]
        assert isinstance(compacted[1], HumanMessage)
        summary = compacted[1]
        self.assertEqual(summary.additional_kwargs.get("lc_source"), "summarization")
        self.assertNotIn("Question", [message.text for message in compacted])
        for request in main_requests:
            for message in request:
                self.assertNotIn("conversation_history", message.text)
        result = self.tool_results(main_requests[4])["after-summary"]
        self.assertEqual(result.status, "success")
        self.assertIn(MODELS_LINES[0], result.text)
        self.assertEqual(state.get("files", {}), {})
        self.assertEqual(state["messages"][-1].text, "Done.")
        self.assert_nothing_outside_reached_the_model()
        self.assert_tool_surface()

        # Standard compaction replaces restored page history; retained calls remain visible between summary and
        # answer, removed ones do not, and no extra transcript storage is maintained.
        select_conversation(self.client, AgentConversation.objects.get(pk=config.get("configurable", {})["thread_id"]))
        response = self.client.get(reverse("plugins:nautobot_agent:agent"))
        retained_calls = [
            (message, call)
            for message in state["messages"]
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        ]
        self.assertEqual(retained_calls[-1][1]["id"], "after-summary")
        self.assertNotIn("early-0", [call["id"] for _, call in retained_calls])
        history = page_json(response, "agent-state")["history"]
        self.assertEqual(
            history,
            [
                *(
                    {
                        "role": "tool",
                        "id": json.dumps([message.id, "tool", call["id"]], separators=(",", ":")),
                        "name": call["name"],
                        "args": call["args"],
                        "status": "completed",
                    }
                    for message, call in retained_calls
                ),
                {"role": "assistant", "content": state["messages"][-1].text},
            ],
        )
        self.assertNotIn(summary.text, [entry["content"] for entry in transcript_entries(state["messages"])])
        self.assertNotIn(summary.text, [entry["content"] for entry in project_scope_history(state["messages"])])
        self.assertEqual(history[-2]["args"], {"file_path": "/nautobot/models.py", "limit": 2})
        self.assertNotIn(MODELS_LINES[0], json.dumps(history))

    def test_summarization_exhaustion_fails_durably_with_complete_capture(self):
        self.enterContext(
            patch("nautobot_agent.runtime.agent.compute_summarization_defaults", return_value=self.FORCED_DEFAULTS)
        )
        after = tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 2}, "after-summary")
        self.start_graph(self.compaction_script(after))
        with override_agent_settings(chat_max_model_calls=3, tracing=True):
            response = self.send_response("Question")
        self.assertEqual(response.json()["status"], AgentRunStatus.FAILED)
        run = AgentRun.objects.get(user=self.user)
        self.assertEqual(run.error_code, "run_model_call_limit")
        self.assertEqual(run.prompt, "")
        self.assertEqual(
            run.trace.capture_state,
            TraceCaptureState.COMPLETE,
            list(run.trace.spans.filter(ended_at__isnull=False).values_list("kind", "name", "status")),
        )
        self.assertEqual(run.trace.spans.filter(kind="chat_model").count(), 3)
        self.assertEqual(len(self.scripted_model.requests), 3)

    def test_skill_configuration_failures_block_graph_admission_safely(self):
        self.start_graph([])
        with (
            patch(
                "nautobot_agent.models.Skill.objects.runtime_snapshot",
                side_effect=DatabaseError("SQL-SECRET-SENTINEL"),
            ),
            self.assertRaises(DatabaseError),
        ):
            runtime_graph(self.user)

        self.client.force_login(self.user)
        with (
            patch(
                "nautobot_agent.models.Skill.objects.runtime_snapshot",
                side_effect=DatabaseError("SQL-SECRET-SENTINEL"),
            ),
            run_dependencies(),
            self.assertLogs("nautobot_agent.chat.execution", level="WARNING") as logs,
        ):
            response = post_run_message(self.client, "Do not admit this send.")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error_code"], "DatabaseError")
        self.assertNotIn("SQL-SECRET-SENTINEL", response.content.decode())
        diagnostic = "\n".join(logs.output)
        self.assertIn("DatabaseError", diagnostic)
        self.assertIn("nautobot_agent.runtime.agent.graph:", diagnostic)
        self.assertNotIn("SQL-SECRET-SENTINEL", diagnostic)
        self.assertNotIn(__file__, diagnostic)

        Skill.objects.bulk_create(
            [
                Skill(
                    name="Corrupt enabled row",
                    identifier="corrupt",
                    description="Valid description",
                    instructions="   ",
                    enabled=True,
                )
            ]
        )
        with self.assertRaises(ImproperlyConfigured) as invalid:
            runtime_graph(self.user)
        self.assertNotIn("Corrupt enabled row", str(invalid.exception))
        with (
            run_dependencies(),
            self.assertLogs("nautobot_agent.chat.execution", level="WARNING") as logs,
        ):
            response = post_run_message(self.client, "Do not execute with invalid skills.")
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error_code"], "execution_unavailable")
        diagnostic = "\n".join(logs.output)
        self.assertIn("ImproperlyConfigured", diagnostic)
        self.assertIn("nautobot_agent.runtime.agent.graph:", diagnostic)
        self.assertNotIn("Corrupt enabled row", diagnostic)
        self.assertNotIn(__file__, diagnostic)

    def test_reduced_grants_preserve_retained_source_results_but_deny_new_reads_over_http(self):
        app_tree = create_app_tree(self.tree)
        app_sources = {"nautobot_agent": SourceCatalog().apps["nautobot_agent"]}
        with patched_app_discovery(self, app_tree):
            app_sources.update(SourceCatalog().apps)
        group = Group.objects.create(name="HTTP App Readers")
        self.user.groups.add(group)
        enable_agent_settings(self, source_apps_by_group={group.name: ["enabled_app"]})
        graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "before-reduction"),
                AIMessage(content="Earlier App source was read."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "after-reduction"),
                tool_call_message("read_file", {"file_path": "/skills/nautobot-charts/SKILL.md"}, "chart-skill"),
                tool_call_message("read_file", {"file_path": "/skills/nautobot-mermaid/SKILL.md"}, "mermaid-skill"),
                tool_call_message(
                    "read_file", {"file_path": "/apps/nautobot_agent/__init__.py"}, "agent-implementation"
                ),
                AIMessage(content="Reduced-grant reply completed."),
            ],
            app_source_backends=app_sources,
        )
        page_url = reverse("plugins:nautobot_agent:agent")
        self.assertEqual(self.client.get(page_url).status_code, 200)
        self.send_message("Read the enabled App source.")

        thread_id = self.client.session[CONVERSATION_KEY]
        config = thread_config(thread_id)
        earlier = self.tool_results(graph.get_state(config).values["messages"])["before-reduction"]
        self.assertEqual(earlier.status, "success")
        self.assertIn(APP_MODELS_LINES[0], earlier.text)
        old_catalog = [
            {
                "path": f"/skills/{name}/SKILL.md",
                "name": name,
                "description": "Previously retained skill metadata.",
            }
            for name in ("nautobot-charts", "nautobot-mermaid")
        ]
        graph.update_state(config, {"skills_metadata": old_catalog})

        self.add_permissions("nautobot_agent.view_agentconversation")
        self.user.groups.remove(group)
        self.send_message("Use the default skills and retry the App read.")

        current_state = runtime_graph(self.user).get_state(config).values
        results = self.tool_results(current_state["messages"])
        self.assertEqual(results["before-reduction"].status, "success")
        self.assertIn(APP_MODELS_LINES[0], results["before-reduction"].text)
        self.assert_denied(results["after-reduction"])
        self.assertEqual(
            {name: results[name].status for name in ("chart-skill", "mermaid-skill")},
            {"chart-skill": "success", "mermaid-skill": "success"},
        )
        self.assertIn("render_chart", results["chart-skill"].text)
        self.assertIn("sequenceDiagram", results["mermaid-skill"].text)
        self.assert_denied(results["agent-implementation"])
        paths = {entry["path"] for entry in current_state["skills_metadata"]}
        self.assertIn("/skills/nautobot-charts/SKILL.md", paths)
        self.assertIn("/skills/nautobot-mermaid/SKILL.md", paths)
        self.assertTrue(all(path.startswith("/skills/") for path in paths))

    def test_forced_compaction_reduces_live_and_reloaded_usage(self):
        defaults = {
            "trigger": ("messages", 8),
            "keep": ("messages", 2),
            "truncate_args_settings": {"trigger": ("messages", 40), "keep": ("messages", 40)},
        }
        script = [
            AIMessage(content="First answer", usage_metadata=provider_usage(900000, 100000, 1)),
            tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 1}, "early"),
            tool_call_message("read_file", {"file_path": "/nautobot/core/views.py", "limit": 1}, "middle"),
            tool_call_message("read_file", {"file_path": "/nautobot/__init__.py", "limit": 1}, "latest"),
            AIMessage(content="Summary", usage_metadata=provider_usage(900000, 900000, 1)),
            tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 1}, "after"),
            AIMessage(content="Done", usage_metadata=provider_usage(1500, 500, 2)),
        ]
        with (
            patch.object(
                ScriptedToolModel,
                "profile",
                new_callable=PropertyMock,
                create=True,
                return_value={"max_input_tokens": 128000},
            ),
            patch("nautobot_agent.runtime.agent.compute_summarization_defaults", return_value=defaults),
        ):
            self.start_graph(script)
            first_events = parse_run_events(post_run_message(self.client, "Initial"))
            events = parse_run_events(post_run_message(self.client, "Compact"))

        first = [data for event, data in first_events if event == "context"]
        snapshots = [data for event, data in events if event == "context"]
        self.assertEqual([item["used"] for item in first], [1_000_000])
        self.assertEqual([item["used"] for item in snapshots], [None, None, None, None, 2000])
        restored = page_json(self.client.get(reverse("plugins:nautobot_agent:agent")), "agent-state")["context_usage"]
        self.assertEqual(restored["used"], snapshots[-1]["used"])
        self.assertTrue(
            any(
                "Summary" in message.text
                for message in self.scripted_model.requests[-1]
                if isinstance(message, HumanMessage)
            )
        )
