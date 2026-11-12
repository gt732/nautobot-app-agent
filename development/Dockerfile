# syntax=docker/dockerfile:1
# Keep the base image aligned with Nautobot in poetry.lock.
FROM ghcr.io/nautobot/nautobot-dev:3.2.6-py3.12

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y nodejs chromium \
    && rm -rf /var/lib/apt/lists/*

ENV POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    CHROME_BIN=/usr/bin/chromium

WORKDIR /source
COPY pyproject.toml poetry.lock README.md LICENSE ./
RUN poetry install --no-root --extras dev --with docs --no-interaction

COPY nautobot_agent ./nautobot_agent
COPY tests ./tests
COPY docs ./docs
COPY tasks.py mkdocs.yml ./
RUN poetry install --only-root --no-interaction \
    && python -c "import importlib.metadata as m; assert m.version('nautobot') == '3.2.6'"

COPY development/nautobot_config.example.py /opt/nautobot/nautobot_config.py
