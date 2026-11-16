# Building documentation

The site uses MkDocs, Material, and mkdocstrings. `README.md` is the concise public overview; detailed user and administrator guidance belongs in `docs/user/` and `docs/admin/`.

## Install documentation dependencies

From the repository root, using your existing development environment:

```shell
poetry install --only docs --no-root
```

This installs only the optional documentation dependency group. Building the site does not require Nautobot, Django setup, Redis, a database, provider credentials, or a running Nautobot server. Source references use static analysis rather than importing the app.

The Invoke commands additionally require Invoke from the development environment. Without Invoke, use the equivalent MkDocs commands below.

## Preview

```shell
invoke docs
```

Open <http://127.0.0.1:8001>. Stop the documentation server with Ctrl+C. This is a documentation-only server, not a Nautobot server.

Equivalent command:

```shell
python -m mkdocs serve --livereload
```

## Build and validate

```shell
invoke build-and-check-docs
```

Equivalent command:

```shell
python -m mkdocs build --no-directory-urls --strict
```

Warnings fail the build. Generated HTML goes into `nautobot_agent/static/nautobot_agent/docs/`, which is ignored by Git. File-style links allow the documentation to be served through Nautobot's static-file handling.

The packaging configuration includes this generated directory in wheels and source distributions when it exists. Build the documentation before packaging when the distribution should include in-app documentation; package builds do not generate it automatically. Do not commit generated HTML.

## GitHub Pages

The public site is <https://gt732.github.io/nautobot-app-agent/>.
The `Documentation` workflow in `.github/workflows/docs.yml` builds and
deploys it on pushes to `main`, or through **Actions → Documentation → Run workflow**.
Repository **Settings → Pages → Source** must be **GitHub Actions**.

The workflow installs only the locked documentation dependency group and
statically renders code references; it does not start Nautobot or use provider
credentials. Its strict build uses directory-style URLs and writes to `site/`:

```shell
python -m mkdocs build --strict --site-dir site
```

The hosted build is separate from the file-linked in-app build above.
Both output directories are ignored by Git. Source-edit links continue to
target `docs/` on `main`.

The full Docker CI workflow skips pushes and pull requests that change only
`docs/`, `README.md`, `mkdocs.yml`, `.readthedocs.yaml`, or the documentation
deployment workflow. Mixed code/documentation changes still run full CI, as do
changes to dependencies, CI itself, and runtime skill Markdown. Manual CI runs
remain available through **Actions → CI → Run workflow**. Documentation
deployment still runs on pushes to `main`.

If branch protection requires the Docker CI check, GitHub path filtering can
leave a docs-only pull request waiting for that skipped check. Account for this
when configuring required checks.

## Nautobot branding

The site uses the official Nautobot app cookiecutter's logo, favicon, stylesheet,
and adapted footer, with this app's banner and guide content. Theme settings live
in `mkdocs.yml`; the assets and footer override live under `docs/assets/`.
The upstream assets retain their Apache-2.0 license in `docs/assets/LICENSE`;
the app itself remains MIT-licensed. Community links do not imply sponsorship.

## Edit the site

- Keep `README.md` concise: summarize capabilities and trust boundaries, then link to guide pages rather than reproducing detailed setup or behavior references.
- Keep detailed user workflows and data handling under `docs/user/`, and administrator installation, configuration, permissions, integrations, operations/recovery, compatibility, upgrade, and removal guidance under `docs/admin/`.
- Add supplementary Markdown under `docs/` and register completed pages in `mkdocs.yml` navigation.
- Use admonitions, fenced code, and images where they help readers.
- Keep code-reference directives pointed at existing Python modules; mkdocstrings renders their docstrings from source.

## Screenshots

Before adding or replacing a screenshot, check it for credentials, private inventory, usernames, endpoints, and other sensitive details. Trace screenshots need particular care because trace and span data is unredacted. Prefer one readable image per topic. Use paths relative to the Markdown file in `docs/`; README image paths are relative to the repository root.
