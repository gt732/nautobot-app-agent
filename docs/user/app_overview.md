# Nautobot Agent overview

Nautobot Agent brings conversational assistance to Nautobot. Ask questions about Nautobot and its installed Apps, request explanations or code examples, or—when enabled—use permission-aware inventory tools, charts, diagrams, and administrator-configured MCP integrations. By default, requests are expected to be Nautobot-related; administrators can configure the scope policy to allow broader requests.

## For users

Open **Nautobot Agent → Agent** to start or continue a conversation. Built-in inventory access is read-only and limited by your Nautobot permissions. The app supports several model providers; see [administrator configuration](../admin/configuration.md). Charts and diagrams are available in supported forms; see [Charts and diagrams](charts_and_diagrams.md). See [Getting started](getting_started.md), [Conversations](conversations.md), [Privacy and data handling](privacy.md), and [External interactions](external_interactions.md).

The Agent can explain authorized source code, review code you share, and draft examples. Built-in tools do not edit files; you must apply suggested changes yourself. Administrator-enabled MCP tools may provide additional file operations, subject to their configured capabilities.

## For administrators

Administrators install the app, configure a default model and optional integrations, and manage permissions. See [Installation](../admin/install.md), [Configuration](../admin/configuration.md), and [Integrations](../admin/integrations.md).

## Boundaries and trust

Built-in data and source tools are permission-filtered and read-only; they do not expose every Nautobot field or App. Requests and relevant data are sent to the configured model provider. Traces are enabled by default and may contain unredacted data. MCP tools may have broader access or cause external side effects. Verify model output; it does not authorize changes. See [Privacy and data handling](privacy.md) and [External interactions](external_interactions.md).
