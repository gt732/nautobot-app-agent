"""Confined read-only source mounts for installed Nautobot and enabled App packages."""

import logging
import time
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any

import nautobot
from deepagents.backends import FilesystemBackend, StateBackend
from deepagents.backends.protocol import (
    DEFAULT_GREP_TIMEOUT,
    FILE_NOT_FOUND,
    INVALID_PATH,
    PERMISSION_DENIED,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import InvalidGlobPatternError, compile_grep_include_glob
from django.apps import apps
from django.conf import settings
from nautobot.apps import NautobotAppConfig

from nautobot_agent.skills import SkillDefinition

logger = logging.getLogger("nautobot_agent.backends")

SOURCE_MOUNT = "/nautobot/"
APPS_MOUNT = "/apps/"

_READ_ONLY = "the source is read-only"


class NautobotSourceUnavailableError(Exception):
    """Raised when an installed Nautobot or App package cannot be used as a source root.

    The message is path-free and safe to show to the model.
    """


@dataclass(frozen=True)
class AppSource:
    """An eligible App's package identity, virtual mount, and confined source adapter."""

    mount: str
    backend: "NautobotSourceBackend | UnavailableSourceBackend"


def source_root(module: ModuleType) -> Path:
    """Return the confined root of a loaded regular package module."""
    package_name = module.__name__
    unsupported = f"the installed {package_name} package is not a single regular filesystem directory"
    locations = getattr(module, "__path__", None)
    initializer_path = getattr(module, "__file__", None)
    if locations is None:
        raise NautobotSourceUnavailableError(unsupported)
    locations = list(locations)
    if len(locations) != 1 or not isinstance(initializer_path, str) or not initializer_path:
        raise NautobotSourceUnavailableError(unsupported)

    try:
        root = Path(locations[0]).resolve(strict=True)
        initializer = Path(initializer_path).resolve(strict=True)
        if not root.is_dir() or not initializer.is_file() or initializer.parent != root:
            raise NautobotSourceUnavailableError(unsupported)
    except (OSError, RuntimeError, ValueError):
        # Filesystem exception text can contain physical paths; the safe boundary exposes only the fixed diagnostic.
        raise NautobotSourceUnavailableError(unsupported) from None
    return root


def enabled_app_configs() -> tuple[NautobotAppConfig, ...]:
    """Return registered Nautobot Apps enabled by settings, in registry order."""
    enabled = set(settings.PLUGINS)
    return tuple(
        config for config in apps.get_app_configs() if isinstance(config, NautobotAppConfig) and config.name in enabled
    )


def _source_path_error(operation: str) -> str:
    """Return a path-free error for an expected source operation failure."""
    return f"Error: unable to {operation} the requested Nautobot source path."


class _ReadOnlySourceBackend(BackendProtocol):  # pylint: disable=abstract-method
    """Share mutation refusals across source adapters."""

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Refuse every upload without mutating the source."""
        return [FileUploadResponse(path=path, error=PERMISSION_DENIED) for path, _ in files]

    def write(self, file_path: str, content: str) -> WriteResult:
        """Refuse writes."""
        return WriteResult(error=f"Error: {_READ_ONLY}; cannot write '{file_path}'.")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Refuse edits."""
        return EditResult(error=f"Error: {_READ_ONLY}; cannot edit '{file_path}'.")

    def delete(self, file_path: str) -> DeleteResult:
        """Refuse deletes."""
        return DeleteResult(error=f"Error: {_READ_ONLY}; cannot delete '{file_path}'.")


class SkillsBackend(_ReadOnlySourceBackend, StateBackend):
    """Expose captured skill documents without checkpoint-state fallback or mutation."""

    def __init__(self, definitions: tuple[SkillDefinition, ...]) -> None:
        """Capture one immutable skill-document snapshot."""
        self._files = {
            f"/{definition.identifier}/SKILL.md": {"content": definition.document, "encoding": "utf-8"}
            for definition in definitions
        }

    def _read_files(self) -> dict[str, Any]:
        return self._files


class NautobotSourceBackend(_ReadOnlySourceBackend, FilesystemBackend):
    """Read-only installed package source, excluding project-static and bytecode caches."""

    def __init__(self, root: Path) -> None:
        """Initialize a mounted package source."""
        super().__init__(root_dir=root, virtual_mode=True)

    def _check_source_path(self, path: Path) -> Path:
        relative = path.relative_to(self.cwd)
        if relative.parts and relative.parts[0].casefold() == "project-static":
            raise ValueError("Collected static assets are not mounted.")
        if "__pycache__" in relative.parts:
            raise ValueError("Bytecode caches are not mounted.")
        return path

    def _resolve_path(self, key: str) -> Path:
        self._check_source_path(self.cwd / key.lstrip("/"))
        return self._check_source_path(super()._resolve_path(key))

    def _to_virtual_path(self, path: Path, resolved: Path | None = None) -> str:
        self._check_source_path(path)
        resolved = self._check_source_path(path.resolve() if resolved is None else resolved)
        return "/" + resolved.relative_to(self.cwd).as_posix()

    def _source_entries(self, root: Path):
        """Yield entry/resolved-path pairs; prune exclusions and never follow directory links."""
        if root.is_file():
            yield root, root
            return
        pending = [root]
        while pending:
            try:
                for child in pending.pop().iterdir():
                    try:
                        self._check_source_path(child)
                        resolved = self._check_source_path(child.resolve())
                        descend = resolved == child and child.is_dir()
                    except (OSError, RuntimeError, ValueError) as error:
                        # Excluded paths and races are pruned without exposing host paths or aborting partial walks.
                        logger.debug("Skipping source entry during traversal (%s).", type(error).__name__)
                        continue
                    yield child, resolved
                    if descend:
                        pending.append(child)
            except (OSError, RuntimeError) as error:
                # A directory that cannot be enumerated is skipped; other readable subtrees remain available.
                logger.debug("Skipping unreadable source directory during traversal (%s).", type(error).__name__)
                continue

    def _glob_source(self, pattern: str, path: str | None = None) -> GlobResult:
        """Apply the SDK glob contract over the pruned source tree."""
        try:
            matcher = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError:
            return GlobResult(error=_source_path_error("search"))
        root = self._resolve_path(path or "/")
        if not root.is_dir():
            return GlobResult(matches=[])
        matches: list[FileInfo] = []
        deadline = time.monotonic() + 5
        for entry, resolved in self._source_entries(root):
            if time.monotonic() > deadline:
                return GlobResult(
                    matches=sorted(matches, key=lambda item: item["path"]), truncated=True, truncation_reason="budget"
                )
            try:
                if matcher(entry.relative_to(root).as_posix()) and entry.is_file():
                    matches.append({"path": self._to_virtual_path(entry, resolved), "is_dir": False})
            except (OSError, RuntimeError, ValueError) as error:
                # File metadata races should not discard matches collected elsewhere in this partial result.
                logger.debug("Skipping unreadable source entry during glob (%s).", type(error).__name__)
                continue
        return GlobResult(matches=sorted(matches, key=lambda item: item["path"]))

    def _ripgrep_search(self, pattern, base_full, include_glob, max_count=None):
        """Select the exclusion-aware walk rather than the SDK's unfiltered rg command."""
        # The pinned SDK cannot pass exclusions to rg. Use the pruned Python walk instead.
        return None, False

    def _search_source_file(self, entry: Path, pattern: str, remaining: int | None, deadline: float):
        """Stream one source file, retaining partial results on timeout or decoding failure."""
        matches = []
        try:
            with entry.open(encoding="utf-8", errors="strict") as handle:
                for number, line in enumerate(handle, 1):
                    if number % 2048 == 0 and time.monotonic() > deadline:
                        return matches, True
                    if pattern not in line:
                        continue
                    if remaining is not None and len(matches) >= remaining:
                        return matches, True
                    matches.append((number, line.rstrip("\n")))
        except (OSError, UnicodeDecodeError) as error:
            logger.debug("Skipping unreadable source file during search (%s).", type(error).__name__)
        return matches, False

    def _searchable(self, entry: Path, root: Path, matcher) -> bool:
        """Whether a walked entry is a size-bounded file selected by the include glob."""
        try:
            return (
                entry.is_file()
                and (matcher is None or matcher(entry.relative_to(root).as_posix()))
                and entry.stat().st_size <= self.max_file_size_bytes
            )
        except (OSError, RuntimeError) as exc:
            # Unreadable files are skipped while preserving successful matches from the rest of the walk.
            logger.debug("Skipping unreadable source entry during search (%s).", type(exc).__name__)
            return False

    def _python_search(self, pattern, base_full, include_glob, *, max_count=None, timeout=DEFAULT_GREP_TIMEOUT):
        """Keep SDK literal matching, caps and context without scanning collected assets."""
        matcher = compile_grep_include_glob(include_glob) if include_glob else None
        root = base_full if base_full.is_dir() else base_full.parent
        results = {}
        deadline = time.monotonic() + timeout
        try:
            for entry, resolved in self._source_entries(base_full):
                if time.monotonic() > deadline:
                    return results, True, None
                if not self._searchable(entry, root, matcher):
                    continue
                items, truncated = self._search_source_file(entry, pattern, max_count, deadline)
                if items:
                    results[self._to_virtual_path(entry, resolved)] = items
                    if max_count is not None:
                        max_count -= len(items)
                if truncated:
                    return results, True, None
        except (OSError, ValueError) as error:
            logger.debug("Skipping unreadable source path during search (%s).", type(error).__name__)
        return results, False, None

    @staticmethod
    def _sanitize_error(result, operation):
        """Retain partial results and metadata, but never expose raw filesystem diagnostics."""
        if result.error:
            return replace(result, error=_source_path_error(operation))
        return result

    def ls(self, path: str) -> LsResult:
        """List a directory of the Nautobot source."""
        try:
            result = super().ls(path)
        except (OSError, ValueError):
            return LsResult(error=_source_path_error("list"))
        return self._sanitize_error(result, "list")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a file of the Nautobot source."""
        try:
            result = super().read(file_path, offset, limit)
        except (OSError, ValueError):
            return ReadResult(error=_source_path_error("read"))
        return self._sanitize_error(result, "read")

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Search the Nautobot source for literal text."""
        try:
            result = super().grep(pattern, path, glob, max_count=max_count, context_lines=context_lines)
        except (OSError, ValueError):
            return GrepResult(error=_source_path_error("search"))
        return self._sanitize_error(result, "search")

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Find Nautobot source files matching a pattern."""
        try:
            return self._glob_source(pattern, path)
        except (OSError, ValueError):
            return GlobResult(error=_source_path_error("search"))

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from the package without allowing writes or exposing filesystem diagnostics."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                response = super().download_files([path])[0]
            except (OSError, ValueError):
                response = FileDownloadResponse(path=path, error=INVALID_PATH)
            responses.append(response)
        return responses


class UnavailableSourceBackend(_ReadOnlySourceBackend):
    """Return path-free errors for a discovered source package that has no usable root."""

    def __init__(self, reason: str) -> None:
        """Keep the safe discovery reason for operation errors."""
        self._reason = reason

    def _error(self) -> str:
        """Return the path-free unavailable-source message."""
        return f"Error: Nautobot source is unavailable: {self._reason}."

    def ls(self, path: str) -> LsResult:
        """Report that directory listings are unavailable."""
        return LsResult(error=self._error())

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Report that file reads are unavailable."""
        return ReadResult(error=self._error())

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Skip an unavailable mount without hiding matches from other sources."""
        del context_lines
        return GrepResult(matches=[])

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """Report that source globbing is unavailable."""
        return GlobResult(error=self._error())

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Return one standard missing-file response per requested path."""
        return [FileDownloadResponse(path=path, error=FILE_NOT_FOUND) for path in paths]


def _build_backend(module: ModuleType) -> NautobotSourceBackend | UnavailableSourceBackend:
    """Return the read-only adapter for one loaded package, or a path-free unavailable adapter."""
    package_name = module.__name__
    try:
        root = source_root(module)
    except NautobotSourceUnavailableError as error:
        logger.warning("Source access for package %s is unavailable: %s", package_name, error)
        return UnavailableSourceBackend(reason=str(error))
    logger.debug("Source root for package %s resolved to %s", package_name, root)
    return NautobotSourceBackend(root=root)


class SourceCatalog:
    """Discover the core source and eligible App sources once per catalog."""

    @cached_property
    def core(self) -> NautobotSourceBackend | UnavailableSourceBackend:
        """Return the read-only Nautobot source adapter."""
        return _build_backend(nautobot)

    @cached_property
    def apps(self) -> dict[str, AppSource]:
        """Return eligible registered App sources keyed by full package name."""
        return {
            config.name: AppSource(mount=f"{APPS_MOUNT}{config.label}/", backend=_build_backend(config.module))
            for config in enabled_app_configs()
        }

    def app_mounts(self, packages: tuple[str, ...]) -> dict[str, NautobotSourceBackend | UnavailableSourceBackend]:
        """Return mounts for the eligible packages selected by resolve_source_apps."""
        return {self.apps[name].mount: self.apps[name].backend for name in packages}


source_catalog = SourceCatalog()
