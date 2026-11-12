# Docker development

The shared development stack installs Nautobot Agent in editable mode alongside
Nautobot **3.2.6**, Python **3.12**, PostgreSQL **17**, Redis **7**, and Celery
worker and scheduler services. Python dependencies come from `poetry.lock`.

## Requirements

- Git.
- Docker Engine with Docker Compose v2 or newer, or Docker Desktop.
- Linux containers. On Windows 11, use Docker Desktop's WSL 2 backend.

The commands below work from PowerShell, macOS terminals, and Linux shells.
No host Python, Poetry, Node.js, database, or Redis installation is required.
On Windows, a checkout inside the WSL filesystem generally gives faster bind
mounts than a Windows drive. Allow Docker Desktop access to the checkout if
prompted.

## Start

Clone the repository and enter its root directory. Copy
`development/nautobot_config.example.py` to `development/nautobot_config.py`
once:

=== "PowerShell"

    ```powershell
    Copy-Item development/nautobot_config.example.py development/nautobot_config.py
    ```

=== "macOS / Linux"

    ```shell
    cp development/nautobot_config.example.py development/nautobot_config.py
    ```

The local copy is ignored by Git. Edit it to try deployment settings without
changing the shared example. Keep provider secrets in the ignored root `.env`.
Do not overwrite an existing local configuration when updating the checkout;
consult the example for new settings. Omitted plugin settings use app defaults.

Then run:

```shell
docker compose up
```

Compose builds the development image on first use. Initial dependency downloads
and database migrations can take several minutes. The web container runs
Nautobot's initialization, including migrations and static-file collection;
workers start after Nautobot passes its health check.

Compose mounts the local configuration read-only into web, worker, and beat
containers and refuses to create a missing source path. If startup reports a
missing `development/nautobot_config.py`, copy the example first. Image builds
use only the tracked example; the local configuration is not a build input.

Open <http://localhost:8080> and sign in with username **admin**, password
**admin**. To run in the background instead, use `docker compose up -d`.

!!! warning "Local development only"
This stack uses public, disposable database credentials, a fixed secret key,
and an administrator API token of
`0123456789abcdef0123456789abcdef01234567`. Initialization resets the admin
password to `admin` on web-container startup. Debug mode is enabled and the
development server is bound to host loopback only. Do not deploy this
configuration, expose it through a public proxy, or load production data.
PostgreSQL and Redis have no published host ports.

