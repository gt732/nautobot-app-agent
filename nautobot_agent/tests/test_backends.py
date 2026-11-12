"""Tests for Nautobot package discovery and the read-only source backend."""

import base64
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from deepagents.backends.protocol import DEFAULT_GREP_TIMEOUT, GlobResult, GrepResult, LsResult, ReadResult

from nautobot_agent.sources.backends import (
    NautobotSourceBackend,
    NautobotSourceUnavailableError,
    SourceCatalog,
    UnavailableSourceBackend,
    source_root,
)
from nautobot_agent.tests.support.sources import (
    APP_AVAILABLE_ROOTS,
    APP_MOUNTED_PACKAGES,
    MODELS_LINES,
    PNG_BYTES,
    SIBLING_SENTINEL,
    TemporarySourceTestCase,
    create_app_tree,
    create_link,
    fixture_catalog,
    loaded_package_module,
    patched_app_discovery,
    snapshot_tree,
)


class SourceRootTests(TemporarySourceTestCase):
    """Loaded regular package modules resolve to one confined filesystem root."""

    def test_source_root_resolution(self):
        repository = self.tree.base / "checkout"
        package = repository / "nautobot"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (repository / "pyproject.toml").write_text("[tool]\n")
        layouts = {"site-packages": self.tree.package, "editable repository": package}
        for label, package_root in layouts.items():
            with self.subTest(layout=label):
                root = source_root(loaded_package_module("nautobot", package_root))
                self.assertEqual(root, package_root.resolve())
                self.assertNotEqual(root, package_root.parent.resolve())

        package = self.tree.package
        other = self.tree.site / "sibling_pkg"
        archive = self.tree.base / "site.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("nautobot/__init__.py", "")
        cases = {
            "plain module": ModuleType("nautobot"),
            "empty file": loaded_package_module("nautobot", package, initializer=""),
            "multiple package roots": loaded_package_module("nautobot", package, locations=[package, other]),
            "namespace package": loaded_package_module("nautobot", package, initializer=False),
            "archive-only package": loaded_package_module(
                "nautobot", archive / "nautobot", initializer=archive / "nautobot" / "__init__.py"
            ),
            "missing directory": loaded_package_module("nautobot", self.tree.base / "gone" / "nautobot"),
            "initializer outside location": loaded_package_module(
                "nautobot", package, initializer=other / "__init__.py"
            ),
            "search location that is a file": loaded_package_module(
                "nautobot", package / "models.py", initializer=package / "models.py"
            ),
        }
        for label, module in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(NautobotSourceUnavailableError) as raised:
                    source_root(module)
                self.assertNotIn(self.location_token, str(raised.exception))

        module = loaded_package_module("nautobot", package)
        for error in (OSError("secret location"), RuntimeError("secret location"), ValueError("secret location")):
            with self.subTest(error=type(error).__name__):
                with patch("nautobot_agent.sources.backends.Path.resolve", side_effect=error):
                    with self.assertRaises(NautobotSourceUnavailableError) as raised:
                        source_root(module)
                self.assertNotIn("secret location", str(raised.exception))


