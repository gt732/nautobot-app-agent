# Permissions and source access

Nautobot Agent uses authenticated Nautobot users, native object permissions, and Nautobot's object restrictions. The **Agent** page requires an active login but does not require `nautobot_agent.view_agentconversation`. Inventory queries still require the relevant Nautobot model `view` permission and remain subject to object restrictions. Access to the Agent page is not a grant to all inventory data.

Assign only the access needed for each administrator or user. The codenames below are Nautobot permissions for the app's native pages and actions.

## App pages and native object actions

| App area | Permission codenames |
| --- | --- |
| Conversation History | `nautobot_agent.view_agentconversation`, `nautobot_agent.change_agentconversation`, `nautobot_agent.delete_agentconversation` |
| All Conversations | `nautobot_agent.view_agentconversation` and `nautobot_agent.viewall_agentconversation`; bulk delete also requires `nautobot_agent.delete_agentconversation` |
| LLM Configurations | `nautobot_agent.add_llmconfiguration`, `nautobot_agent.view_llmconfiguration`, `nautobot_agent.change_llmconfiguration`, `nautobot_agent.delete_llmconfiguration` |
| Skills | `nautobot_agent.add_skill`, `nautobot_agent.view_skill`, `nautobot_agent.change_skill`, `nautobot_agent.delete_skill` |
| MCP Servers | `nautobot_agent.add_mcpserver`, `nautobot_agent.view_mcpserver`, `nautobot_agent.change_mcpserver`, `nautobot_agent.delete_mcpserver` |
| Trace list and summaries | `nautobot_agent.view_agentexecutiontrace` |
| Trace payload inspection | Both `nautobot_agent.view_agentexecutiontrace` and `nautobot_agent.view_agentexecutionspan` |

Saving LLM configurations requires superuser status, in addition to native add/change permissions. Model options can direct deployment-profile or worker-environment credentials to a provider.

Saving an MCP server requires superuser status if its existing or submitted configuration uses stdio or a credential profile. This includes switching transport, clearing a profile, and enabling or disabling such a server. Other users with native add/change rights may configure credential-free HTTP servers. Stdio programs run with the worker account's host access; credential profiles can expose deployment secrets to configured HTTP servers. Every user with Agent chat access can call every enabled MCP server with its configured credentials and worker identity; there are no per-user or per-group MCP access restrictions. Only configure servers and commands you trust; MCP tools can interact with external systems. See [Integrations](integrations.md#mcp-servers) for configuration and trust guidance.

`viewall_agentconversation` widens the All Conversations listing only when combined with `view_agentconversation`; Nautobot object restrictions still apply. It does not grant rename or delete rights, which require their own permissions and restrictions. An authorized user may read another owner's transcript but cannot Resume it; Resume is owner-only. Personal Conversation History remains limited to the user's own conversations. The Agent page and chat endpoints are separate from conversation-list permissions.

## Live Nautobot data

Data tools enforce the current user's Nautobot permissions and object restrictions. Permission to view a Device does not grant access to all its related records. Sensitive fields and Agent conversation/trace storage are not available through these tools.

Administrators can also set `blocked_models`, which applies even to superusers. For example, blocking `dcim.Location` stops Location lookups, but Device queries can still show Location names and IDs, filter or sort by Location, and count Devices per Location. See [Configuration](configuration.md#settings-reference) for setup. The separate [scope policy](configuration.md#scope-policy) controls which topics the Agent accepts, not data access.

## Installed source inspection

Source access is separate from inventory permissions. Users can inspect eligible installed Nautobot core source. Additional enabled-App source is granted through `source_apps_by_group`, a mapping of exact Nautobot group names to enabled App Python package names. Only enabled, registered Apps are eligible; unknown or disabled packages are skipped. Superusers can inspect all eligible enabled-App source.

For example, this grants members of an existing group named exactly `Agent Source Readers` access to the installed `nautobot_agent` package source:

```python
PLUGINS_CONFIG = {
    # Preserve other plugin entries and app settings.
    "nautobot_agent": {
        "source_apps_by_group": {"Agent Source Readers": ["nautobot_agent"]},
    },
}
```

Use your deployment's actual group and package names. This is read-only source access: it does not grant inventory permissions, access to project-static files or media, or permission to change installed files. See [Configuration](configuration.md#settings-reference) for the setting reference.

## Execution traces and spans

Trace access is a full-trust grant. `nautobot_agent.view_agentexecutiontrace` permits trace-list and summary access; viewing payloads additionally requires `nautobot_agent.view_agentexecutionspan`. Payloads may include unredacted prompts, tool inputs and results, and other sensitive execution data; there is no nested-payload redaction. Grant both permissions only to staff trusted with this data. Requests and data selected for model context may also be sent to the configured provider; see [Privacy and data handling](../user/privacy.md).

Trace access follows the conversation owner's object restrictions. Administrators may optionally configure a Nautobot object-permission constraint such as `{"conversation__user": "$user"}` on the trace view permission to limit rows to the requesting user. This is configured in Nautobot's permission administration. See [Operations](operations.md#trace-based-investigation) for trace retention and investigation.