The app is enabled automatically. To configure provider credentials, see
[Provider credentials](#provider-credentials), then follow
[Getting started](../user/getting_started.md) to configure an LLM.
Provider traffic leaves the containers; review
[Privacy and data handling](../user/privacy.md) before using real data.
For host-based services such as Ollama, `localhost` inside a container is the
container itself. Docker Desktop provides `host.docker.internal`; on Linux,
configure an appropriate reachable host address.

If port 8080 is occupied, create a root `.env` containing `NAUTOBOT_PORT=8081`
and open port 8081 instead. This file is ignored by Git; do not put private
credentials in tracked development files.

## Provider credentials

Put your provider keys in the ignored `.env` at the repository root. Keep only
the providers you use; no keys are required to start the stack.

```dotenv
OPENAI_API_KEY=replace-with-your-key
ANTHROPIC_API_KEY=replace-with-your-key
GOOGLE_API_KEY=replace-with-your-key
OPENROUTER_API_KEY=replace-with-your-key
DEEPSEEK_API_KEY=replace-with-your-key
# Optional, for Ollama running on a Docker Desktop host:
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Compose automatically reads this file before creating containers. Exported shell
variables take precedence over `.env` values. Only the listed provider variables
are forwarded to the app services; the file is not mounted or included in the
image. Do not commit it or put keys in `development/development.env`.

The development configuration creates these profiles for nonempty variables:

| Provider                       | Environment variable | Credential profile |
| ------------------------------ | -------------------- | ------------------ |
| OpenAI                         | `OPENAI_API_KEY`     | `openai`           |
| Anthropic                      | `ANTHROPIC_API_KEY`  | `anthropic`        |
| Google Gemini (`google_genai`) | `GOOGLE_API_KEY`     | `google`           |
| OpenRouter                     | `OPENROUTER_API_KEY` | `openrouter`       |
| DeepSeek                       | `DEEPSEEK_API_KEY`   | `deepseek`         |
| Ollama                         | `OLLAMA_BASE_URL`    | `ollama`           |

Select the corresponding credential profile when creating an LLM configuration.
Ollama does not require a key; without `OLLAMA_BASE_URL`, leave its credential
profile blank and supply a reachable `base_url` in the LLM's Parameters.
This does not create model configurations or choose a default Agent model.

After changing `.env`, run `docker compose up -d` to recreate containers with
the updated values. `docker compose restart` does not reload their environment.
No image rebuild is required.

!!! warning "Environment values are sensitive"
Docker administrators can inspect container environment variables.
`docker compose config` also expands the keys; do not share its output.
Use `docker compose config --quiet` to validate without printing them.

## Edit and run checks

The app, browser tests, documentation, and shared tooling files are mounted
individually. The entire checkout is never mounted. The image build also uses
an explicit allowlist of project inputs rather than sending the whole checkout.

Python web changes reload automatically; refresh the browser for frontend
changes. Worker and beat use `watchmedo` to restart automatically when Python
files change in the app or Nautobot configuration directories. The watchers use
polling to support Docker Desktop bind mounts on Windows and macOS as well as
Linux. Editing code can interrupt active development chat runs.

Web and worker logging defaults to `DEBUG`; beat defaults to `INFO`. Override
`NAUTOBOT_LOG_LEVEL` or `NAUTOBOT_BEAT_LOG_LEVEL` in the root `.env` and run
`docker compose up -d` to apply different levels.

Edit files with your usual local editor. Run checks from your host terminal;
you do not need to open a shell or editor inside the container:

```shell
docker compose exec -T nautobot invoke unittest --no-input
docker compose exec -T nautobot invoke unittest --no-input --label nautobot_agent.tests.test_run_smoke
docker compose exec -T nautobot sh -c 'node --test tests/*.test.cjs'
docker compose exec -T nautobot invoke tests --no-input
docker compose exec -T nautobot invoke build-and-check-docs
```

Each command starts a separate process and returns its output and exit status
to your terminal; it does not restart the web server. Local linting and type
checking remain available through a host Python environment with the project
development dependencies installed. The full `invoke tests` gate also runs
static checks inside Docker.

The image includes Node.js and Chromium for the browser suite, as well as Python
development and documentation dependencies. Test databases live in the
development PostgreSQL service, separately from the app database, and are reused
between runs. Do not run concurrent Django test suites against the same test
database. Keep the PostgreSQL volume to avoid repeating initial migrations.

## Continuous integration

GitHub CI reuses this Compose stack and its locked dependencies. One job builds
the development image after copying the example configuration, starts
PostgreSQL and Redis, then runs `invoke lock
--check`, `invoke tests --no-input`, and `invoke build-and-check-docs` inside a
one-off app container. It does not start the web server, worker, or beat and
does not need provider keys.

The existing `invoke tests` task owns the check sequence: Ruff formatting and
linting, ty, djlint, Markdown linting, migration checks, Pylint, browser tests,
Django tests, and coverage reports. CI does not maintain a second list of lint
commands or regenerate the committed lockfile.

The workflow runs for pull requests, pushes to `main` and `develop`, and manual
dispatches. It tests the pinned Docker baseline,
not every Python/Nautobot combination allowed by the package metadata. It does
not build distribution packages or publish releases.

## Dependency and schema changes

After changing dependencies, update `poetry.lock` using Poetry and rebuild:

```shell
docker compose up --build
```

The mounted dependency files are read-only inside containers. Keep the Nautobot
version in `poetry.lock`, `development/Dockerfile`, and the Compose image tag
aligned when changing the supported development baseline.

After adding migrations, stop the stack and start it again so initialization
applies them before the workers start:

```shell
docker compose down
docker compose up
```

## Logs and shutdown

```shell
docker compose ps
docker compose logs -f nautobot worker
docker compose down
```