class ProjectStaticExclusionTests(TemporarySourceTestCase):
    """The core source mount omits collected static assets, not just HTML documentation."""

    def setUp(self):
        super().setUp()
        docs = self.tree.package / "project-static" / "docs"
        docs.mkdir(parents=True)
        (docs / "index.html").write_text("excluded-static-sentinel\n")
        (docs.parent / "bundle.js").write_text("excluded-static-sentinel\n")
        self.backend = fixture_catalog(self, self.tree).core

    def test_static_tree_is_excluded(self):
        listed = self.backend.ls("/")
        globbed = self.backend.glob("**/*")
        grepped = self.backend.grep("excluded-static-sentinel", "/")

        self.assertNotIn("/project-static/", [entry["path"] for entry in listed.entries or []])
        self.assertFalse(any("/project-static/" in entry["path"] for entry in globbed.matches or []))
        self.assertEqual(grepped.matches, [])
        self.assertIsNone(grepped.error)
        self.assertIn("/models.py", [entry["path"] for entry in globbed.matches or []])
        self.assertEqual(
            self.backend.grep("needle", "/", "core/*.py").matches,
            [
                {"path": "/core/views.py", "line": 1, "text": "def render_needle():"},
                {"path": "/core/views.py", "line": 2, "text": '    return "core needle"'},
            ],
        )
        for path in (
            "/project-static",
            "/project-static/docs",
            "/project-static/docs/index.html",
            "/project-static/bundle.js",
            "/PROJECT-STATIC/docs/index.html",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.backend.ls(path).error)
                self.assertTrue(self.backend.read(path).error)
                self.assertFalse(self.backend.glob("*", path).matches)
                self.assertFalse(self.backend.grep("excluded-static-sentinel", path).matches)
                response = self.backend.download_files([path])[0]
                self.assertTrue(response.error)
                self.assertIsNone(response.content)

        original = Path.iterdir

        def guarded_iterdir(path):
            if "project-static" in path.parts:
                self.fail("Source search traversed project-static.")
            return original(path)

        with patch.object(Path, "iterdir", guarded_iterdir):
            self.assertIsNone(self.backend.glob("**/*").error)
            self.assertEqual(self.backend.grep("excluded-static-sentinel", "/").matches, [])

    def test_links_into_static_tree_are_excluded(self):
        for junction in (False, True):
            alias = "static_junction" if junction else "static_symlink"
            with self.subTest(junction=junction):
                if junction and sys.platform != "win32":
                    self.skipTest("Native junctions exist only on Windows.")
                create_link(self, self.tree.package / alias, self.tree.package / "project-static", junction=junction)
                self.assertNotIn(f"/{alias}/", [entry["path"] for entry in self.backend.ls("/").entries or []])
                self.assertTrue(self.backend.ls(f"/{alias}").error)
                self.assertTrue(self.backend.read(f"/{alias}/docs/index.html").error)
                response = self.backend.download_files([f"/{alias}/docs/index.html"])[0]
                self.assertTrue(response.error)
                self.assertIsNone(response.content)
                self.assertFalse(self.backend.glob("**/*", f"/{alias}").matches)
                self.assertFalse(self.backend.grep("excluded-static-sentinel", f"/{alias}").matches)
                self.assertFalse(any(alias in item["path"] for item in self.backend.glob("**/*").matches or []))
                self.assertEqual(self.backend.grep("excluded-static-sentinel", "/").matches, [])

    def test_metadata_failures_preserve_matches_and_search_completion(self):
        before = self.tree.package / "before.py"
        bad = self.tree.package / "bad.py"
        after = self.tree.package / "after.py"
        before.write_text("needle before\n")
        bad.write_text("unreadable metadata\n")
        after.write_text("needle after\n")
        original_is_file = Path.is_file
        original_stat = Path.stat

        def run(entries):
            armed = False

            def is_file(path, *args, **kwargs):
                nonlocal armed
                result = original_is_file(path, *args, **kwargs)
                if path == bad and result:
                    armed = True
                return result

            def stat(path, *args, **kwargs):
                if path == bad and armed:
                    raise PermissionError("unreadable metadata")
                return original_stat(path, *args, **kwargs)

            with (
                patch.object(self.backend, "_source_entries", return_value=iter((entry, entry) for entry in entries)),
                patch.object(Path, "is_file", is_file),
                patch.object(Path, "stat", stat),
            ):
                return self.backend.grep("needle", "/")

        expected = [
            {"path": "/before.py", "line": 1, "text": "needle before"},
            {"path": "/after.py", "line": 1, "text": "needle after"},
        ]
        for case, entries, matches in (
            ("continues after bad entry", [before, bad, after], expected),
            ("exhausts after bad entry", [before, bad], expected[:1]),
        ):
            with self.subTest(case=case):
                result = run(entries)
                self.assertEqual(result.matches, matches)
                self.assertIsNone(result.error)

        original_iterdir = Path.iterdir

        def skip_templates(path):
            if path.name == "templates":
                raise PermissionError("unreadable directory")
            return original_iterdir(path)

        with patch.object(Path, "iterdir", skip_templates):
            result = self.backend.glob("**/*")
        self.assertIsNone(result.error)
        self.assertIn("/core/views.py", [entry["path"] for entry in result.matches or []])
        self.assertNotIn("/templates/page.html", [entry["path"] for entry in result.matches or []])


