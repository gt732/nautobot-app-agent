# Installation

## Prerequisites

Install Nautobot Agent in the same Python environment or deployment image used by Nautobot's web and worker processes. Its package name is `nautobot-agent`; its plugin name is `nautobot_agent`. Nautobot must have its database, Redis cache, and Celery workers configured. A worker must consume the chat queue selected in [Configuration](configuration.md).

The package requires Python `>=3.12,<3.15` and Nautobot `>=3.0.0,<4.0.0`; see [Compatibility](compatibility.md). Worker processes also need permission to launch child processes for the bundled Python interpreter. The installed package supplies its executable dependency.

Follow the deployment's Nautobot installation procedure; see the official [Nautobot installation and deployment options](https://docs.nautobot.com/projects/core/en/stable/user-guide/administration/installation/) and [Nautobot App guidance](https://docs.nautobot.com/projects/core/en/stable/development/apps/). Add the approved app artifact to persistent package-management or image-build inputs so it is available after deployment rebuilds.

Install the published package from [PyPI](https://pypi.org/project/nautobot-agent/)
into the active Nautobot Python environment:

```shell
python -m pip install nautobot-agent
```

## Install and enable

Add the app to the existing Nautobot plugin list. Preserve any other plugins:

```python
PLUGINS = [
    # Keep the deployment's existing plugins.
    "nautobot_agent",
]
```

Add or merge the app's entry in the existing `PLUGINS_CONFIG` mapping as needed. Keep secrets in deployment secret management. For example, define a provider profile and select it in an LLM configuration:

```python
import os

PLUGINS_CONFIG = {
    # Keep existing plugin configuration entries.
    "nautobot_agent": {
        "credentials": {
            "openai": {"api_key": os.environ["OPENAI_API_KEY"]},
        },
    },
}
```

Apply app migrations with Nautobot's post-upgrade command, then reload or restart web and worker processes using the deployment's normal procedure:

```shell
nautobot-server post_upgrade
```

Confirm a Celery worker consumes the configured chat queue; otherwise submitted runs remain queued. A dedicated queue and worker can keep chat work separate from other Nautobot background tasks. See [Operations](operations.md).

## Next steps

1. Create an LLM configuration in **Nautobot Agent → LLM Configurations**, select its credential profile, and assign it as the default Agent model. See [Configuration](configuration.md).
2. Grant intended users the appropriate permissions for chat, inventory queries, and administration; see [Permissions and source access](permissions.md).
3. Reload the Agent page. The composer is enabled after a default Agent model is configured.
4. Follow [Getting started](../user/getting_started.md) to start a chat.
