"""Database-free coverage of pure run-outcome precedence."""

from django.test import SimpleTestCase

from nautobot_agent.chat.execution import RunObservations, RunOutcome, TerminationKind, decide_run_outcome
from nautobot_agent.models import AgentRunStatus


class RunOutcomeDecisionTests(SimpleTestCase):
    """Test run outcome precedence for observed termination conditions."""

    def test_outcome_precedence_table(self):
        completed, refused, failed = TerminationKind.COMPLETED, TerminationKind.REFUSED, TerminationKind.FAILED
        cases = (
            (RunObservations(completed, "", "", "first"), AgentRunStatus.INTERRUPTED, "first"),
            (RunObservations(completed, ownership_invalid=True), AgentRunStatus.INTERRUPTED, "ownership_unavailable"),
            (RunObservations(completed, drain_reason="cancelled"), AgentRunStatus.COMPLETED, ""),
            (RunObservations(refused, drain_reason="soft_deadline"), AgentRunStatus.REFUSED, ""),
            (RunObservations(failed, drain_reason="cancelled"), AgentRunStatus.CANCELLED, ""),
            (RunObservations(failed, "ProjectionError"), AgentRunStatus.FAILED, "ProjectionError"),
            (RunObservations(failed, "execution_unavailable", "cancelled"), AgentRunStatus.CANCELLED, ""),
            (RunObservations(failed, "soft_time_limit", "cancelled"), AgentRunStatus.FAILED, "soft_time_limit"),
            (RunObservations(failed, drain_reason="soft_deadline"), AgentRunStatus.FAILED, "soft_deadline"),
            (
                RunObservations(failed, drain_reason="cancelled", ownership_invalid=True),
                AgentRunStatus.INTERRUPTED,
                "ownership_unavailable",
            ),
            (RunObservations(failed, interruption_reason="first"), AgentRunStatus.INTERRUPTED, "first"),
            (RunObservations(refused), AgentRunStatus.REFUSED, ""),
            (RunObservations(completed), AgentRunStatus.COMPLETED, ""),
            (RunObservations(failed), AgentRunStatus.FAILED, ""),
        )
        for observations, status, error_code in cases:
            with self.subTest(observations=observations):
                self.assertEqual(decide_run_outcome(observations), RunOutcome(status=status, error_code=error_code))
