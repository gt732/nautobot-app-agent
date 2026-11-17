"""Tasks for use with Invoke.

Copyright (c) 2023, Network to Code, LLC
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import shlex
import subprocess
import sys
import time
from pathlib import Path

from invoke import task
from invoke.exceptions import Exit


def python_module(module, *args):
    """Build a command that executes a module with this Invoke interpreter."""
    return join_command([sys.executable, "-m", module, *map(str, args)])


def join_command(args):
    """Quote command arguments for the platform's default shell."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


@task(help={"check": "Check that poetry.lock is up to date instead of updating it."})
def lock(context, check=False):
    """Generate poetry.lock or check it for outdated dependencies."""
    context.run("poetry check --lock" if check else "poetry lock")


@task
def docs(context):
    """Build and serve documentation at http://127.0.0.1:8001."""
    context.run(python_module("mkdocs", "serve", "--livereload"))


@task
def build_and_check_docs(context):
    """Build strict, file-linked documentation for Nautobot's app static files."""
    context.run(python_module("mkdocs", "build", "--no-directory-urls", "--strict"))


@task(
    help={
        "target": "Module, file, or directory to inspect; repeatable (default: app package).",
        "recursive": "Set when target is a directory rather than a module or file name.",
        "verbose": "Show pylint's per-module progress output.",
    },
    iterable=["target"],
)
def pylint(context, target=None, recursive=False, verbose=False):
    """Run pylint code analysis and migration checks."""
    base = [
        sys.executable,
        "-m",
        "pylint",
        "--rcfile",
        "pyproject.toml",
    ]
    migrations_dir = Path(__file__).parent / "nautobot_agent" / "migrations"
    migrations_module = "nautobot_agent.migrations"
    migration_path = migrations_dir.resolve()

    def is_migration_target(item):
        if item == migrations_module or item.startswith(f"{migrations_module}."):
            return True
        try:
            Path(item).resolve().relative_to(migration_path)
        except ValueError:
            return False
        return True

    targets = target or ["nautobot_agent"]
    style_targets = [item for item in targets if not is_migration_target(item)]
    exit_code = 0
    if style_targets:
        command = [
            *base,
            "--init-hook",
            "import nautobot; nautobot.setup()",
            r"--ignore-paths=^.*[\\/]nautobot_agent[\\/]migrations[\\/][0-9]+_.*\.py$",
        ]
        if verbose:
            command.append("--verbose")
        if recursive:
            command.append("--recursive=y")
        command.extend(style_targets)
        if not context.run(join_command(command), warn=True):
            exit_code = 1

    # These AST-only migration checks need neither the app registry nor Nautobot startup.
    migration_command = [
        *base,
        "--load-plugins=pylint_django.checkers.migrations",
        "--disable=all",
        "--enable=fatal,new-db-field-with-default,missing-backwards-migration-callable",
        migrations_module,
    ]
    if not context.run(join_command(migration_command), warn=True):
        exit_code = 1
    if exit_code:
        raise Exit(code=exit_code)


@task(
    help={"target": "File or directory to type-check; repeatable (default: app package)."},
    iterable=["target"],
)
def ty(context, target: list[str] | None = None):
    """Run ty static type checking using this Invoke interpreter's environment."""
    command = python_module("ty", "check", "--python", sys.executable, *(target or ["nautobot_agent"]))
    context.run(command)


@task(
    help={
        "action": "Available values are ['lint', 'format']; repeatable (default: both).",
        "target": "File or directory to inspect; repeatable (default: all files).",
        "fix": "Automatically fix selected actions (default: False).",
        "diff": "Show diffs of changes (default: False).",
        "output_format": "Ruff output format (default: concise).",
    },
    iterable=["action", "target"],
)
def ruff(  # noqa: PLR0913  # each parameter is an Invoke CLI flag
    context, action=None, target=None, fix=False, diff=False, output_format="concise"
):
    """Run ruff formatting and lint checks."""
    action = action or ["lint", "format"]
    target = target or ["."]
    commands = []
    if "format" in action:
        mode = [] if fix else ["--check", "--diff"] if diff else ["--check"]
        commands.append(["format", *mode, *target])
    if "lint" in action:
        mode = ["--fix"] if fix else ["--diff"] if diff else []
        commands.append(["check", *mode, "--output-format", output_format, *target])
    # Run every selected action before failing so one report does not hide the other.
    failed = [args for args in commands if not context.run(python_module("ruff", *args), warn=True)]
    if failed:
        raise Exit(code=1)


@task(help={"target": "File or directory to inspect; repeatable (default: all files)."}, iterable=["target"])
def djlint(context, target=None):
    """Run djlint to lint Django templates."""
    result = context.run(python_module("djlint", "--lint", *(target or ["."])), warn=True, hide="both", pty=False)
    print(result.stdout, end="")
    if result.ok or "No files to check" in result.stdout:
        return
    print(result.stderr, end="")
    raise Exit(code=result.return_code or 1)


