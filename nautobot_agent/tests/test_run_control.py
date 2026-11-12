"""Production main/delegate admission stops new work after run drain."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage
from langgraph.runtime import RunControl

from nautobot_agent.runtime.budgets import RunBudget, RunBudgetExceeded
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import ConversationUnavailable, RunControlMiddleware, RunDraining
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import tool_call_message
from nautobot_agent.tests.support.graph import SourceGraphTestCase


class RunControlMissingAuthorityTests(SimpleTestCase):
    """Missing invocation authority must never reach a model or tool handler."""

    def test_sync_calls_reject_missing_authority(self):
        middleware = RunControlMiddleware()
        for method in (middleware.wrap_model_call, middleware.wrap_tool_call):
            for context in (None, SimpleNamespace(), SimpleNamespace(authority=None)):
                with self.subTest(method=method.__name__, context=context):
                    handler = Mock()
                    request = SimpleNamespace(runtime=SimpleNamespace(context=context))
                    with self.assertRaises(AttributeError):
                        method(request, handler)
                    handler.assert_not_called()

    def test_tool_reservations_are_atomic_latched_and_never_start_n_plus_one(self):
        budget = RunBudget(tool_limit=8, model_limit=40)
        handlers = []

        def dispatch():
            runtime = SimpleNamespace(context=SimpleNamespace(authority=AllowAllAuthority(), budget=budget))
            handler = Mock(side_effect=lambda _request: handlers.append(True))
            try:
                RunControlMiddleware().wrap_tool_call(SimpleNamespace(runtime=runtime), handler)
            except RunBudgetExceeded:
                return False
            return True

        with ThreadPoolExecutor(max_workers=16) as executor:
            admitted = list(executor.map(lambda _index: dispatch(), range(40)))
        self.assertEqual(sum(admitted), 8)
        self.assertEqual(len(handlers), 8)
        with self.assertRaises(RunBudgetExceeded) as caught:
            budget.reserve_tool_call()
        self.assertEqual(caught.exception.code, "run_tool_call_limit")
        self.assertEqual(len(handlers), 8)

    def test_model_reservations_are_atomic_and_latch_across_both_ceilings(self):
        budget = RunBudget(tool_limit=20, model_limit=7)

        def reserve(_index):
            try:
                budget.reserve_model_call()
            except RunBudgetExceeded:
                return False
            return True

        with ThreadPoolExecutor(max_workers=16) as executor:
            admitted = list(executor.map(reserve, range(40)))
        self.assertEqual(sum(admitted), 7)
        with self.assertRaises(RunBudgetExceeded) as caught:
            budget.reserve_tool_call()
        self.assertEqual(caught.exception.code, "run_model_call_limit")

        inverse = RunBudget(tool_limit=1, model_limit=10)
        inverse.reserve_tool_call()
        with self.assertRaises(RunBudgetExceeded):
            inverse.reserve_tool_call()
        with self.assertRaises(RunBudgetExceeded) as model_denied:
            inverse.reserve_model_call()
        self.assertEqual(model_denied.exception.code, "run_tool_call_limit")

    def test_authority_precedes_budget_latch(self):
        budget = RunBudget(tool_limit=1, model_limit=1)
        budget.reserve_tool_call()
        with self.assertRaises(RunBudgetExceeded):
            budget.reserve_tool_call()

        class MissingAuthority:  # pylint: disable=too-few-public-methods
            """Represent lost run ownership."""

            def require_new_work(self):
                """Reject work before checking budget exhaustion."""
                raise ConversationUnavailable()

        runtime = SimpleNamespace(context=SimpleNamespace(authority=MissingAuthority(), budget=budget))
        with self.assertRaises(RunDraining):
            RunControlMiddleware().wrap_tool_call(SimpleNamespace(runtime=runtime), Mock())

    def test_failed_tool_keeps_reservation_and_exhaustion_blocks_models(self):
        budget = RunBudget(tool_limit=1, model_limit=1)
        request = SimpleNamespace(
            runtime=SimpleNamespace(context=SimpleNamespace(authority=AllowAllAuthority(), budget=budget))
        )
        middleware = RunControlMiddleware()
        with self.assertRaises(ValueError):
            middleware.wrap_tool_call(request, Mock(side_effect=ValueError("handled tool failure")))
        denied = Mock()
        with self.assertRaises(RunBudgetExceeded):
            middleware.wrap_tool_call(request, denied)
        with self.assertRaises(RunBudgetExceeded):
            middleware.wrap_model_call(request, denied)
        denied.assert_not_called()


class RunControlGraphTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Main/delegate executor connections require committed users while exercising drain control."""

    def test_delegate_rechecks_admission_after_dispatch(self):
        graph = self.start_graph(
            [
                tool_call_message("task", {"description": "Do work", "subagent_type": "general-purpose"}, "task"),
                AIMessage(content="delegate should not run"),
            ]
        )
        control = RunControl()

        class Authority:  # pylint: disable=too-few-public-methods
            """Request drain when the delegate rechecks durable admission."""

            checks = 0

            def require_new_work(self):
                """Allow dispatch, then stop the first delegate model call."""
                self.checks += 1
                if self.checks == 2:
                    control.request_drain()
                if control.drain_requested:
                    raise RunDraining("This execution is shutting down.")

        context = AgentContext(
            user_id=str(self.user.pk), authority=Authority(), budget=RunBudget(tool_limit=100, model_limit=40)
        )
        with self.assertRaises(RunDraining):
            self.invoke(graph, context=context)
        self.assertEqual(len(self.scripted_model.requests), 1)
