# Privacy and data handling

## What leaves Nautobot

Your messages and relevant conversation context are sent to the configured language-model provider. The Agent may also send Nautobot data returned by built-in tools and source text you are authorized to inspect. The provider and deployment determine where information is processed and retained. A self-hosted model keeps traffic within your network only if deployed accordingly; local hosting alone is not a privacy guarantee.

Built-in Nautobot data and installed-App source tools are read-only and enforce Nautobot permissions and source authorization. Optional MCP servers are a separate trust boundary: depending on configuration and worker identity, their tools may access external systems or cause side effects. Enable only trusted integrations; see [Administrator integrations](../admin/integrations.md) and [External interactions](external_interactions.md).

Blocking a type of record does not hide its name or ID everywhere: a Device may still show its assigned Location. Related records may also reveal information indirectly. Blocking does not redact saved conversations, traces, or previously copied data. Other tools, including source inspection and MCP integrations, have separate access controls.

## Saved conversations and refusals

Conversation history is saved in Nautobot's database and does not expire automatically. Disabling tracing does not disable history. Users with the applicable permission can delete eligible conversations; an active (queued or running) run temporarily prevents deletion. Deleting a conversation does not delete information already sent to a model provider or external MCP system.

A request refused by the scope policy remains in saved history but is excluded from later Agent model context, even when tracing is disabled. The classifier receives the request to decide whether it is in scope.

## Tracing

Tracing is enabled by default and can be disabled with `tracing: False`; this does not disable conversation history. Traces and spans may contain unredacted prompts, tool inputs and results, metadata, event details, exception text, and other sensitive execution data. They are not automatically redacted or expired. Users with trace and span view permissions have full-trust access to retained data, including complete JSON for supported fields; grant these permissions only to trusted users. Retention can use worker memory and database storage, and infrastructure limits may affect saving or retrieving traces. See [trace-based operations](../admin/operations.md#trace-based-investigation).

## Verify model assistance

Model responses are assistance, not a guaranteed complete or correct inventory, nor authorization to make changes. Verify important claims against Nautobot and follow your organization's review procedures. Built-in operations are read-only, but optional MCP capabilities may have external effects.
