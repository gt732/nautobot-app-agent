"""Production graph coverage of enabled Nautobot App source mounts for the main agent and its native delegate.

The adapters come from the real ``SourceCatalog`` enumeration over temporary packages, so only the
trusted PLUGINS setting, registered configurations, and import specs are controlled. The main agent runs each full call
map; its native delegate runs a representative subset of ids per test. Graph execution is synchronous;
the shared filesystem tools also receive direct asynchronous invocation coverage.
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from asgiref.sync import async_to_sync
from deepagents.backends import CompositeBackend
from deepagents.backends.utils import create_file_data
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.exceptions import ImproperlyConfigured
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage

from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.sources.backends import SourceCatalog
from nautobot_agent.sources.source_access import SourceAccessMiddleware, SourceBackend, resolve_source_apps
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import (
    DELEGATE_TOOLS,
    INTERNAL_SENTINEL,
    enable_agent_settings,
    runtime_graph,
    tool_call_message,
)
from nautobot_agent.tests.support.graph import GraphTestCase
from nautobot_agent.tests.support.sources import (
    APP_MODELS_LINES,
    APP_OUTSIDE_SENTINELS,
    create_app_tree,
    patched_app_discovery,
    snapshot_tree,
)

SEEDED_PATH = "/private/seeded.txt"


def select_calls(calls, representative, delegate):
    """Run every main-agent call and representative delegate calls."""
    if not delegate:
        return calls
    return {call_id: calls[call_id] for call_id in representative}


# GraphTestCase supplies NautobotTestCaseMixin with Django rollback isolation, without unrelated status fixtures.
class AppSourceGraphTestCase(GraphTestCase):  # pylint: disable=too-many-ancestors,nb-incorrect-base-class
    """Source-tool executor threads require committed users and grants."""

    user: Any  # The Nautobot test base supplies this principal; scenarios replace it.

    def setUp(self):
        super().setUp()
        self.app_tree = create_app_tree(self.tree)
        with patched_app_discovery(self, self.app_tree):
            self.catalog = SourceCatalog()
            self.app_backends = self.catalog.apps
        group = Group.objects.create(name="Source Graph App Readers")
        self.user.groups.add(group)
        enable_agent_settings(self, source_apps_by_group={group.name: list(self.app_backends)})
        self.site = self.tree.site
        self.seeded = {
            path: create_file_data(INTERNAL_SENTINEL)
            for path in (SEEDED_PATH, "/conversation_history/session_seeded.md", "/blobs/seeded.bin")
        }

    def run_turn(self, calls, *, delegate=False):
        """Run the tool calls as one turn of the main agent or of its delegate; return results by call id."""
        messages = [tool_call_message(name, args, call_id) for call_id, (name, args) in calls.items()]
        if delegate:
            task = {"description": "Inspect App source.", "subagent_type": "general-purpose"}
            script = [tool_call_message("task", task, "delegate"), *messages]
            script += [AIMessage(content="Delegate done."), AIMessage(content="Done.")]
        else:
            script = [*messages, AIMessage(content="Done.")]
        graph = self.start_graph(script, app_source_backends=self.app_backends)
        state, config = self.invoke(graph, files=self.seeded)
        files = graph.get_state(config).values["files"]
        self.assertEqual(
            {path: files[path]["content"] for path in self.seeded},
            dict.fromkeys(self.seeded, INTERNAL_SENTINEL),
        )
        if delegate:
            return self.tool_results(self.scripted_model.calls_with_tools(DELEGATE_TOOLS)[-1])
        return self.tool_results(state["messages"])

    def test_app_source_is_listed_globbed_searched_and_read_by_every_agent_and_mode(self):
        calls = {
            "ls": ("ls", {"path": "/apps/enabled_app/"}),
            "glob": ("glob", {"pattern": "**/*.py", "path": "/apps/enabled_app/"}),
            "grep": ("grep", {"pattern": "needle", "path": "/apps/enabled_app/views", "output_mode": "content"}),
            "grep-html": (
                "grep",
                {
                    "pattern": "needle",
                    "path": "/apps/enabled_app/templates",
                    "glob": "*.html",
                    "output_mode": "content",
                },
            ),
            "read": ("read_file", {"file_path": "/apps/enabled_app/models.py", "offset": 1, "limit": 3}),
            "nested-ls": ("ls", {"path": "/apps/nested_app/"}),
            "nested-read": ("read_file", {"file_path": "/apps/nested_app/__init__.py"}),
            "core-read": ("read_file", {"file_path": "/nautobot/__init__.py"}),
        }
        representative = ("read",)
        before = snapshot_tree(self.tree.base)

        for delegate in (False, True):
            with self.subTest(delegate=delegate):
                mode_calls = select_calls(calls, representative, delegate)
                results = self.run_turn(mode_calls, delegate=delegate)

                self.assertEqual({result.status for result in results.values()}, {"success"})
                self.assertIn(APP_MODELS_LINES[1], results["read"].text)
                self.assertIn(APP_MODELS_LINES[3], results["read"].text)
                self.assertNotIn(APP_MODELS_LINES[4], results["read"].text)
                if not delegate:
                    self.assertIn("/apps/enabled_app/models.py", results["ls"].text)
                    self.assertIn("/apps/enabled_app/views/handlers.py", results["glob"].text)
                    self.assertNotIn("nested_app", results["glob"].text)
                    self.assertNotIn("sibling_pkg", results["glob"].text)
                    self.assertIn("/apps/enabled_app/views/handlers.py", results["grep"].text)
                    self.assertNotIn("models.py", results["grep"].text)
                    self.assertIn("/apps/enabled_app/templates/page.html", results["grep-html"].text)
                    self.assertIn("/apps/nested_app/models.py", results["nested-ls"].text)
                    self.assertNotIn("secret.py", results["nested-ls"].text)
                    self.assertIn("4.5.6-nested", results["nested-read"].text)
                    self.assertIn("9.9.9-test", results["core-read"].text)
                self.assertEqual(snapshot_tree(self.tree.base), before)
                self.assert_nothing_outside_reached_the_model(sentinels=APP_OUTSIDE_SENTINELS)
                self.assert_tool_surface()

    def test_core_only_and_exact_dotted_package_grants_are_enforced_in_main_and_delegate(self):
        enable_agent_settings(self, source_apps_by_group={"Unassigned": ["enabled_app"]})
        core_user = get_user_model().objects.create_user(username="source-core-only")
        calls = {
            "core": ("read_file", {"file_path": "/nautobot/__init__.py"}),
            "enabled": ("read_file", {"file_path": "/apps/enabled_app/models.py"}),
            "nested": ("read_file", {"file_path": "/apps/nested_app/__init__.py"}),
            "search": ("grep", {"pattern": "SENTINEL", "path": "/apps/enabled_app/"}),
            "listing": ("ls", {"path": "/apps/enabled_app/"}),
            "glob": ("glob", {"pattern": "**/*.py", "path": "/apps/enabled_app/"}),
            "traversal": ("read_file", {"file_path": "/apps/nested_app/../enabled_app/models.py"}),
        }

        self.user = core_user
        for delegate in (False, True):
            with self.subTest(policy="core-only", delegate=delegate):
                mode_calls = select_calls(calls, ("core", "enabled"), delegate)
                results = self.run_turn(mode_calls, delegate=delegate)
                self.assertEqual(results["core"].status, "success")
                self.assert_denied(results["enabled"])
                self.assertNotIn(APP_MODELS_LINES[0], self.scripted_model.request_text())
                if not delegate:
                    for call_id in ("nested", "search", "listing", "glob", "traversal"):
                        self.assert_denied(results[call_id], expect_permission=call_id != "traversal")
        graph = self.start_graph([AIMessage(content="Done.")], app_source_backends=self.app_backends)
        downloads = []
        backend = SourceBackend(self.catalog, (), ())
        self.scripted_model.on_call = lambda _index: downloads.extend(
            backend.download_files(["/apps/enabled_app/models.py"])
        )
        self.invoke(graph)
        self.assertIsNone(downloads[0].content)
        self.assertEqual(downloads[0].error, "file_not_found")

        nested_group = Group.objects.create(name="Nested Package Readers")
        original_group = Group.objects.create(name="Enabled App Readers")
        self.user.groups.add(nested_group, original_group)
        enable_agent_settings(
            self,
            source_apps_by_group={
                original_group.name: ["enabled_app"],
                nested_group.name: ["vendor.nested_app", "vendor.nested_app"],
            },
        )
        expected = ("enabled_app", "vendor.nested_app")
        self.assertEqual(resolve_source_apps(self.user, self.catalog), expected)
        self.user.groups.remove(original_group)
        self.assertEqual(resolve_source_apps(self.user, self.catalog), ("vendor.nested_app",))
        results = self.run_turn({"enabled": calls["enabled"], "nested": calls["nested"]})
        self.assert_denied(results["enabled"])
        self.assertEqual(results["nested"].status, "success")
        self.assertIn("4.5.6-nested", results["nested"].text)
        self.assertNotIn(APP_MODELS_LINES[0], self.scripted_model.request_text())
        self.user.groups.add(original_group)
        self.assertEqual(resolve_source_apps(self.user, self.catalog), expected)

        # Registry label "nested_app" differs from the configured package identity "vendor.nested_app".
        enable_agent_settings(self, source_apps_by_group={nested_group.name: ["nested_app"]})
        self.assertEqual(resolve_source_apps(self.user, self.catalog), ())
        graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/nested_app/__init__.py"}, "wrong-label"),
                AIMessage(content="Done."),
            ],
            app_source_backends=self.app_backends,
        )
        state, _ = self.invoke(graph)
        self.assert_denied(self.tool_results(state["messages"])["wrong-label"])

    def test_grant_transitions_and_superuser_identities(self):  # pylint: disable=too-many-locals,too-many-statements
        # Fresh group membership and captured-graph access transitions.
        user = self.user
        group = user.groups.get(name="Source Graph App Readers")
        second_group = Group.objects.create(name="Equivalent Source Readers")
        second_user = get_user_model().objects.create_user(username="source-equivalent-user")
        second_user.groups.add(second_group)
        prefetch_user = get_user_model().objects.prefetch_related("groups").get(pk=user.pk)
        access = list(self.app_backends)
        enable_agent_settings(self, source_apps_by_group={group.name: access, second_group.name: access})
        self.assertEqual(resolve_source_apps(prefetch_user, self.catalog), tuple(sorted(self.app_backends)))
        graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "captured"),
                AIMessage(content="Captured graph finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "captured-after-edit"),
                AIMessage(content="Captured graph finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "removed"),
                AIMessage(content="Narrow graph finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "broad-again"),
                AIMessage(content="Broad graph finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "narrow-again"),
                AIMessage(content="Narrow graph finished."),
            ],
            app_source_backends=self.app_backends,
        )
        captured_state, _ = self.invoke(graph)
        self.assertEqual(self.tool_results(captured_state["messages"])["captured"].status, "success")

        user.groups.remove(group)
        # A graph captured before the membership edit retains its admitted App routes until its turn finishes.
        captured_state, _ = self.invoke(graph)
        self.assertEqual(self.tool_results(captured_state["messages"])["captured-after-edit"].status, "success")
        # The next lookup observes fresh membership even on the prefetched identity.
        self.assertEqual(resolve_source_apps(prefetch_user, self.catalog), ())
        narrowed = runtime_graph(user)
        narrowed_state, _ = self.invoke(narrowed)
        self.assert_denied(self.tool_results(narrowed_state["messages"])["removed"])
        broad_again = runtime_graph(second_user)
        broad_state, _ = self.invoke(broad_again)
        self.assertEqual(self.tool_results(broad_state["messages"])["broad-again"].status, "success")
        narrow_again = runtime_graph(user)
        narrow_state, _ = self.invoke(narrow_again)
        self.assert_denied(self.tool_results(narrow_state["messages"])["narrow-again"])

        self.scripted_model.responses = [
            tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "equivalent-grant"),
            AIMessage(content="Equivalent grant read completed."),
        ]
        equivalent_graph = runtime_graph(second_user)
        equivalent_state, _ = self.invoke(
            equivalent_graph,
            context=AgentContext(
                user_id=str(second_user.pk),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
        )
        self.assertEqual(self.tool_results(equivalent_state["messages"])["equivalent-grant"].status, "success")

        # Superuser promotion and demotion update source access on subsequent turns.
        enable_agent_settings(self, source_apps_by_group={})
        user = get_user_model().objects.create_user(username="source-promoted-user")
        self.user = user
        core_graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "before-promotion"),
                AIMessage(content="Core-only reply finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "after-promotion"),
                AIMessage(content="Superuser reply finished."),
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "after-demotion"),
                AIMessage(content="Narrow reply finished."),
            ],
            app_source_backends=self.app_backends,
        )
        core_state, _ = self.invoke(core_graph)
        self.assert_denied(self.tool_results(core_state["messages"])["before-promotion"])

        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
        promoted = runtime_graph(user)
        promoted_state, _ = self.invoke(promoted)
        self.assertEqual(self.tool_results(promoted_state["messages"])["after-promotion"].status, "success")

        user.is_superuser = False
        user.save(update_fields=["is_superuser"])
        demoted = runtime_graph(user)
        demoted_state, _ = self.invoke(demoted)
        self.assert_denied(self.tool_results(demoted_state["messages"])["after-demotion"])

        # An active superuser without staff or groups reads every eligible App; staff alone does not.
        enable_agent_settings(self, source_apps_by_group={"Unrelated Group": ["enabled_app"]})
        superuser = get_user_model().objects.create_user(username="source-superuser", is_superuser=True, is_staff=False)
        self.user = superuser
        with self.assertNumQueries(0):
            self.assertEqual(resolve_source_apps(superuser, self.catalog), tuple(sorted(self.app_backends)))
        graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/nested_app/__init__.py"}, "superuser-nested"),
                AIMessage(content="Done."),
            ],
            app_source_backends=self.app_backends,
        )
        state, _ = self.invoke(graph)
        result = self.tool_results(state["messages"])["superuser-nested"]
        self.assertEqual(result.status, "success")
        self.assertIn("4.5.6-nested", result.text)
        reader_group = Group.objects.create(name="All Eligible App Readers")
        reader = get_user_model().objects.create_user(username="source-equal-superuser")
        reader.groups.add(reader_group)
        enable_agent_settings(self, source_apps_by_group={reader_group.name: list(self.app_backends)})
        reader_graph = runtime_graph(reader)
        reader_state, _ = self.invoke(
            reader_graph,
            context=AgentContext(
                user_id=str(reader.pk),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
        )
        self.assertEqual(self.tool_results(reader_state["messages"])["superuser-nested"].status, "success")

        staff_only = get_user_model().objects.create_user(username="source-staff", is_staff=True)
        staff_group = Group.objects.create(name="Staff With No Source Grant")
        staff_only.groups.add(staff_group)
        self.user = staff_only
        enable_agent_settings(self, source_apps_by_group={})
        self.assertEqual(resolve_source_apps(staff_only, self.catalog), ())
        graph = self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "staff-denied"),
                AIMessage(content="Done."),
            ],
            app_source_backends=self.app_backends,
        )
        state, _ = self.invoke(graph)
        self.assert_denied(self.tool_results(state["messages"])["staff-denied"])

        # Invalid superuser identities never resolve source grants.
        mapping = {"Readers": list(self.app_backends)}
        enable_agent_settings(self, source_apps_by_group=mapping)
        inactive = get_user_model().objects.create_user(username="source-inactive", is_superuser=True, is_active=False)
        unsaved = get_user_model()(username="source-unsaved", is_superuser=True, is_active=True)
        anonymous = AnonymousUser()
        anonymous.is_superuser = True
        for principal in (anonymous, inactive, unsaved, None):
            with self.subTest(principal=type(principal).__name__):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_source_apps(principal, self.catalog)

    def test_parent_internal_disabled_sibling_and_traversal_paths_are_denied(self):
        outside = self.app_tree.outside
        denied_by_permission = {
            "ls-apps": ("ls", {"path": "/apps"}),
            "ls-apps-slash": ("ls", {"path": "/apps/"}),
            "glob-apps": ("glob", {"pattern": "*", "path": "/apps"}),
            "grep-apps": ("grep", {"pattern": "needle", "path": "/apps"}),
            "ls-root": ("ls", {"path": "/"}),
            "grep-root": ("grep", {"pattern": "needle", "path": "/"}),
            "ls-disabled": ("ls", {"path": "/apps/disabled_app/"}),
            "glob-disabled": ("glob", {"pattern": "*", "path": "/apps/disabled_app"}),
            "grep-disabled": ("grep", {"pattern": "SENTINEL", "path": "/apps/disabled_app/"}),
            "read-disabled": ("read_file", {"file_path": "/apps/disabled_app/secret.py"}),
            "read-unrelated": ("read_file", {"file_path": "/apps/unrelated_django/secret.py"}),
            "read-unregistered": ("read_file", {"file_path": "/apps/unregistered_app/secret.py"}),
            "read-vendor-parent": ("read_file", {"file_path": "/apps/vendor/secret.py"}),
            "read-sibling": ("read_file", {"file_path": "/sibling_pkg/secret.py"}),
            "read-internal": ("read_file", {"file_path": SEEDED_PATH}),
            "read-history": ("read_file", {"file_path": "/conversation_history/session_seeded.md"}),
            "read-blob": ("read_file", {"file_path": "/blobs/seeded.bin"}),
            "read-etc": ("read_file", {"file_path": "/etc/passwd"}),
            "grep-history": ("grep", {"pattern": INTERNAL_SENTINEL, "path": "/conversation_history"}),
        }
        refused = {
            "glob-pathless": ("glob", {"pattern": "**/*.py"}),
            "traversal-disabled": ("read_file", {"file_path": "/apps/enabled_app/../disabled_app/secret.py"}),
            "traversal-vendor": ("read_file", {"file_path": "/apps/nested_app/../vendor/secret.py"}),
            "traversal-sibling": ("read_file", {"file_path": "/apps/enabled_app/../../sibling_pkg/secret.py"}),
            "traversal-backslash": ("read_file", {"file_path": "/apps/enabled_app\\..\\..\\sibling_pkg\\secret.py"}),
            "traversal-search": ("grep", {"pattern": "needle", "path": "/apps/enabled_app/../disabled_app"}),
            "traversal-core-project": ("read_file", {"file_path": "/nautobot/../../project/secret.txt"}),
            "nested-parent-file": ("read_file", {"file_path": "/apps/nested_app/secret.py"}),
            "absolute-disabled": ("read_file", {"file_path": str(outside["disabled"])}),
            "absolute-sibling": ("read_file", {"file_path": outside["sibling"].as_posix()}),
        }
        representative = ("ls-apps", "traversal-sibling")
        before = snapshot_tree(self.tree.base)

        for delegate in (False, True):
            with self.subTest(delegate=delegate):
                mode_calls = select_calls({**denied_by_permission, **refused}, representative, delegate)
                results = self.run_turn(mode_calls, delegate=delegate)

                for call_id in denied_by_permission.keys() & mode_calls.keys():
                    self.assert_denied(results[call_id])
                for call_id in refused.keys() & mode_calls.keys():
                    self.assert_denied(results[call_id], expect_permission=False)
                for call_id in ("ls-apps", "ls-apps-slash", "glob-apps", "grep-apps", "ls-root", "grep-root"):
                    if call_id not in mode_calls:
                        continue
                    for mounted in ("enabled_app", "nested_app", "/nautobot/"):
                        self.assertNotIn(mounted, results[call_id].text)
                self.assertEqual(snapshot_tree(self.tree.base), before)
                self.assert_nothing_outside_reached_the_model(sentinels=APP_OUTSIDE_SENTINELS, allow_location=True)
                self.assert_tool_surface()

    def test_denied_grep_inputs_do_not_search_source_backends_in_main_or_delegate(self):
        group = self.user.groups.get(name="Source Graph App Readers")
        enable_agent_settings(self, source_apps_by_group={group.name: ["enabled_app"]})
        enabled_backend = self.app_backends["enabled_app"].backend
        nested_backend = self.app_backends["vendor.nested_app"].backend
        denied_calls = {
            "root": ("grep", {"pattern": "needle", "path": "/"}),
            "apps": ("grep", {"pattern": "needle", "path": "/apps"}),
            "unauthorized-mount": ("grep", {"pattern": "needle", "path": "/apps/nested_app/"}),
            "traversal": ("grep", {"pattern": "needle", "path": "/apps/enabled_app/../nested_app"}),
        }

        for delegate in (False, True):
            with self.subTest(delegate=delegate):
                for call_id, call in denied_calls.items():
                    with self.subTest(call=call_id), ExitStack() as stack:
                        search = stack.enter_context(
                            patch.object(CompositeBackend, "grep", autospec=True, side_effect=CompositeBackend.grep)
                        )
                        enabled_search = stack.enter_context(
                            patch.object(enabled_backend, "grep", wraps=enabled_backend.grep)
                        )
                        nested_search = stack.enter_context(
                            patch.object(nested_backend, "grep", wraps=nested_backend.grep)
                        )
                        results = self.run_turn({call_id: call}, delegate=delegate)
                        self.assert_denied(
                            results[call_id],
                            expect_permission=call_id != "traversal",
                        )
                        enabled_search.assert_not_called()
                        nested_search.assert_not_called()
                        search.assert_not_called()

                with ExitStack() as stack:
                    enabled_search = stack.enter_context(
                        patch.object(enabled_backend, "grep", wraps=enabled_backend.grep)
                    )
                    result = self.run_turn(
                        {"valid": ("grep", {"pattern": "needle", "path": "/apps/enabled_app/views"})},
                        delegate=delegate,
                    )["valid"]
                    self.assertEqual(result.status, "success")
                    self.assertIn("/apps/enabled_app/views/handlers.py", result.text)
                    enabled_search.assert_called_once()

    def test_async_grep_tools_reject_denied_paths_before_backend_search(self):
        enabled_backend = self.app_backends["enabled_app"].backend
        calls = [
            ("grep", {"pattern": "needle", "path": "/"}),
            ("grep", {"pattern": "needle", "path": "/apps/nested_app/"}),
            ("grep", {"pattern": "needle", "path": "/apps/enabled_app/../nested_app"}),
            ("grep", {"pattern": "needle", "path": "/apps/enabled_app/views"}),
        ]
        backend = SourceBackend(self.catalog, ("enabled_app",), ())
        filesystem = SourceAccessMiddleware(backend=backend)
        grep = next(tool for tool in filesystem.tools if tool.name == "grep")
        runtime = ToolRuntime(
            state={},
            context=None,
            config={},
            stream_writer=lambda _value: None,
            tool_call_id="async-grep",
            store=None,
        )

        with (
            patch.object(backend, "agrep", wraps=backend.agrep) as search,
            patch.object(enabled_backend, "grep", wraps=enabled_backend.grep) as enabled_search,
            patch.object(
                self.app_backends["vendor.nested_app"].backend,
                "grep",
                wraps=self.app_backends["vendor.nested_app"].backend.grep,
            ) as nested_search,
        ):
            for index, (name, arguments) in enumerate(calls):
                call = {
                    "name": name,
                    "args": {**arguments, "runtime": runtime},
                    "id": f"async-{index}",
                    "type": "tool_call",
                }
                with self.subTest(arguments=arguments):
                    result = async_to_sync(grep.ainvoke)(call)
                    if index < 3:
                        self.assert_denied(result, expect_permission=index != 2)
                    else:
                        self.assertEqual(result.status, "success")
                        self.assertIn("/apps/enabled_app/views/handlers.py", result.text)
                    if index < 3:
                        search.assert_not_awaited()
                        enabled_search.assert_not_called()
                        nested_search.assert_not_called()
        enabled_search.assert_called_once()
        nested_search.assert_not_called()
        search.assert_awaited_once()

    def test_pathless_grep_returns_only_permitted_mounted_sources_in_main_and_delegate(self):
        group = self.user.groups.get(name="Source Graph App Readers")
        enable_agent_settings(self, source_apps_by_group={group.name: ["enabled_app"]})
        calls = {
            "omitted": ("grep", {"pattern": "needle", "output_mode": "content"}),
            "null": ("grep", {"pattern": "needle", "path": None, "output_mode": "content"}),
            "internal": ("grep", {"pattern": "INTERNAL-STATE", "output_mode": "content"}),
            "outside": ("grep", {"pattern": "SENTINEL", "output_mode": "content"}),
        }
        for delegate in (False, True):
            with self.subTest(delegate=delegate):
                results = self.run_turn(calls, delegate=delegate)
                for call_id in ("omitted", "null"):
                    self.assertEqual(results[call_id].status, "success")
                    self.assertIn("/nautobot/models.py", results[call_id].text)
                    self.assertIn("/apps/enabled_app/models.py", results[call_id].text)
                    self.assertNotIn("/apps/nested_app/", results[call_id].text)
                for call_id in ("internal", "outside"):
                    self.assertEqual(results[call_id].status, "success")
                    self.assertNotIn("seeded", results[call_id].text)
                self.assert_nothing_outside_reached_the_model(sentinels=APP_OUTSIDE_SENTINELS)

    def test_grep_keeps_mounted_matches_when_a_file_or_granted_app_is_unavailable(self):
        group = self.user.groups.get(name="Source Graph App Readers")
        root = self.app_tree.source.site / "enabled_app"
        bad = root / "bad.py"
        original_open = type(bad).open

        def open_source(path, *args, **kwargs):
            if path == bad:
                raise PermissionError("unreadable source")
            return original_open(path, *args, **kwargs)

        for failure in ("unavailable app", "unreadable file", "invalid UTF-8"):
            packages = ["enabled_app", "missing_app"] if failure == "unavailable app" else ["enabled_app"]
            enable_agent_settings(self, source_apps_by_group={group.name: packages})
            if failure == "invalid UTF-8":
                bad.write_bytes(b"filler\n" * 2000 + b"\xffneedle invalid\n")
            elif failure == "unreadable file":
                bad.write_text("needle unreadable\n")
            with self.subTest(failure=failure), ExitStack() as stack:
                if failure == "unreadable file":
                    stack.enter_context(patch.object(type(bad), "open", open_source))
                results = self.run_turn(
                    {
                        "pathless": ("grep", {"pattern": "needle", "output_mode": "content"}),
                        "scoped": (
                            "grep",
                            {"pattern": "needle", "path": "/apps/enabled_app/", "output_mode": "content"},
                        ),
                    }
                )
                for result in results.values():
                    self.assertEqual(result.status, "success")
                    self.assertIn("/apps/enabled_app/models.py", result.text)
                    self.assertNotIn("bad.py", result.text)
                    self.assertNotIn(str(root), result.text)

    def test_interleaved_graphs_keep_disjoint_captured_source_grants(self):
        enabled_group = self.user.groups.get(name="Source Graph App Readers")
        nested_group = Group.objects.create(name="Disjoint Nested Source Readers")
        nested_user = get_user_model().objects.create_user(username="source-disjoint-nested")
        enabled_user = self.user
        nested_user.groups.add(nested_group)
        enable_agent_settings(
            self,
            source_apps_by_group={
                enabled_group.name: ["enabled_app"],
                nested_group.name: ["vendor.nested_app"],
            },
        )

        enabled_script = [
            tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "enabled-first"),
            tool_call_message("read_file", {"file_path": "/apps/nested_app/__init__.py"}, "enabled-denied"),
            AIMessage(content="Enabled source revisited."),
        ]
        nested_script = [
            tool_call_message("read_file", {"file_path": "/apps/nested_app/__init__.py"}, "nested-first"),
            tool_call_message("read_file", {"file_path": "/apps/enabled_app/models.py"}, "nested-denied"),
            AIMessage(content="Nested source revisited."),
        ]
        enabled_graph = self.start_graph(enabled_script, app_source_backends=self.app_backends)
        self.user = nested_user
        nested_graph = self.start_graph(nested_script, app_source_backends=self.app_backends)

        def run(graph, user):
            self.user = user
            state, _ = self.invoke(
                graph,
                context=AgentContext(
                    user_id=str(user.pk),
                    authority=AllowAllAuthority(),
                    budget=RunBudget(tool_limit=100, model_limit=40),
                ),
            )
            return state

        enabled_state = run(enabled_graph, enabled_user)
        nested_state = run(nested_graph, nested_user)
        enabled_again = run(enabled_graph, enabled_user)
        nested_again = run(nested_graph, nested_user)

        for state in (enabled_state, enabled_again):
            self.assertEqual(self.tool_results(state["messages"])["enabled-first"].status, "success")
            self.assert_denied(self.tool_results(state["messages"])["enabled-denied"])
        for state in (nested_state, nested_again):
            self.assertEqual(self.tool_results(state["messages"])["nested-first"].status, "success")
            self.assert_denied(self.tool_results(state["messages"])["nested-denied"])
