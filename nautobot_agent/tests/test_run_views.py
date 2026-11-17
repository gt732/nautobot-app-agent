"""HTTP contract tests for durable chat run submission and restoration."""

import json
import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import SUCCESS, get_messages
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from kombu import Connection
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from redis.exceptions import TimeoutError as RedisTimeoutError

from nautobot_agent.chat import events as run_events
from nautobot_agent.chat import runs
from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.chat.errors import (
    BUSY_MESSAGE,
    RUN_STORAGE_UNAVAILABLE_MESSAGE,
    UNAVAILABLE_MESSAGE,
)
from nautobot_agent.chat.runs import (
    RunReservationConflict,
    finish_run,
    locked_run_write,
    request_cancel,
)
from nautobot_agent.models import (
    AgentConversation,
    AgentRun,
    AgentRunStatus,
    HeartbeatNow,
)
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.checkpointer import thread_config
from nautobot_agent.tests.support.chat import (
    claimed_run,
    create_configuration,
    create_conversation,
    create_reserved_run,
    create_storage_artifacts,
    guarded_test_config,
    page_json,
    select_conversation,
    use_runtime,
)
from nautobot_agent.tests.support.data import grant
from nautobot_agent.tests.support.db import CommittedTestCase


class AgentRunViewTests(CommittedTestCase):  # pylint: disable=too-many-public-methods
    """Real HTTP admission requires outermost autocommit and publication after the reservation commits."""

    databases = ("default",)

    def setUp(self):
        super().setUp()
        use_runtime(self)
        create_configuration()
        self.owner = get_user_model().objects.create_user(username=f"run-view-{uuid.uuid4()}")
        self.foreign = get_user_model().objects.create_user(username=f"run-view-foreign-{uuid.uuid4()}")
        self.client.force_login(self.owner)
        self.submit_url = reverse("plugins:nautobot_agent:agent_run_submit")
        self.state_url = reverse("plugins:nautobot_agent:agent_state")
        self.request_id = str(uuid.uuid4())

    def payload(self, **updates):
        """Build explicit submission input with a stable idempotency identity."""
        return {
            "message": "  normalized request  ",
            "conversation_id": None,
            "client_request_id": self.request_id,
            **updates,
        }

    def post_run(self, data=None, *, client=None):
        """POST normalized JSON through the actual submission URL."""
        return (client or self.client).post(
            self.submit_url,
            data=json.dumps(data if data is not None else self.payload()),
            content_type="application/json",
        )

    def assert_plain_agent_page_selects(self, conversation_id):
        """Assert model-free plain-page restoration resolves the selected conversation."""
        page = self.client.get(reverse("plugins:nautobot_agent:agent"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page_json(page, "agent-state")["conversation_id"], str(conversation_id))
        self.assertEqual(page_json(page, "agent-state")["history"], [])

    def test_submission_is_idempotent_and_returns_the_same_owned_identity(self):
        published = []
        with patch(
            "nautobot_agent.views.publish_run",
            side_effect=lambda *args, **kwargs: published.append((args, kwargs)),
        ):
            first = self.post_run()
            self.assertEqual(first.status_code, 202)
            first_conversation_id = first.json()["conversation_id"]
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), first_conversation_id)
            self.assert_plain_agent_page_selects(first_conversation_id)
            switched = create_conversation(self.owner)
            select_conversation(self.client, switched)
            duplicate = self.post_run(self.payload(message="different request body"))

        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json(), first.json())
        self.assertEqual(self.client.session.get(CONVERSATION_KEY), first_conversation_id)
        self.assert_plain_agent_page_selects(first_conversation_id)
        self.assertEqual(first["Cache-Control"], "private, no-store")
        self.assertEqual(len(published), 1)
        run = AgentRun.objects.get(pk=first.json()["run_id"])
        self.assertEqual(run.prompt, "normalized request")
        self.assertEqual(run.status, AgentRunStatus.QUEUED)
        self.assertEqual(run.conversation_id, uuid.UUID(first.json()["conversation_id"]))
        self.assertEqual(first.json()["events_url"], reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)))
        self.assertTrue(first.json()["cancellable"])
        self.assertIn("/cancel/", first.json()["cancel_url"])

    def test_submission_rejects_oversized_message_before_creating_storage(self):
        rejected = self.post_run(self.payload(message="x" * 32_001))
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["details"]["message"][0]["code"], "max_length")
        self.assertFalse(AgentRun.objects.filter(user=self.owner).exists())
        self.assertFalse(AgentConversation.objects.filter(user=self.owner).exists())
        with patch("nautobot_agent.views.publish_run"):
            accepted = self.post_run(self.payload(message="x" * 32_000))
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(AgentRun.objects.get(pk=accepted.json()["run_id"]).prompt, "x" * 32_000)

    def test_separate_conversations_admit_concurrently_and_busy_conflict_is_safe(self):
        owned = create_conversation(self.owner)
        sentinel = create_conversation(self.owner)
        with patch("nautobot_agent.views.publish_run"):
            accepted = self.post_run(self.payload(conversation_id=str(owned.pk)))
            self.assertEqual(accepted.status_code, 202)
            concurrent = self.post_run(
                self.payload(conversation_id=str(sentinel.pk), client_request_id=str(uuid.uuid4()))
            )
        self.assertEqual(accepted.json()["conversation_id"], str(owned.pk))
        self.assertEqual(concurrent.status_code, 202)
        self.assertEqual(concurrent.json()["conversation_id"], str(sentinel.pk))
        self.assertNotEqual(concurrent.json()["run_id"], accepted.json()["run_id"])
        self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(sentinel.pk))
        busy = self.post_run(self.payload(conversation_id=str(owned.pk), client_request_id=str(uuid.uuid4())))
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["error"], "Conversation already has an active run.")
        self.assertEqual(busy.json()["blocking_run"], accepted.json())
        self.assertNotIn("prompt", busy.json()["blocking_run"])
        self.assertEqual(busy["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(sentinel.pk))

        foreign_conversation = create_conversation(self.foreign)
        invalid_owner = self.post_run(
            self.payload(
                conversation_id=str(foreign_conversation.pk),
                client_request_id=str(uuid.uuid4()),
            )
        )
        self.assertEqual(invalid_owner.status_code, 404)
        self.assertEqual(invalid_owner.json()["error"], "Conversation not found.")
        self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(sentinel.pk))
        self.assert_plain_agent_page_selects(str(sentinel.pk))

    def test_user_limit_preserves_selection_and_other_users_can_still_submit(self):
        with patch("nautobot_agent.views.publish_run") as publish:
            for _ in range(2):
                accepted = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
                self.assertEqual(accepted.status_code, 202)
            selected = self.client.session.get(CONVERSATION_KEY)
            rejected = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(rejected.json()["code"], "user_run_limit")
            self.assertNotIn("blocking_run", rejected.json())
            self.assertEqual(rejected["Cache-Control"], "private, no-store")
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), selected)
            self.assertEqual(AgentConversation.objects.filter(user=self.owner).count(), 2)
            self.assertEqual(AgentRun.objects.filter(user=self.owner).count(), 2)
            self.assertEqual(publish.call_count, 2)

            self.client.force_login(self.foreign)
            foreign = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
            self.assertEqual(foreign.status_code, 202)
            self.assertEqual(AgentRun.objects.filter(user=self.foreign).count(), 1)

    def test_dispatch_failures_resolve_queued_run(self):
        with self.subTest("publish error resolves the run idempotently"):
            sentinel = create_conversation(self.owner)
            select_conversation(self.client, sentinel)
            with patch("nautobot_agent.views.publish_run", side_effect=OSError("private broker detail")):
                failed = self.post_run()
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.json()["status"], AgentRunStatus.DISPATCH_FAILED)
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(sentinel.pk))
            run = AgentRun.objects.get(pk=failed.json()["run_id"])
            self.assertTrue(run.is_terminal)
            self.assertEqual(run.error_code, "OSError")
            duplicate_failed = self.post_run(self.payload(message="changed after dispatch failure"))
            self.assertEqual(duplicate_failed.status_code, 200)
            self.assertEqual(duplicate_failed.json()["run_id"], failed.json()["run_id"])
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), duplicate_failed.json()["conversation_id"])
            self.assert_plain_agent_page_selects(duplicate_failed.json()["conversation_id"])

        with self.subTest("unreachable broker resolves the queued run"):
            unreachable = Connection(
                "amqp://guest:guest@127.0.0.1:1//",
                connect_timeout=0.1,
                transport_options={
                    "socket_timeout": 0.1,
                    "socket_connect_timeout": 0.1,
                    "retry_on_timeout": False,
                    "max_retries": 0,
                },
            )
            with patch("nautobot_agent.tasks.app.connection_for_write", return_value=unreachable):
                response = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["status"], AgentRunStatus.DISPATCH_FAILED)
            run = AgentRun.objects.get(pk=response.json()["run_id"])
            self.assertTrue(run.error_code)
            self.assertNotIn("guest", run.error_code)
            self.assertFalse(run.prompt)

        with self.subTest("claim race preserves running"):

            def claim_then_fail(run_id, *args, **kwargs):
                claimed_run(run_id)
                raise OSError("private broker detail")

            with patch("nautobot_agent.views.publish_run", side_effect=claim_then_fail):
                accepted = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["status"], AgentRunStatus.RUNNING)

    def test_admission_failures_are_sanitized_and_do_not_change_selection(self):
        sentinel = create_conversation(self.owner)
        select_conversation(self.client, sentinel)
        for error, message in (
            (DatabaseError("private storage detail"), RUN_STORAGE_UNAVAILABLE_MESSAGE),
            (ImproperlyConfigured("private configuration detail"), RUN_STORAGE_UNAVAILABLE_MESSAGE),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("nautobot_agent.views.chat_runs.submit_run", side_effect=error):
                    response = self.post_run()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"error": message})
                self.assertEqual(response["Cache-Control"], "private, no-store")
                self.assertEqual(self.client.session[CONVERSATION_KEY], str(sentinel.pk))

    def test_submission_requires_csrf_and_url_callback_excludes_atomic_requests(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        denied = self.post_run(client=csrf_client)
        self.assertEqual(denied.status_code, 403)
        anonymous = Client().post(
            reverse("plugins:nautobot_agent:agent_run_submit"),
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(
            self.client.post(
                reverse("plugins:nautobot_agent:agent_run_submit"),
                data=b"{",
                content_type="application/json",
            ).status_code,
            400,
        )
        for body in (
            json.dumps(self.payload(message=[])).encode(),
            json.dumps(self.payload(message=42)).encode(),
            json.dumps(self.payload(client_request_id=42)).encode(),
            json.dumps({**self.payload(), "extra": True}).encode(),
        ):
            with self.subTest(body=body):
                invalid = self.client.post(
                    reverse("plugins:nautobot_agent:agent_run_submit"),
                    data=body,
                    content_type="application/json",
                )
                self.assertEqual(invalid.status_code, 400)

        observed = []
        old_atomic_requests = connection.settings_dict.get("ATOMIC_REQUESTS", False)
        connection.settings_dict["ATOMIC_REQUESTS"] = True
        try:
            with patch(
                "nautobot_agent.views.publish_run",
                side_effect=lambda *args, **kwargs: observed.append(connection.in_atomic_block),
            ):
                response = self.post_run()
        finally:
            connection.settings_dict["ATOMIC_REQUESTS"] = old_atomic_requests
        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed, [False])

    def test_cancel_is_owner_only_csrf_protected_and_reports_actual_transition(self):
        with patch("nautobot_agent.views.publish_run"):
            accepted = self.post_run()
        run_id = uuid.UUID(accepted.json()["run_id"])
        cancel_url = reverse("plugins:nautobot_agent:agent_run_cancel", args=(run_id,))
        denied_client = Client(enforce_csrf_checks=True)
        denied_client.force_login(self.owner)
        self.assertEqual(denied_client.post(cancel_url).status_code, 403)

        foreign_client = Client()
        foreign_client.force_login(self.foreign)
        self.assertEqual(foreign_client.post(cancel_url).status_code, 404)
        self.assertEqual(
            foreign_client.post(reverse("plugins:nautobot_agent:agent_run_cancel", args=(uuid.uuid4(),))).status_code,
            404,
        )

        with patch("nautobot_agent.views.chat_runs.request_cancel", side_effect=DatabaseError("private SQL")):
            unavailable = self.client.post(cancel_url)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json(), {"error": RUN_STORAGE_UNAVAILABLE_MESSAGE})
        self.assertEqual(unavailable["Cache-Control"], "private, no-store")
        self.assertEqual(AgentRun.objects.get(pk=run_id).status, AgentRunStatus.QUEUED)

        queued = self.client.post(cancel_url)
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(queued.json()["status"], AgentRunStatus.CANCELLED)
        self.assertIs(queued.json()["execution_started"], False)
        self.assertFalse(AgentRun.objects.get(pk=run_id).prompt)

        with patch("nautobot_agent.views.publish_run"):
            running_response = self.post_run(self.payload(client_request_id=str(uuid.uuid4())))
        running_id = uuid.UUID(running_response.json()["run_id"])
        claimed_run(running_id)
        running_url = reverse("plugins:nautobot_agent:agent_run_cancel", args=(running_id,))
        pending = self.client.post(running_url)
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.json()["status"], AgentRunStatus.RUNNING)
        self.assertTrue(pending.json()["cancel_requested"])
        self.assertIs(pending.json()["execution_started"], True)
        self.assertEqual(AgentRun.objects.get(pk=running_id).prompt, "normalized request")
        self.assertEqual(pending["Cache-Control"], "private, no-store")

        AgentRun.objects.filter(pk=running_id).update(heartbeat_at=timezone.now() - timedelta(seconds=31))
        interrupted = self.client.post(running_url)
        self.assertEqual(interrupted.status_code, 200)
        self.assertEqual(interrupted.json()["status"], AgentRunStatus.INTERRUPTED)
        self.assertEqual(interrupted.json()["error_code"], "stale_heartbeat")
        self.assertTrue(interrupted.json()["terminal"])
        self.assertEqual(AgentRun.objects.get(pk=running_id).prompt, "")

    def test_delete_never_releases_active_runs(self):  # pylint: disable=too-many-locals
        grant(self.owner, AgentConversation, actions=("view", "viewall"))
        grant(self.owner, AgentConversation, actions=("delete",), constraints={"user": "$user"})

        bulk_delete_url = reverse("plugins:nautobot_agent:agentconversation_bulk_delete")
        for status in (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING):
            with self.subTest("single delete", status=status):
                target = create_conversation(self.owner)
                busy_artifacts = create_storage_artifacts(target, label=f"busy-{status}")
                with patch("nautobot_agent.views.publish_run"):
                    submitted = self.post_run(
                        self.payload(conversation_id=str(target.pk), client_request_id=str(uuid.uuid4()))
                    )
                run = AgentRun.objects.get(pk=submitted.json()["run_id"])
                if status != AgentRunStatus.QUEUED:
                    claimed_run(run.pk)
                    run.refresh_from_db()

                url = reverse("plugins:nautobot_agent:agentconversation_delete", args=(target.pk,))
                self.client.post(url, {"confirm": "true"})

                target.refresh_from_db()
                run.refresh_from_db()

                self.assertEqual(run.status, status)
                self.assertTrue(AgentConversation.objects.filter(pk=target.pk).exists())
                for row in busy_artifacts:
                    self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
                if status == AgentRunStatus.RUNNING:
                    finish_run(run.pk, AgentRunStatus.COMPLETED)
                else:
                    request_cancel(run.pk, self.owner)

        with self.subTest("interrupted runs do not block deletion and late workers cannot write"):
            target = create_conversation(self.owner)
            with patch("nautobot_agent.views.publish_run"):
                submitted = self.post_run(
                    self.payload(conversation_id=str(target.pk), client_request_id=str(uuid.uuid4()))
                )
            run_id = uuid.UUID(submitted.json()["run_id"])
            claimed_run(run_id)
            finish_run(run_id, AgentRunStatus.INTERRUPTED, error_code="stale_heartbeat")

            url = reverse("plugins:nautobot_agent:agentconversation_delete", args=(target.pk,))
            self.client.post(url, {"confirm": "true"})

            self.assertFalse(AgentConversation.objects.filter(pk=target.pk).exists())
            self.assertFalse(AgentRun.objects.filter(pk=run_id).exists())
            with self.assertRaises(RunReservationConflict), locked_run_write(run_id):
                pass
            self.assertFalse(AgentRun.objects.filter(pk=run_id).exists())

        with self.subTest("bulk delete skips the active target and deletes the idle one"):
            active = create_conversation(self.owner)
            idle = create_conversation(self.owner)
            active_artifacts = create_storage_artifacts(active, label="bulk-active")
            idle_artifacts = create_storage_artifacts(idle, label="bulk-idle")
            with patch("nautobot_agent.views.publish_run"):
                submitted = self.post_run(
                    self.payload(conversation_id=str(active.pk), client_request_id=str(uuid.uuid4()))
                )
            response = self.client.post(
                bulk_delete_url, {"pk": [str(active.pk), str(idle.pk)], "_confirm": "yes", "confirm": "true"}
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(AgentConversation.objects.filter(pk=active.pk).exists())
            self.assertFalse(AgentConversation.objects.filter(pk=idle.pk).exists())
            active.refresh_from_db()
            run = AgentRun.objects.get(pk=submitted.json()["run_id"])

            self.assertEqual(run.status, AgentRunStatus.QUEUED)
            for row in active_artifacts:
                self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
            for row in idle_artifacts:
                self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
            request_cancel(run.pk, self.owner)

        with self.subTest("bulk delete skips active, revoked, and removed targets"):
            now = timezone.now()
            active, revoked, removed, idle = (
                create_conversation(self.owner, last_activity_at=now - timedelta(minutes=index)) for index in range(4)
            )
            active_artifacts = create_storage_artifacts(active, label="changed-active")
            revoked_artifacts = create_storage_artifacts(revoked, label="changed-revoked")
            idle_artifacts = create_storage_artifacts(idle, label="changed-idle")
            with patch("nautobot_agent.views.publish_run"):
                submitted = self.post_run(
                    self.payload(conversation_id=str(active.pk), client_request_id=str(uuid.uuid4()))
                ).json()
            session = self.client.session
            session["nautobot_agent_conversation_id"] = str(idle.pk)
            session.save()
            lock_reservations = "nautobot_agent.chat.conversation.runs.lock_conversation_reservations"
            original_lock_reservations = runs.lock_conversation_reservations

            def change_access_before_lock(owner_by_id, conversations, *, using):
                AgentConversation.objects.filter(pk=revoked.pk).update(user=self.foreign)
                AgentConversation.objects.filter(pk=removed.pk).delete()
                return original_lock_reservations(owner_by_id, conversations, using=using)

            with patch(lock_reservations, side_effect=change_access_before_lock):
                response = self.client.post(
                    bulk_delete_url,
                    {
                        "pk": [str(target.pk) for target in (active, revoked, removed, idle)],
                        "_confirm": "yes",
                        "confirm": "true",
                    },
                )

            self.assertEqual(response.status_code, 302)
            active.refresh_from_db()
            revoked.refresh_from_db()

            self.assertEqual(revoked.user_id, self.foreign.pk)
            self.assertFalse(AgentConversation.objects.filter(pk__in=(removed.pk, idle.pk)).exists())
            self.assertNotIn("nautobot_agent_conversation_id", self.client.session)
            sent = list(get_messages(response.wsgi_request))[-1]  # earlier requests' messages may linger
            self.assertEqual(sent.level, SUCCESS)
            self.assertEqual(re.findall(r"\d+", str(sent)), ["1", "3"])
            self.assertIn(BUSY_MESSAGE, str(sent))
            self.assertIn(UNAVAILABLE_MESSAGE, str(sent))
            for row in (*active_artifacts, *revoked_artifacts):
                self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
            for row in idle_artifacts:
                self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
            selected_session = self.client.session
            selected_session[CONVERSATION_KEY] = str(active.pk)
            selected_session.save()
            self.client.post(
                bulk_delete_url,
                {"pk": [str(active.pk)], "_confirm": "yes", "confirm": "true"},
            )
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(active.pk))
            selected_session = self.client.session
            selected_session[CONVERSATION_KEY] = str(revoked.pk)
            selected_session.save()
            self.client.post(
                bulk_delete_url,
                {"pk": [str(revoked.pk)], "_confirm": "yes", "confirm": "true"},
            )
            self.assertEqual(self.client.session.get(CONVERSATION_KEY), str(revoked.pk))


