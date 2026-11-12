"""Credential-free HTTP-to-task proof for durable accepted and refused runs."""

import json
import threading
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connections
from django.urls import reverse
from fastmcp import FastMCP
from langchain_core.messages import AIMessage

from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import AgentRun, AgentRunStatus, MCPServer
from nautobot_agent.tasks import execute_chat_run
from nautobot_agent.tests.support.chat import (
    delete_run_events,
    tool_call_message,
)
from nautobot_agent.tests.support.graph import DurableSourceGraphTestCase
from nautobot_agent.tests.support.mcp import (
    Host,
    InProcessLaunch,
    patched_launches,
)


class DurableRunHttpSmokeTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Use committed fixtures, the production graph/saver, real Redis events, and the task entry point."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username=f"run-smoke-{uuid.uuid4()}")
        self.client.force_login(self.user)
        self.task_finished = threading.Event()

    def _launch_task(self, threads):
        def launch(run_id, *_options, **_limits):
            result = []

            def execute():
                try:
                    result.append(execute_chat_run.apply(args=(str(run_id),), throw=True))
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    result.append(exc)
                finally:
                    connections.close_all()
                    self.task_finished.set()

            thread = threading.Thread(target=execute, name=f"chat-run-{run_id}")
            threads.append((thread, result))
            thread.start()

        return launch

    def _submit(self, message, request_id=None, *, conversation_id=None):
        response = self.client.post(
            reverse("plugins:nautobot_agent:agent_run_submit"),
            data=json.dumps(
                {
                    "message": message,
                    "conversation_id": str(conversation_id) if conversation_id else None,
                    "client_request_id": str(request_id or uuid.uuid4()),
                }
            ),
            content_type="application/json",
        )
        if response.status_code == 202:
            self.addCleanup(delete_run_events, response.json()["run_id"])
        return response

    def _wait_terminal(self, run):
        self.assertTrue(self.task_finished.wait(15), f"Run {run.pk} task did not finish.")
        run.refresh_from_db()
        self.assertTrue(run.is_terminal)
        return run.status

    def _join_tasks(self, threads):
        for thread, result in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "Task thread did not terminate within its bound.")
            self.assertTrue(result, "Task entry returned no result.")
            if isinstance(result[0], Exception):
                raise result[0]

    def _mcp_server(self, remote):
        server = MCPServer.objects.create(name=f"Smoke {uuid.uuid4()}", url="http://127.0.0.1:1/mcp")
        server.enabled = True
        server.save(update_fields=("enabled",))
        tool_name = f"{server.pk.hex[:12]}_operation"
        return server, tool_name, lambda _spec: InProcessLaunch(remote)

    def _start_mcp_run(self, script, launch_factory, threads):
        build_turn = MCPTurn(host=Host())
        self.addCleanup(build_turn.close)
        self.enterContext(patched_launches(launch_factory))
        self.start_graph(script, mcp_turn=build_turn)
        build_turn.close()
        with (
            patch("nautobot_agent.views.publish_run", side_effect=self._launch_task(threads)),
        ):
            response = self._submit("run the MCP operation")
        self.assertEqual(response.status_code, 202)
        return response, AgentRun.objects.get(pk=response.json()["run_id"])

    def test_production_graph_response_loss_interrupts_and_releases_with_unknown_outcome(self):
        remote = FastMCP("smoke-response-loss")
        invocations = []

        @remote.tool
        def operation() -> str:
            """Record the actual remote operation."""
            invocations.append("invoked")
            return "remote result"

        _server, tool_name, _factory = self._mcp_server(remote)
        lost = []

        def response_loss():
            lost.append("after invocation")
            raise ConnectionError("response lost after operation")

        def launch_factory(_spec):
            return InProcessLaunch(remote, response_loss=response_loss)

        threads = []
        self.addCleanup(self._join_tasks, threads)
        response, run = self._start_mcp_run(
            [tool_call_message(tool_name, {}, "uncertain"), AIMessage(content="must not complete")],
            launch_factory,
            threads,
        )
        self.assertEqual(self._wait_terminal(run), AgentRunStatus.INTERRUPTED)
        self._join_tasks(threads)
        run.refresh_from_db()
        self.assertEqual(invocations, ["invoked"])
        self.assertEqual(lost, ["after invocation"])
        self.assertEqual((run.error_code, run.prompt), ("mcp_outcome_unknown", ""))
        self.assertTrue(run.external_outcome_unknown)
        self.assertFalse(AgentRun.objects.active().filter(conversation_id=run.conversation_id).exists())

        page = self.client.get(response.json()["events_url"], {"after": "0-0"}).json()
        self.assertTrue(page["terminal"])
        self.assertTrue(page["external_outcome_unknown"])
        self.assertNotIn("done", [event["event"] for event in page["events"]])