@task(help={"fix": "Apply available Markdown fixes before scanning."})
def markdownlint(context, fix=False):
    """Lint Markdown files."""
    if fix:
        context.run(python_module("pymarkdown", "fix") + " *.md")
    context.run(python_module("pymarkdown", "scan") + " *.md")


@task
def check_migrations(context):
    """Check for missing Django migrations."""
    context.run(python_module("nautobot.core.cli", "makemigrations", "--dry-run", "--check"))


def _validate_durations(durations):
    if durations is None:
        return None
    if isinstance(durations, str):
        try:
            durations = int(durations)
        except (TypeError, ValueError) as error:
            raise Exit("durations must be a nonnegative integer", code=2) from error
    if durations < 0:
        raise Exit("durations must be a nonnegative integer", code=2)
    return durations


@task(
    help={
        "label": "Directory or module to test instead of all Nautobot tests.",
        "failfast": "Stop after the first failing test.",
        "buffer": "Discard output from passing tests.",
        "pattern": "Run specific test methods, classes, or modules.",
        "verbose": "Enable verbose test output.",
        "coverage": "Enable coverage reporting (default: False).",
        "no_input": "Suppress interactive prompts.",
        "timing": "Show test database setup and total run times (default: False).",
        "durations": "Show the N slowest tests; 0 shows all durations (default: disabled).",
        "keepdb": "Reuse the test database (default: True); pass --no-keepdb after editing applied migrations.",
    }
)
def unittest(  # noqa: PLR0913  # each parameter is an Invoke CLI flag
    context,
    label="nautobot_agent",
    failfast=False,
    buffer=True,
    pattern="",
    verbose=False,
    coverage=False,
    no_input=False,
    timing=False,
    durations: int | None = None,
    keepdb=True,
):
    """Run Nautobot unit tests using the configured local development environment."""
    durations = _validate_durations(durations)
    command = [sys.executable]
    if coverage:
        command.extend(["-m", "coverage", "run", "--module", "nautobot.core.cli", "test", label])
    else:
        command.extend(["-m", "nautobot.core.cli", "test", label])
    if keepdb:
        command.append("--keepdb")
    if failfast:
        command.append("--failfast")
    if buffer:
        command.append("--buffer")
    if pattern:
        command.extend(["-k", pattern])
    if verbose:
        command.extend(["--verbosity", "2"])
    if no_input:
        command.append("--no-input")
    if timing:
        command.append("--timing")
    if durations is not None:
        command.extend(["--durations", str(durations)])
    context.run(join_command(command))


@task(help={"missing": "Show line numbers of statements not executed."})
def unittest_coverage(context, missing=False):
    """Report coverage from `invoke unittest --coverage`."""
    args = ["report", "--skip-covered"]
    if missing:
        args.append("--show-missing")
    context.run(python_module("coverage", *args))


@task
def coverage_lcov(context):
    """Generate an LCOV coverage report."""
    context.run(python_module("coverage", "lcov", "-o", "lcov.info"))


@task(
    help={
        "failfast": "Stop after the first failing test (default: False).",
        "no_input": "Suppress interactive prompts (default: False).",
        "lint_only": "Run static validation only; exclude the Node browser suite, Django tests, and coverage (default: False).",
        "timing": "Show each gate stage's elapsed time and Django test timing (default: False).",
        "durations": "Show the N slowest Django tests; 0 shows all durations (default: disabled).",
    }
)
def tests(  # noqa: PLR0913  # each parameter is an Invoke CLI flag
    context,
    failfast=False,
    no_input=False,
    lint_only=False,
    timing=False,
    durations: int | None = None,
):
    """Run static validation, then the Node browser and Django behavioral suites with coverage."""
    durations = _validate_durations(durations)
    started = time.perf_counter()

    def run_stage(name, function, *args, **kwargs):
        print(f"Running {name}...", flush=True)
        stage_started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            if timing:
                print(f"{name}: {time.perf_counter() - stage_started:.3f}s", flush=True)

    for name, check in (
        ("ruff", ruff),
        ("ty", ty),
        ("djlint", djlint),
        ("markdownlint", markdownlint),
        ("migrations check", check_migrations),
        ("pylint", pylint),
    ):
        run_stage(name, check, context)
    if not lint_only:
        run_stage(
            "browser tests",
            context.run,
            join_command(
                [
                    "node",
                    "--test",
                    *(str(path) for path in sorted(Path("tests").glob("*.test.cjs"))),
                ]
            ),
        )
        run_stage(
            "unit tests",
            unittest,
            context,
            failfast=failfast,
            no_input=no_input,
            coverage=True,
            timing=timing,
            durations=durations,
        )
        run_stage("coverage report", unittest_coverage, context)
        run_stage("coverage LCOV", coverage_lcov, context)
    if timing:
        print(f"Full gate: {time.perf_counter() - started:.3f}s", flush=True)
    print("All tests have passed!")