class RunReadViewTests(TestCase):
    """Validate read-only selectors and owner-checked page restoration under rollback isolation."""

    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(username=f"run-read-{uuid.uuid4()}")
        cls.foreign = user_model.objects.create_user(username=f"run-read-foreign-{uuid.uuid4()}")

    def setUp(self):
        self.client.force_login(self.owner)
        self.state_url = reverse("plugins:nautobot_agent:agent_state")

    def create_run(self, *, conversation=None, status=AgentRunStatus.QUEUED, **fields):
        """Persist a minimal run projection with its reservation consistent with its status."""
        conversation = conversation or create_conversation(self.owner)
        now = fields.pop("submitted_at", timezone.now())
        if status not in AgentRunStatus.values:
            raise ValueError(f"Unsupported fixture status: {status}")
        run = create_reserved_run(conversation, prompt="saved request", client_request_id=uuid.uuid4())
        if status == AgentRunStatus.CANCELLED:
            request_cancel(run.pk, self.owner)
        elif status == AgentRunStatus.DISPATCH_FAILED:
            runs._resolve_dispatch_failure(run.pk, "dispatch_failed")  # pylint: disable=protected-access
        elif status != AgentRunStatus.QUEUED:
            claimed_run(run.pk)
            if status != AgentRunStatus.RUNNING:
                finish_run(run.pk, status)
        if fields:
            AgentRun.objects.filter(pk=run.pk).update(**fields)
        if now != run.submitted_at:
            AgentRun.objects.filter(pk=run.pk).update(submitted_at=now)
        run.refresh_from_db()
        return conversation, run

    def test_owned_reads_lazily_expire_queued_runs(self):
        _conversation, polled = self.create_run(queue_expires_at=HeartbeatNow())
        events_url = reverse("plugins:nautobot_agent:agent_run_events", args=(polled.pk,))
        cancel_url = reverse("plugins:nautobot_agent:agent_run_cancel", args=(polled.pk,))

        foreign_client = Client()
        foreign_client.force_login(self.foreign)
        self.assertEqual(foreign_client.get(events_url, {"status_only": "1"}).status_code, 404)
        self.assertEqual(foreign_client.post(cancel_url).status_code, 404)
        foreign_state = foreign_client.get(self.state_url, {"client_request": str(polled.client_request_id)})
        self.assertEqual(foreign_state.json(), {"run_id": None})
        polled.refresh_from_db()
        self.assertEqual(polled.status, AgentRunStatus.QUEUED)

        payload = self.client.get(events_url, {"status_only": "1"}).json()
        self.assertEqual(payload["status"], AgentRunStatus.DISPATCH_FAILED)
        self.assertEqual(payload["error_code"], "queue_wait_expired")
        self.assertTrue(payload["terminal"])
        self.assertFalse(payload["cancellable"])
        polled.refresh_from_db()
        self.assertEqual(polled.prompt, "")

        _conversation, by_request = self.create_run(queue_expires_at=HeartbeatNow())
        summary = self.client.get(self.state_url, {"client_request": str(by_request.client_request_id)}).json()
        self.assertEqual(summary["status"], AgentRunStatus.DISPATCH_FAILED)

        _conversation, cancelled = self.create_run(queue_expires_at=HeartbeatNow())
        response = self.client.post(reverse("plugins:nautobot_agent:agent_run_cancel", args=(cancelled.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error_code"], "queue_wait_expired")

        conversation, restored = self.create_run(queue_expires_at=HeartbeatNow())
        state = self.client.get(self.state_url, {"conversation": str(conversation.pk)}).json()
        self.assertIsNone(state["active_run"])
        self.assertEqual(state["latest_run"]["run_id"], str(restored.pk))
        self.assertEqual(state["latest_run"]["status"], AgentRunStatus.DISPATCH_FAILED)
        restored.refresh_from_db()
        self.assertEqual(restored.error_code, "queue_wait_expired")

    def test_state_requires_one_selector_and_client_request_miss_is_indistinguishable(self):
        for suffix in ("", f"?conversation={uuid.uuid4()}&client_request={uuid.uuid4()}", "?conversation=bad"):
            self.assertEqual(self.client.get(f"{self.state_url}{suffix}").status_code, 400)
        response = self.client.get(f"{self.state_url}?client_request={uuid.uuid4()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"run_id": None})

    def test_state_projects_checkpoint_model_free_or_unavailable(self):
        create_configuration()
        use_runtime(self, saver=current_runtime().saver)
        conversation = create_conversation(self.owner)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {
            "messages": [HumanMessage("saved question", id="question"), AIMessage("saved answer", id="answer")],
            "todos": [{"content": "saved todo", "status": "completed"}],
        }
        checkpoint["channel_versions"] = {"messages": 1, "todos": 1}
        current_runtime().saver.put(
            guarded_test_config(thread_config(conversation.pk), conversation),
            checkpoint,
            {"source": "input", "step": 0},
            {"messages": 1, "todos": 1},
        )
        with (
            patch("nautobot_agent.runtime.llm.init_chat_model", side_effect=AssertionError("provider invoked")),
            patch(
                "nautobot_agent.mcp.tools.MCPToolCache.get",
                side_effect=AssertionError("MCP catalog requested during state restoration"),
            ),
        ):
            response = self.client.get(f"{self.state_url}?conversation={conversation.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["history"],
            [
                {"role": "user", "content": "saved question"},
                {"role": "assistant", "content": "saved answer"},
            ],
        )
        self.assertEqual(response.json()["todos"], [{"content": "saved todo", "status": "completed"}])
        self.assertIsNone(response.json()["model_name"])

        with self.subTest("failed restoration"):
            with patch.object(
                current_runtime().saver, "get_tuple", side_effect=DatabaseError("private database details")
            ):
                state = self.client.get(f"{self.state_url}?conversation={conversation.pk}")
                page = self.client.get(reverse("plugins:nautobot_agent:agent"), {"conversation": str(conversation.pk)})
            self.assertEqual(state.status_code, 503)
            self.assertEqual(page.status_code, 200)
            self.assertTrue(page.context["notice"])
            self.assertEqual(page_json(page, "agent-state")["conversation_id"], str(conversation.pk))
            self.assertNotContains(page, "private database details")
            self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

    def test_state_selection_is_session_neutral_for_success_and_failures(self):
        use_runtime(self, saver=current_runtime().saver)
        requested = create_conversation(self.owner)
        selected = create_conversation(self.owner)
        foreign = create_conversation(self.foreign)
        select_conversation(self.client, selected)
        base = f"{self.state_url}?conversation="

        response = self.client.get(f"{base}{str(requested.pk).upper()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversation_id"], str(requested.pk))
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(selected.pk))
        self.assertEqual(response["Cache-Control"], "private, no-store")

        for selector, status in (
            ("malformed", 400),
            (requested.pk.hex, 400),
            (str(uuid.uuid4()), 404),
            (str(foreign.pk), 404),
        ):
            with self.subTest(selector=selector):
                failed = self.client.get(f"{base}{selector}")
                self.assertEqual(failed.status_code, status)
                self.assertEqual(failed["Cache-Control"], "private, no-store")
                self.assertEqual(self.client.session[CONVERSATION_KEY], str(selected.pk))

        with patch.object(current_runtime().saver, "get_tuple", side_effect=DatabaseError("private storage detail")):
            degraded_selected = self.client.get(f"{base}{requested.pk}")
        self.assertEqual(degraded_selected.status_code, 503)
        self.assertEqual(degraded_selected["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(selected.pk))

        session = self.client.session
        session.pop(CONVERSATION_KEY)
        session.save()
        no_selection = self.client.get(f"{base}{requested.pk}")
        self.assertEqual(no_selection.status_code, 200)
        self.assertNotIn(CONVERSATION_KEY, self.client.session)

        with patch.object(current_runtime().saver, "get_tuple", side_effect=DatabaseError("private storage detail")):
            degraded = self.client.get(f"{base}{requested.pk}")
        self.assertEqual(degraded.status_code, 503)
        self.assertEqual(degraded["Cache-Control"], "private, no-store")
        self.assertNotIn("private storage detail", degraded.content.decode())
        self.assertNotIn(CONVERSATION_KEY, self.client.session)

    def test_initial_conversation_lookup_database_error_is_sanitized(self):
        conversation = create_conversation(self.owner)
        sentinel = create_conversation(self.owner)
        select_conversation(self.client, sentinel)
        with patch(
            "nautobot_agent.chat.conversation.get_object_or_404",
            side_effect=DatabaseError("private lookup details"),
        ):
            response = self.client.get(self.state_url, {"conversation": str(conversation.pk)})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Conversation state is unavailable."})
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertNotIn("private lookup details", response.content.decode())
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(sentinel.pk))

    def test_run_routes_and_state_selectors_are_owner_only(self):
        conversation, run = self.create_run()
        own_lookup = self.client.get(f"{self.state_url}?client_request={run.client_request_id}")
        self.assertEqual(own_lookup.status_code, 200)
        self.assertEqual(own_lookup.json()["run_id"], str(run.pk))

        foreign_client = Client()
        foreign_client.force_login(self.foreign)
        for url in (
            reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)),
            f"{self.state_url}?conversation={conversation.pk}",
        ):
            result = foreign_client.get(url)
            self.assertEqual(result.status_code, 404)
            self.assertEqual(result["Cache-Control"], "private, no-store")
        foreign_lookup = foreign_client.get(f"{self.state_url}?client_request={run.client_request_id}")
        self.assertEqual(foreign_lookup.status_code, 200)
        self.assertEqual(foreign_lookup.json(), {"run_id": None})

        events = self.client.get(reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)))
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events["Cache-Control"], "private, no-store")
        invalid_cursor = self.client.get(
            reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)) + "?after=invalid"
        )
        self.assertEqual(invalid_cursor.status_code, 400)

    def test_event_timeout_returns_sanitized_503_then_resumes_from_cursor(self):
        _conversation, run = self.create_run(status=AgentRunStatus.RUNNING)
        publisher = run_events.RunEvents()
        self.addCleanup(publisher.close)
        publisher.start(run.pk)
        self.addCleanup(publisher._get_client()[1].delete, publisher._key(run.pk))  # pylint: disable=protected-access
        cursor = publisher.publish(run.pk, "token", {"text": "before"})
        publisher.publish(run.pk, "token", {"text": "after"})
        url = reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,))
        reader_client = run_events._reader._get_client()[1]  # pylint: disable=protected-access
        with patch.object(reader_client, "eval", side_effect=RedisTimeoutError("private Redis address")):
            response = self.client.get(url, {"after": cursor})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Live events are temporarily unavailable."})
        self.assertEqual(response["Cache-Control"], "private, no-store")
        recovered = self.client.get(url, {"after": cursor})
        self.assertEqual(recovered.status_code, 200)
        self.assertFalse(recovered.json()["resync_required"])
        self.assertEqual(recovered.json()["events"], [{"event": "token", "data": {"text": "after"}}])
        run.refresh_from_db()
        self.assertFalse(run.live_events_incomplete)
        self.assertEqual(run.status, AgentRunStatus.RUNNING)

    def test_state_storage_failures_stale_runs_and_cursor_coverage(self):
        _conversation, run = self.create_run()
        for operation, url in (
            ("run", reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,))),
            ("client_request", f"{self.state_url}?client_request={run.client_request_id}"),
        ):
            with (
                self.subTest(operation=operation),
                patch("nautobot_agent.views.run_status.reconcile_owned_run", side_effect=DatabaseError("private SQL")),
            ):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"error": "Run storage is unavailable."})
                self.assertEqual(response["Cache-Control"], "private, no-store")

        with self.subTest("stale active run is reconciled"):
            conversation, stale = self.create_run(
                status=AgentRunStatus.RUNNING, heartbeat_at=timezone.now() - timedelta(seconds=31)
            )
            response = self.client.get(self.state_url, {"conversation": str(conversation.pk)})
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["active_run"])
            self.assertEqual(response.json()["latest_run"]["status"], AgentRunStatus.INTERRUPTED)
            self.assertTrue(response.json()["latest_run"]["terminal"])
            stale.refresh_from_db()
            self.assertEqual(
                (stale.status, stale.error_code, stale.prompt), (AgentRunStatus.INTERRUPTED, "stale_heartbeat", "")
            )

        with self.subTest("initial running cursor cannot hide lost coverage"):
            _conversation, running = self.create_run(status=AgentRunStatus.RUNNING)
            events_url = reverse("plugins:nautobot_agent:agent_run_events", args=(running.pk,))
            starting = self.client.get(events_url, {"after": "0-0"}).json()
            self.assertEqual(starting["status"], AgentRunStatus.RUNNING)
            self.assertFalse(starting["resync_required"])
            self.assertEqual(starting["events"], [])
            lost_cursor = self.client.get(events_url, {"after": "1-0"}).json()
            self.assertTrue(lost_cursor["resync_required"])
            AgentRun.objects.filter(pk=running.pk).update(live_events_incomplete=True)
            incomplete = self.client.get(events_url, {"after": "0-0"}).json()
            self.assertTrue(incomplete["resync_required"])
            with patch(
                "nautobot_agent.views.run_events.read_run_events",
                side_effect=AssertionError("status-only request read event storage"),
            ):
                status_only = self.client.get(events_url, {"status_only": "1", "after": "invalid"})
            self.assertEqual(status_only.status_code, 200)
            payload = status_only.json()
            self.assertEqual(payload["status"], AgentRunStatus.RUNNING)
            self.assertTrue(payload["live_events_incomplete"])
            self.assertFalse(payload["terminal"])
            self.assertTrue({"events", "has_more", "next_cursor", "resync_required"}.isdisjoint(payload))
            self.assertEqual(self.client.get(events_url, {"status_only": "true", "after": "invalid"}).status_code, 400)
