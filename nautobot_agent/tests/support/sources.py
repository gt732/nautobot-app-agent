"""Temporary source trees and App-discovery test fixtures."""

import os
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from types import ModuleType
from typing import NamedTuple
from unittest.mock import patch

from django.apps import AppConfig
from django.test import SimpleTestCase
from nautobot.apps import NautobotAppConfig

from nautobot_agent.sources import backends
from nautobot_agent.sources.backends import SourceCatalog

## Temporary source and enabled-App fixtures
SIBLING_SENTINEL = "SIBLING-PACKAGE-SENTINEL-7f3a"
PROJECT_SENTINEL = "PROJECT-DIRECTORY-SENTINEL-91bc"
INSTANCE_SENTINEL = "INSTANCE-DATA-SENTINEL-c2d4"
HOME_SENTINEL = "HOME-DIRECTORY-SENTINEL-5e60"
OUTSIDE_SENTINELS = (SIBLING_SENTINEL, PROJECT_SENTINEL, INSTANCE_SENTINEL, HOME_SENTINEL)

DISABLED_SENTINEL = "DISABLED-APP-SENTINEL-3d19"
UNRELATED_SENTINEL = "UNRELATED-PACKAGE-SENTINEL-8a42"
PARENT_SENTINEL = "NESTED-PARENT-PACKAGE-SENTINEL-60ce"
APP_OUTSIDE_SENTINELS = (*OUTSIDE_SENTINELS, DISABLED_SENTINEL, UNRELATED_SENTINEL, PARENT_SENTINEL)
APP_MODELS_LINES = [f"app models line {number:02d}" + (" needle" if number % 2 == 0 else "") for number in range(1, 7)]
APP_MOUNTED_PACKAGES = (
    "enabled_app",
    "vendor.nested_app",
    "missing_app",
    "namespace_app",
    "archive_app",
    "ambiguous_app",
)
APP_AVAILABLE_ROOTS = ("/apps/enabled_app/", "/apps/nested_app/")

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
MODELS_LINES = [f"models line {number:02d}" + (" needle" if number % 2 == 0 else "") for number in range(1, 11)]
LARGE_LINE_COUNT = 1


class SourceTree(NamedTuple):
    """Temporary installation-like layout: the package plus unrelated neighbors that must stay unreadable."""

    base: Path
    site: Path
    package: Path
    outside: dict[str, Path]


def create_source_tree(base: Path) -> SourceTree:
    """Create a fake ``nautobot`` package beside a sibling package, project, instance, and home directory."""
    site = base / "site"
    package = site / "nautobot"
    for directory in (package / "core", package / "templates", package / "static", site / "sibling_pkg"):
        directory.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "9.9.9-test"\n')
    (package / "models.py").write_text("\n".join(MODELS_LINES) + "\n")
    (package / "core" / "views.py").write_text('def render_needle():\n    return "core needle"\n')
    (package / "templates" / "page.html").write_text("<p>template needle</p>\n")
    (package / "static" / "logo.png").write_bytes(PNG_BYTES)
    large = "".join(f"large line {n:04d} {'x' * 80}\n" for n in range(1, LARGE_LINE_COUNT + 1))
    (package / "large.py").write_text(large)

    outside = {
        "sibling": site / "sibling_pkg" / "secret.py",
        "project": base / "project" / "secret.txt",
        "instance": base / "instance" / "secret.txt",
        "home": base / "home" / "user" / ".ssh" / "id_test",
    }
    contents = dict(zip(outside, OUTSIDE_SENTINELS, strict=True))
    for name, path in outside.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{contents[name]} needle\n")
    (site / "sibling_pkg" / "__init__.py").write_text("")
    return SourceTree(base=base, site=site, package=package, outside=outside)


def create_source_fixture(case) -> SourceTree:
    """Provide an owned temporary source tree for an explicitly constructed catalog."""
    temporary = tempfile.mkdtemp(prefix="nbagent-src-")
    case.addCleanup(rmtree, temporary)
    return create_source_tree(Path(temporary).resolve())


def loaded_package_module(
    name: str, package: Path, *, initializer: Path | str | bool | None = None, locations=None
) -> ModuleType:
    """Create a loaded-package-shaped module without importing or executing temporary source code."""
    module = ModuleType(name)
    module.__path__ = [str(location) for location in (locations if locations is not None else [package])]
    if initializer is not False:
        module.__file__ = str(initializer if isinstance(initializer, Path | str) else package / "__init__.py")
    return module


def snapshot_tree(root: Path) -> dict:
    """Return every directory, file (with bytes), and link under root for exact before/after comparison."""
    entries = {}
    for current, directories, files in os.walk(root):
        for name in [*directories, *files]:
            path = Path(current, name)
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[key] = ("link", os.readlink(path))
            elif path.is_dir():
                entries[key] = ("dir", None)
            else:
                entries[key] = ("file", path.read_bytes())
    return entries


def create_link(case, link: Path, target: Path) -> None:
    """Create a symlink removed at cleanup; unlinking never touches what it points to."""
    link.symlink_to(target, target_is_directory=target.is_dir())
    case.addCleanup(link.unlink)


class AppTree(NamedTuple):
    """Temporary App packages beside the core package: enabled, nested, unavailable, disabled, and unrelated."""

    source: SourceTree
    configs: tuple[AppConfig, ...]
    plugins: tuple[str, ...]
    outside: dict[str, Path]


def app_config(config_class: type[AppConfig], name: str, module: ModuleType, path: Path) -> AppConfig:
    """Return a registered-style configuration with module metadata and an independent config path override."""
    config = config_class(name, module)
    config.path = str(path)
    return config


