# Compatibility

The package metadata declares these version constraints:

| Component | Required version |
| --- | --- |
| Nautobot | `>=3.0.0,<4.0.0` |
| Python | `>=3.12,<3.15` (3.12, 3.13, or 3.14) |

These are package constraints, not a claim that every combination has been tested. Select a Nautobot release that supports the Python version used by the deployment. For example, Nautobot 3.0.0 requires Python `>=3.10,<3.14`, so it overlaps this package's Python range on 3.12 and 3.13, but not 3.14. See [Nautobot 3.0.0 package metadata](https://pypi.org/project/nautobot/3.0.0/).

Nautobot's configured Redis cache and Celery worker infrastructure must be available. Workers must consume the chat queue configured for the app; see [Installation](install.md) and [Configuration](configuration.md).

The package includes native integrations for OpenAI, Anthropic, Google GenAI, Ollama, OpenRouter, and DeepSeek. Their presence does not guarantee compatibility with a particular provider model or deployment. See [LLM configurations and model roles](configuration.md#llm-configurations-and-model-roles).
