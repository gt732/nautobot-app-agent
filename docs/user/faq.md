# Frequently asked questions

## Why is the message composer unavailable?

A default Agent language model has not been configured. Ask your administrator to complete [configuration](../admin/configuration.md), then reload **Nautobot Agent → Agent**. See [Getting started](getting_started.md).

## Why is my request still queued?

**Queued; waiting for a worker.** means execution has not started. If the queued warning remains, contact your administrator; do not interpret it as a running or failed request. See [Operations](../admin/operations.md).

## Why are records or installed-App source missing?

Results are limited to data and fields your Nautobot permissions allow, and queries may be bounded or paginated. Empty results do not prove no records exist. Source access also depends on the administrator's group-to-App configuration. Ask your administrator to check permissions or source eligibility, or narrow the question. See [External interactions](external_interactions.md#nautobot-data-and-installed-app-source) and [Permissions and source access](../admin/permissions.md).

## Why was my question refused, or could not be classified?

A scope refusal means the request was not classified as Nautobot-related under the configured policy. Try rephrasing it around Nautobot, or ask your administrator about the policy. A classifier failure means the Agent could not classify the request; if it persists, ask your administrator to check the model configuration. Scope-refused requests remain in saved history but are excluded from later model context. See [Scope policy](../admin/configuration.md#scope-policy) and [Privacy and data handling](privacy.md).

A provider refusal is separate from scope policy. The `model_refused` error means the provider declined the request, so the run fails without an answer. Rephrase the request or start **New chat**. If refusals persist, ask your administrator to review the model's policies.

## The request fails because of a provider or context limit

Read the visible safe error code and guidance. Provider credentials, access, model availability, rate limits, connectivity, and context limits require administrator review; do not paste secrets into chat. For `model_context_exceeded`, start **New chat** and restate the relevant context more briefly. Context-use guidance is not an exact token budget. See [Configuration](../admin/configuration.md).

## Why can't I see Resume, rename, delete, or another user's history?

**Resume** is owner-only. Viewing other users' conversations requires the separate `viewall_agentconversation` permission; that does not itself allow rename or deletion. Rename and delete follow Nautobot object permissions and constraints. See [Conversations](conversations.md) and [Permissions and source access](../admin/permissions.md#app-pages-and-native-object-actions).

## Why can't I see traces?

Trace access requires the applicable Nautobot trace/span permissions. Recorded traces may contain unredacted sensitive execution data, so access is restricted. See [Privacy and data handling](privacy.md) and [Permissions and source access](../admin/permissions.md#execution-traces-and-spans).

## Why did my chart fail or omit data?

Charts support bars, lines, and pies with limits on series, categories, slices, and payload size. Results can be partial or permission-limited; narrow or aggregate the request rather than assuming omitted values are zero. See [Charts and diagrams](charts_and_diagrams.md#numeric-charts).

## Why can't I delete a conversation?

An active (queued or running) run reserves the conversation, preventing deletion. Do not try to bypass the reservation; stop the run or wait for it to finish. Finished runs, including interrupted runs and runs showing warnings, do not block deletion. In **All Conversations**, busy or otherwise ineligible rows may be skipped during bulk deletion. See [Conversations](conversations.md#all-conversations) and [Operations](../admin/operations.md).

## My submission or status update is uncertain. Should I send it again?

No—not solely because a response or status update is missing. The request might already have been accepted. Check the existing conversation before retrying; if an external operation may have occurred, verify its outcome first.