class EnabledAppSourceDiscoveryTests(TemporarySourceTestCase):
    """Only enabled, registered Nautobot Apps are mounted, each as its own confined inner package."""

    def setUp(self):
        super().setUp()
        self.app_tree = create_app_tree(self.tree)

    def discover(self):
        """Run real enumeration against the temporary loaded-module layout."""
        with patched_app_discovery(self, self.app_tree):
            return SourceCatalog().apps

    def test_enabled_app_mounts(self):
        backends = self.discover()
        self.assertCountEqual(backends, APP_MOUNTED_PACKAGES)
        self.assertEqual(
            {source.mount for source in backends.values() if isinstance(source.backend, NautobotSourceBackend)},
            set(APP_AVAILABLE_ROOTS),
        )
        with patched_app_discovery(self, self.app_tree._replace(plugins=())):
            self.assertEqual(SourceCatalog().apps, {})

        backend = backends["enabled_app"].backend
        root = backend.ls("/")
        init = backend.read("/__init__.py")
        assert root.entries is not None
        assert init.file_data is not None
        self.assertEqual(
            {entry["path"] for entry in root.entries}, {"/__init__.py", "/models.py", "/views/", "/templates/"}
        )
        self.assertIn("1.2.3-app", init.file_data["content"])
        searches = [
            backend.grep("needle"),
            backend.grep("SENTINEL", "/"),
            backend.glob("**/*"),
            backend.glob("../**/*"),
        ]
        for result in [root, init, *searches]:
            self.assert_no_leak(result)

        nested = backends["vendor.nested_app"].backend
        nested_root = nested.ls("/")
        everything = nested.glob("**/*")
        reads = [nested.read("/../secret.py"), nested.read("/secret.py")]
        searches = [nested.grep("needle"), nested.grep("SENTINEL", "/")]
        assert nested_root.entries is not None
        assert everything.matches is not None
        self.assertEqual({entry["path"] for entry in nested_root.entries}, {"/__init__.py", "/models.py"})
        self.assertEqual({match["path"] for match in everything.matches}, {"/__init__.py", "/models.py"})
        for read in reads:
            self.assertTrue(read.error)
            self.assertIsNone(read.file_data)
        for result in [nested_root, everything, *reads, *searches]:
            self.assert_no_leak(result)

    def test_unavailable_apps_get_path_free_adapters_and_do_not_affect_other_mounts(self):
        with self.assertLogs("nautobot_agent.backends", level="WARNING") as logs:
            backends = self.discover()
        for package in ("missing_app", "namespace_app", "archive_app", "ambiguous_app"):
            with self.subTest(package=package):
                backend = backends[package].backend
                results = [backend.ls("/"), backend.read("/__init__.py"), backend.glob("*")]
                self.assertIsInstance(backend, UnavailableSourceBackend)
                for result in results:
                    self.assertIn("unavailable", result.error)
                    self.assert_no_leak(result)
                search = backend.grep("x")
                self.assertEqual(search.matches, [])
                self.assertIsNone(search.error)
        self.assertNotIn(self.location_token, "\n".join(logs.output))
        self.assertIsInstance(backends["enabled_app"].backend, NautobotSourceBackend)
        self.assertIsInstance(backends["vendor.nested_app"].backend, NautobotSourceBackend)


