# Operations

Chat requests run in Celery workers. Nautobot's database and Redis must be available; the database stores run status and Redis provides live updates. A quiet or disconnected update stream does not mean work stopped. Keep the app version and configuration consistent across web and worker processes.

## Worker and capacity planning

- A dedicated chat worker/queue is recommended. Set `chat_celery_queue` and configure a worker to consume it; when unset, Nautobot's default queue is used. Every worker consuming the chat queue should use `--prefetch-multiplier 1 -O fair` to reduce reserved backlog.
- At least one worker must consume the queue. If a run is not claimed before `chat_queue_wait_timeout`, it ends as **Dispatch failed** (`queue_wait_expired`) and will not run later; submit it again when workers are available.
- A conversation runs one request at a time. Each user may have at most two queued or running requests across conversations. If a new request is rejected at this limit, wait for a request to finish or cancel one before retrying. This limit does not cap provider usage or cost.
- Configure timeouts using the [Configuration](configuration.md#settings-reference) reference. A hard timeout is a last-resort limit, not proof that external work stopped.

## Run statuses

| Status | Meaning and action |
| --- | --- |
| Queued | Waiting for a worker. If it persists, check that a worker consumes the configured queue. |
| Running | Work is underway. A quiet event stream does not prove it stopped; reload the page to refresh status. |
| Completed / Refused | Finished, or declined as out of scope. |
| Failed | An error or time limit occurred. Use the error code and worker logs to investigate. |
| Cancelled | The stop was confirmed. |
| Dispatch failed | The run never started (queue deadline or broker publication failure). Resubmit when workers are available. |
| Interrupted | The app lost control of the run. Check external systems before retrying. |

Cancellation of running work is cooperative; while stopping, the run remains Running with a cancellation notice. A Celery `SUCCESS` means only that the task returned; use the run status to determine the outcome.

## Interrupted runs and diagnostic warnings

An interrupted run is terminal and permits a new message, but the app does not replay work, retry tools, or undo external effects.

Interrupted runs may show:

- **External outcome unknown**: a tool was called but its result was not confirmed.
- **Cleanup incomplete**: connections could not be confirmed closed before the run ended. This alone does not mean an external effect occurred.

Warnings persist when the conversation is reopened and do not block deletion or new messages. No warning is not proof that nothing happened. Reconcile affected systems before retrying uncertain work. An unavailable-integration notice means a configured MCP server could not be reached when the request began; other tools may remain available.

Delete conversations through Conversation History. Nautobot's generic **Bulk Delete Objects** Job is not supported for conversations; it bypasses the checks that protect active runs. Active runs block deletion; conversations with expired queued or stale running runs can be deleted.

## Limits and troubleshooting

`chat_max_model_calls` and `chat_max_tool_calls` limit calls across a run, including delegates. See [Per-run call ceilings](configuration.md#per-run-call-ceilings). If a limit is reached, simplify the request or adjust the configured ceiling if appropriate. These are not provider quota errors and do not directly limit tokens or cost.

Model error codes are safe categories; check them alongside web and worker logs:

| Code | Check |
| --- | --- |
| `model_not_found` | Model identifier and account access. |
| `model_authentication_failed` | Credential profile and secret loading. |
| `model_permission_denied` | Provider account authorization. |
| `model_rate_limited` | Provider quota and usage limits. |
| `model_timeout`, `model_connection_failed`, `model_provider_unavailable` | Provider status and worker network/DNS/TLS connectivity. |
| `model_context_exceeded` | Start a new chat or shorten the request. |
| `model_request_invalid` | Model configuration and supported features, such as tool calling. |
| `model_context_limit_missing` | Set a positive `profile.max_input_tokens` on the Agent model. |
| `execution_unavailable` | App configuration and logs; runtime could not be built. |
| `run_model_call_limit`, `run_tool_call_limit` | The run reached its configured call ceiling. |

For query errors, correct invalid query values and retry. If a problem persists, use the displayed error category and logs rather than exposing raw database or provider errors to users.

## Trace-based investigation

Traces can contain unredacted prompts, tool inputs, and results. Grant trace access only to trusted investigators; see [Permissions](permissions.md#execution-traces-and-spans). Model requests may also be sent to the configured provider; see [Privacy and data handling](../user/privacy.md).

A run can succeed even if its trace is incomplete. Capture state is **Pending**, **Complete**, **Partial**, or **Failed**; use run status and logs when details are missing. Previews are limited to 1,024 characters, while JSON links expose full saved payloads to users with full-inspection permission. Traces are not truncated and have no automatic expiry. Deleting a conversation deletes its saved traces and spans too; apply your retention policy to conversation deletion.