def create_app_tree(source: SourceTree) -> AppTree:  # pylint: disable=too-many-locals
    """Add App packages and their registered configurations beside the core package in ``source``.

    Enabled: a regular package, a package nested under a parent package, and four unusable ones (missing,
    namespace, archive, ambiguous). Disabled and unrelated packages hold sentinels that must stay unreadable.
    Configuration ``path`` values deliberately point at parent directories, which must never become a mount.
    """
    site = source.site
    enabled = site / "enabled_app"
    vendor = site / "vendor"
    nested = vendor / "nested_app"
    secret_homes = {
        "disabled": (site / "disabled_app", DISABLED_SENTINEL),
        "unrelated": (site / "unrelated_django", UNRELATED_SENTINEL),
        "unregistered": (site / "unregistered_app", UNRELATED_SENTINEL),
        "vendor": (vendor, PARENT_SENTINEL),
    }
    for directory in (enabled / "views", enabled / "templates", nested, *(home for home, _ in secret_homes.values())):
        directory.mkdir(parents=True, exist_ok=True)
    (enabled / "__init__.py").write_text('APP_VERSION = "1.2.3-app"\n')
    (enabled / "models.py").write_text("\n".join(APP_MODELS_LINES) + "\n")
    (enabled / "views" / "handlers.py").write_text('def render_app_needle():\n    return "handler needle"\n')
    (enabled / "templates" / "page.html").write_text("<p>app template needle</p>\n")
    (nested / "__init__.py").write_text('NESTED_VERSION = "4.5.6-nested"\n')
    (nested / "models.py").write_text("nested model needle\n")
    outside = dict(source.outside)
    for name, (home, sentinel) in secret_homes.items():
        (home / "__init__.py").write_text("")
        (home / "secret.py").write_text(f"{sentinel} needle\n")
        outside[name] = home / "secret.py"

    archive = source.base / "apps.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("archive_app/__init__.py", "")
    modules = {
        "enabled_app": loaded_package_module("enabled_app", enabled),
        "vendor.nested_app": loaded_package_module("vendor.nested_app", nested),
        "missing_app": loaded_package_module(
            "missing_app", site / "missing_app", initializer=site / "missing_app" / "__init__.py"
        ),
        "namespace_app": loaded_package_module("namespace_app", enabled, initializer=False),
        "archive_app": loaded_package_module(
            "archive_app", archive / "archive_app", initializer=archive / "archive_app" / "__init__.py"
        ),
        "ambiguous_app": loaded_package_module("ambiguous_app", enabled, locations=[enabled, nested]),
        "disabled_app": loaded_package_module("disabled_app", site / "disabled_app"),
        "unrelated_django": loaded_package_module("unrelated_django", site / "unrelated_django"),
    }
    configs = (
        app_config(NautobotAppConfig, "enabled_app", modules["enabled_app"], site),
        app_config(NautobotAppConfig, "vendor.nested_app", modules["vendor.nested_app"], vendor),
        app_config(NautobotAppConfig, "missing_app", modules["missing_app"], site / "missing_app"),
        app_config(NautobotAppConfig, "namespace_app", modules["namespace_app"], enabled),
        app_config(NautobotAppConfig, "archive_app", modules["archive_app"], archive),
        app_config(NautobotAppConfig, "ambiguous_app", modules["ambiguous_app"], enabled),
        app_config(NautobotAppConfig, "disabled_app", modules["disabled_app"], site),
        app_config(AppConfig, "unrelated_django", modules["unrelated_django"], site / "unrelated_django"),
    )
    # "unrelated_django" is registered but is not a Nautobot App; "unregistered_app" has no registration at all.
    plugins = (*APP_MOUNTED_PACKAGES, "unrelated_django", "unregistered_app")
    return AppTree(source=source, configs=configs, plugins=plugins, outside=outside)


@contextmanager
def patched_app_discovery(case, tree: AppTree):
    """Apply temporary registered Apps, trusted PLUGINS, and the loaded core module."""
    core_module = loaded_package_module("nautobot", tree.source.package)
    with (
        case.settings(PLUGINS=list(tree.plugins)),
        patch("nautobot_agent.sources.backends.apps.get_app_configs", return_value=list(tree.configs)),
        patch("nautobot_agent.sources.backends.nautobot", core_module),
    ):
        yield


def fixture_catalog(case, tree: SourceTree, *, source_available=True, app_sources=None):
    """Discover and prime an isolated catalog before restoring the installed package layout."""
    app_tree = AppTree(source=tree, configs=(), plugins=(), outside=tree.outside)
    with patched_app_discovery(case, app_tree):
        if not source_available:
            backends.nautobot.__dict__.pop("__path__", None)
        catalog = SourceCatalog()
        _ = catalog.core, catalog.apps
    catalog.apps.update(app_sources or {})
    return catalog


class TemporarySourceTestCase(SimpleTestCase):
    """Base class providing an isolated temporary installation layout and a clean discovery cache."""

    def setUp(self):
        super().setUp()
        self.tree = create_source_fixture(self)
        self.location_token = self.tree.base.name

    def assert_no_leak(self, value, *, allow_location=False):
        """Assert no outside sentinel (and, unless allowed, no physical location fragment) appears in a result."""
        text = repr(value)
        for sentinel in APP_OUTSIDE_SENTINELS:
            self.assertNotIn(sentinel, text)
        if not allow_location:
            self.assertNotIn(self.location_token, text)