class NautobotSourceBackendReadTests(TemporarySourceTestCase):
    """Public read operations return SDK-shaped content, pagination, and metadata for the package only."""

    def setUp(self):
        super().setUp()
        self.backend = NautobotSourceBackend(root=self.tree.package)

    def test_ls_read_download_contracts(self):
        root = self.backend.ls("/")
        core = self.backend.ls("/core")
        self.assertIsNone(root.error)
        assert root.entries is not None
        assert core.entries is not None
        self.assertEqual(
            {entry["path"] for entry in root.entries},
            {"/__init__.py", "/models.py", "/large.py", "/core/", "/templates/", "/static/"},
        )
        self.assertEqual({entry["path"] for entry in core.entries}, {"/core/views.py"})
        self.assert_no_leak(root)

        result = self.backend.read("/__init__.py")
        self.assertIsNone(result.error)
        assert result.file_data is not None
        self.assertEqual(result.file_data["content"], '__version__ = "9.9.9-test"\n')
        self.assertEqual(result.file_data["encoding"], "utf-8")
        self.assertEqual((result.total_lines, result.start_line, result.end_line), (1, 1, 1))
        self.assertIsNone(result.next_offset)

        first = self.backend.read("/models.py", offset=2, limit=3)
        last = self.backend.read("/models.py", offset=8, limit=5)
        assert first.file_data is not None
        assert last.file_data is not None
        self.assertEqual(first.file_data["content"], "\n".join(MODELS_LINES[2:5]) + "\n")
        self.assertEqual((first.total_lines, first.start_line, first.end_line, first.next_offset), (10, 3, 5, 5))
        self.assertEqual(last.file_data["content"], "\n".join(MODELS_LINES[8:]) + "\n")
        self.assertEqual((last.start_line, last.end_line), (9, 10))
        self.assertIsNone(last.next_offset)

        template = self.backend.read("/templates/page.html")
        image = self.backend.read("/static/logo.png")
        assert template.file_data is not None
        assert image.file_data is not None
        self.assertEqual(template.file_data["content"], "<p>template needle</p>\n")
        self.assertEqual(image.file_data["encoding"], "base64")
        self.assertEqual(base64.b64decode(image.file_data["content"]), PNG_BYTES)

        paths = ["/static/logo.png", "/missing.py", "/core", "/../sibling_pkg/secret.py"]
        responses = self.backend.download_files(paths)
        self.assertEqual([response.path for response in responses], paths)
        self.assertEqual(responses[0].content, PNG_BYTES)
        self.assertIsNone(responses[0].error)
        self.assertEqual(
            [response.error for response in responses[1:]],
            ["file_not_found", "is_directory", "invalid_path"],
        )
        for response in responses:
            self.assert_no_leak(response)
        self.assertTrue(all(response.content is None for response in responses[1:]))

    def test_glob_and_grep(self):
        everything = self.backend.glob("**/*.py")
        scoped = self.backend.glob("*.html", path="/templates")
        assert everything.matches is not None
        assert scoped.matches is not None
        self.assertEqual(
            {match["path"] for match in everything.matches},
            {"/__init__.py", "/models.py", "/large.py", "/core/views.py"},
        )
        self.assertEqual({match["path"] for match in scoped.matches}, {"/templates/page.html"})
        self.assertFalse(everything.truncated)

        everywhere = self.backend.grep("needle")
        in_core = self.backend.grep("needle", "/core")
        filtered = self.backend.grep("needle", "/", "*.html")
        regex_text = self.backend.grep("model.*02")
        assert everywhere.matches is not None
        assert in_core.matches is not None
        assert filtered.matches is not None
        self.assertEqual(
            {match["path"] for match in everywhere.matches}, {"/models.py", "/core/views.py", "/templates/page.html"}
        )
        self.assertEqual(
            [match["line"] for match in everywhere.matches if match["path"] == "/models.py"], [2, 4, 6, 8, 10]
        )
        self.assertEqual({match["path"] for match in in_core.matches}, {"/core/views.py"})
        self.assertEqual({match["path"] for match in filtered.matches}, {"/templates/page.html"})
        self.assertEqual(regex_text.matches, [])

        limited = self.backend.grep("needle", "/", "models.py", max_count=2)
        with_context = self.backend.grep("needle", "/", "models.py", max_count=1, context_lines=1)
        assert limited.matches is not None
        assert with_context.matches is not None
        self.assertEqual([match["line"] for match in limited.matches], [2, 4])
        self.assertTrue(limited.truncated)
        (match,) = with_context.matches
        self.assertEqual(match["text"], MODELS_LINES[1])
        assert "context_before" in match
        assert "context_after" in match
        self.assertEqual(match["context_before"], [{"line": 1, "text": MODELS_LINES[0]}])
        self.assertEqual(match["context_after"], [{"line": 3, "text": MODELS_LINES[2]}])
        self.assertTrue(with_context.truncated)

    def test_bytecode_caches_are_excluded_at_every_depth(self):
        for parent in (self.tree.package, self.tree.package / "core"):
            cache = parent / "__pycache__"
            cache.mkdir()
            (cache / "cached.py").write_text("cached-source-sentinel\n")
        self.assertFalse(any("__pycache__" in entry["path"] for entry in self.backend.glob("**/*").matches or []))
        self.assertEqual(self.backend.grep("cached-source-sentinel").matches, [])
        for path in ("/__pycache__", "/core/__pycache__"):
            with self.subTest(path=path):
                self.assertFalse(self.backend.glob("*", path).matches)
                self.assertFalse(self.backend.grep("cached-source-sentinel", path).matches)
                self.assertTrue(self.backend.read(f"{path}/cached.py").error)
                self.assertTrue(self.backend.download_files([f"{path}/cached.py"])[0].error)
        self.assertNotIn("/__pycache__/", [entry["path"] for entry in self.backend.ls("/").entries or []])
        self.assertNotIn("/core/__pycache__/", [entry["path"] for entry in self.backend.ls("/core").entries or []])

    def test_glob_budget_returns_sorted_partial_matches(self):
        """Return sorted results already found when the traversal budget expires."""
        entries = []
        for name in ("zeta.py", "alpha.py", "middle.py", "later.py"):
            entry = self.tree.package / name
            entry.write_text(f"{name}\n")
            entries.append(entry)
        with (
            patch.object(self.backend, "_source_entries", return_value=iter((entry, entry) for entry in entries)),
            patch("nautobot_agent.sources.backends.time.monotonic", side_effect=[0, 1, 2, 3, 6]),
        ):
            result = self.backend.glob("*.py")

        assert result.matches is not None
        self.assertEqual([match["path"] for match in result.matches], ["/alpha.py", "/middle.py", "/zeta.py"])
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncation_reason, "budget")

    def test_grep_budget_preserves_matches_before_expiry(self):
        """Keep prior matches when the budget expires between files or before line 2,048."""
        first = self.tree.package / "first.py"
        second = self.tree.package / "second.py"
        first.write_text("needle first\n")
        second.write_text("needle second\n")
        source = self.tree.package / "long.py"
        lines = ["filler\n"] * 2049
        lines[0] = "needle early\n"
        lines[2047] = "needle boundary\n"
        lines[2048] = "needle later\n"
        source.write_text("".join(lines))
        for label, entries, expected in (
            ("between files", (first, second), {"path": "/first.py", "line": 1, "text": "needle first"}),
            ("within file", (source,), {"path": "/long.py", "line": 1, "text": "needle early"}),
        ):
            with self.subTest(label):
                with (
                    patch.object(
                        self.backend, "_source_entries", return_value=iter((entry, entry) for entry in entries)
                    ),
                    patch(
                        "nautobot_agent.sources.backends.time.monotonic",
                        side_effect=[0, 1, DEFAULT_GREP_TIMEOUT + 1],
                    ),
                ):
                    result = self.backend.grep("needle")
                self.assertEqual(result.matches, [expected])
                self.assertTrue(result.truncated)
                self.assertIsNone(result.error)

    def test_grep_match_cap_carries_across_files_and_exact_cap_is_complete(self):
        """Carry remaining capacity across files and distinguish exact completion from overflow."""
        first = self.tree.package / "first.py"
        second = self.tree.package / "second.py"
        first.write_text("needle first one\nneedle first two\n")
        second.write_text("needle second one\nneedle second extra\n")

        def search(entries, max_count):
            """Run a public grep with a controlled traversal sequence."""
            with patch.object(self.backend, "_source_entries", return_value=iter((entry, entry) for entry in entries)):
                return self.backend.grep("needle", max_count=max_count)

        overflow = search((first, second), 3)
        self.assertEqual(
            overflow.matches,
            [
                {"path": "/first.py", "line": 1, "text": "needle first one"},
                {"path": "/first.py", "line": 2, "text": "needle first two"},
                {"path": "/second.py", "line": 1, "text": "needle second one"},
            ],
        )
        self.assertTrue(overflow.truncated)
        self.assertIsNone(overflow.error)

        exact = search((first, second), 4)
        self.assertEqual(
            exact.matches,
            [
                {"path": "/first.py", "line": 1, "text": "needle first one"},
                {"path": "/first.py", "line": 2, "text": "needle first two"},
                {"path": "/second.py", "line": 1, "text": "needle second one"},
                {"path": "/second.py", "line": 2, "text": "needle second extra"},
            ],
        )
        self.assertFalse(exact.truncated)

    def test_errors_are_sanitized_and_bugs_propagate(self):
        paths = ["/first.py", "/second.py"]
        for error in (OSError("failed at /physical/secret/location"), ValueError("bad /physical/secret/location")):
            with self.subTest(download=type(error).__name__):
                with patch("nautobot_agent.sources.backends.FilesystemBackend.download_files", side_effect=error):
                    responses = self.backend.download_files(paths)
                self.assertEqual([response.path for response in responses], paths)
                self.assertTrue(all(response.content is None for response in responses))
                self.assertTrue(all(response.error for response in responses))
                for response in responses:
                    self.assertNotIn("physical", repr(response))

        results = {
            "read missing": self.backend.read("/missing_module.py"),
            "read directory": self.backend.read("/core"),
            "ls missing": self.backend.ls("/missing_module"),
        }
        for label, result in results.items():
            with self.subTest(case=label):
                assert result.error is not None
                self.assertNotIn("missing_module", result.error)
                self.assert_no_leak(result)
        self.assertIsNone(results["read missing"].file_data)
        self.assertIsNone(results["ls missing"].entries)

        calls = {
            "ls": lambda: self.backend.ls("/"),
            "read": lambda: self.backend.read("/models.py"),
            "glob": lambda: self.backend.glob("*"),
            "grep": lambda: self.backend.grep("needle"),
        }
        for method, call in calls.items():
            target = (
                "nautobot_agent.sources.backends.NautobotSourceBackend._glob_source"
                if method == "glob"
                else f"nautobot_agent.sources.backends.FilesystemBackend.{method}"
            )
            for error in (ValueError("bad /physical/secret/location"), OSError("io /physical/secret/location")):
                with self.subTest(method=method, error=type(error).__name__):
                    with patch(target, side_effect=error):
                        result = call()
                    assert result.error is not None
                    self.assertNotIn("physical", result.error)
            with self.subTest(method=method, error="RuntimeError"):
                with patch(target, side_effect=RuntimeError("bug")):
                    with self.assertRaises(RuntimeError):
                        call()


