# External interactions

Nautobot Agent uses a configured language model and may also use Nautobot data, authorized installed-App source, or administrator-enabled MCP integrations. Each has different access and trust boundaries.

## Language-model providers

The app supports OpenAI, Anthropic, Google Gemini Developer API, Ollama, OpenRouter, and DeepSeek. Your administrator chooses the provider and model and manages its credentials. Your messages, relevant conversation context, and data returned by authorized tools may be sent to that provider. Processing and retention depend on the provider and deployment. A locally deployed Ollama model does not by itself guarantee that data stays private. See [Privacy and data handling](privacy.md) and the administrator [Configuration guide](../admin/configuration.md).

## Nautobot data and installed-App source

Built-in data tools query Nautobot under your identity and permissions. They are read-only and exclude sensitive or internal fields; not every model or field is available. Empty or partial results may reflect permissions or query limits, not just missing records.

The Agent may inspect read-only source text from Nautobot and eligible installed Apps. Installed-App access depends on administrator configuration and your group membership; core source and superuser access have separate eligibility rules. Source searches do not cover static or media files and may omit unreadable files or unavailable Apps. No matches do not prove that code is absent. Ask your administrator if expected source is unavailable.

## MCP integrations

An administrator may enable Model Context Protocol (MCP) servers. These are optional integrations, not built-in read-only inventory tools: their capabilities depend on the server, its configuration, and the worker identity, and they may contact external services or cause side effects. Use only integrations your organization trusts. If a response is lost or a run stops, an external operation may still have occurred and may not have been rolled back. See [Administrator integrations](../admin/integrations.md).

If an integration is unavailable, its tools cannot be used for that run; a requested external action or lookup may remain incomplete. Built-in and other available tools may still work. Check the run status and ask your administrator if needed.