class NautobotSourceBackendConfinementTests(TemporarySourceTestCase):
    """The package is the only readable tree: traversal, absolute, Windows, and link forms never escape it."""

    def setUp(self):
        super().setUp()
        self.backend = NautobotSourceBackend(root=self.tree.package)

    def outside_forms(self):
        """Return path forms that name existing outside data or other host locations."""
        sibling = self.tree.outside["sibling"]
        project = self.tree.outside["project"]
        return [
            "/../sibling_pkg/secret.py",
            "..\\sibling_pkg\\secret.py",
            str(sibling),
            "C:/Windows/win.ini",
            "//localhost/c$/Windows/win.ini",
            str(project),
        ]

    def test_outside_path_forms_cannot_read_list_or_search_outside_data(self):
        for form in self.outside_forms():
            with self.subTest(form=form):
                read = self.backend.read(form)
                results = [
                    read,
                    self.backend.ls(form),
                    self.backend.glob("*", path=form),
                    self.backend.glob("**/*", path=form),
                    self.backend.grep("needle", form),
                    self.backend.grep("SENTINEL", form, "*"),
                ]

                self.assertTrue(read.error)
                self.assertIsNone(read.file_data)
                for result in results:
                    self.assert_no_leak(result)

    def test_alternate_stream_forms_of_package_files_do_not_expose_other_content(self):
        for form in ("/__init__.py:hidden", "/models.py::$DATA", "/models.py:Zone.Identifier"):
            with self.subTest(form=form):
                result = self.backend.read(form)

                self.assert_no_leak(result)
                if result.file_data is not None:
                    expected = {'__version__ = "9.9.9-test"\n', "\n".join(MODELS_LINES) + "\n"}
                    self.assertIn(result.file_data["content"], expected)

    def test_unscoped_and_pattern_searches_never_reach_outside_data(self):
        results = [
            self.backend.grep(SIBLING_SENTINEL),
            self.backend.grep("needle", "/"),
            self.backend.glob("../*/*", path="/"),
            self.backend.ls("/"),
        ]

        for result in results:
            self.assert_no_leak(result)
            self.assertNotIn("sibling_pkg", repr(result))

    def assert_link_is_confined(self, link_name, outside_name):
        """Assert reads, listings, and searches through an outward link return nothing from outside the package."""
        outside = self.tree.outside[outside_name]
        paths = [f"/{link_name}", f"/{link_name}/{outside.name}"]
        results: list[ReadResult | LsResult | GlobResult | GrepResult] = [self.backend.read(path) for path in paths]
        results += [self.backend.ls(f"/{link_name}"), self.backend.ls("/")]
        results += [self.backend.glob("*", path=f"/{link_name}"), self.backend.glob("**/*")]
        results += [self.backend.grep("SENTINEL"), self.backend.grep("SENTINEL", f"/{link_name}")]
        results += [self.backend.grep("needle", "/", "*")]

        for result in results:
            self.assert_no_leak(result)
        for read in results[:2]:
            assert isinstance(read, ReadResult)
            self.assertTrue(read.error)
            self.assertIsNone(read.file_data)
        # Confinement does not break ordinary reads in the same package.
        result = self.backend.read("/__init__.py")
        assert result.file_data is not None
        self.assertIn("9.9.9-test", result.file_data["content"])

    def test_outward_links_are_confined(self):
        links = (
            ("outward_file", "project", False),
            ("outward_dir", "sibling", False),
            ("outward_junction", "sibling", True),
        )
        for link_name, outside_name, junction in links:
            with self.subTest(link=link_name):
                if junction and sys.platform != "win32":
                    self.skipTest("Native junctions exist only on Windows.")
                target = self.tree.outside[outside_name]
                target = target.parent if outside_name == "sibling" else target
                create_link(self, self.tree.package / link_name, target, junction=junction)
                self.assert_link_is_confined(link_name, outside_name)


class NautobotSourceBackendMutationTests(TemporarySourceTestCase):
    """Mutation methods refuse changes without changing the filesystem."""

    def adapters(self):
        """Return both source adapters; both must refuse mutation."""
        return {
            "mounted": NautobotSourceBackend(root=self.tree.package),
            "unavailable": UnavailableSourceBackend(reason="the source is not installed"),
        }

    def sync_calls(self, backend):
        """Return sync mutation calls against an existing file, a new file, and a missing file."""
        return {
            "write existing": lambda: backend.write("/models.py", "overwritten"),
            "write new": lambda: backend.write("/brand_new.py", "new"),
            "write outside": lambda: backend.write("/../sibling_pkg/new.py", "new"),
            "edit existing": lambda: backend.edit("/models.py", "models", "changed", replace_all=True),
            "edit missing": lambda: backend.edit("/missing.py", "a", "b"),
            "delete existing": lambda: backend.delete("/models.py"),
            "delete directory": lambda: backend.delete("/core"),
            "delete missing": lambda: backend.delete("/missing.py"),
        }

    def test_sync_mutations_are_refused(self):
        before = snapshot_tree(self.tree.base)

        for adapter_label, backend in self.adapters().items():
            for label, call in self.sync_calls(backend).items():
                with self.subTest(adapter=adapter_label, call=label):
                    result = call()

                    self.assertIn("read-only", result.error)
                    self.assertIsNone(result.path)
                    self.assertEqual(snapshot_tree(self.tree.base), before)
            responses = backend.upload_files([("/upload.py", b"upload")])
            self.assertEqual([(item.path, item.error) for item in responses], [("/upload.py", "permission_denied")])
            self.assertEqual(snapshot_tree(self.tree.base), before)
