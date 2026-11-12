# Configuration

Configure deployment options under `PLUGINS_CONFIG["nautobot_agent"]` in Nautobot's configuration. Apply changes to web and worker processes and reload them using the deployment's normal procedure. Keep secrets in deployment-managed profiles, not in database-managed model parameters or MCP launch JSON. Unknown setting names or invalid values prevent app startup.

## Settings reference

| Setting | Default | Purpose |
| --- | --- | --- |
| `blocked_models` | `[]` | Models the Agent must not look up, such as `dcim.Location`. Use exact installed model names; matching is case-insensitive and wildcards are not supported. Invalid entries prevent startup. |
| `builtin_skills` | `True` | Includes the packaged `nautobot-charts` and `nautobot-mermaid` skills. Set to `False` to exclude both from Agent execution; enabled custom skills remain available. Must be a boolean. |
| `credentials` | `{}` | Named provider credential profiles. Each profile maps provider constructor-argument names to values; select a profile on an LLM configuration. Keep secrets in deployment secret management. |
| `mcp_credentials` | `{}` | Named HTTP MCP profiles mapping valid HTTP header names to single-line string values. |
| `mcp_environments` | `{}` | Named stdio MCP profiles mapping environment-variable names to string values. Every value in a selected profile must be set, even when the server is disabled. |
| `source_apps_by_group` | `{}` | Maps exact Nautobot group names to lists of enabled App Python package names whose source members may be inspected by those group members. Package names are dotted Python identifiers. See [Permissions and source access](permissions.md). |
| `scope_mode` | `"conversation"` | Controls which requests the Agent accepts; see [Scope policy](#scope-policy). This is not a data-access permission. |
| `tracing` | `True` | Enables execution trace recording. Traces can contain unredacted prompts, tool inputs/results, and sensitive data. See [Privacy and data handling](../user/privacy.md). |
| `interpreter` | `{"max_duration_secs": 5.0, "max_memory_bytes": 67108864, "max_host_calls": 20, "max_output_chars": 16000}` | Per-execution Python limits: interpreter execution duration, memory, calls to host tools, and returned output size. Partial overrides merge with defaults. Values must be positive; duration must be finite, and `max_output_chars` must be at least 1000. Duration excludes time waiting for host tools. |
| `query` | `{"max_page_bytes": 1048576}` | Maximum UTF-8 JSON response size for `query_records` and `trace_relationships`. Partial override supported; minimum 1024 bytes. |
| `query_statement_timeout_ms` | `30000` | Database-side time limit for each SQL statement used by `query_records` and `trace_relationships`; positive integer up to 2147483647. This is not a total tool-call deadline. |
| `chat_return_timeout` | `600` seconds | Delay after a normal departure before a plain Agent-page request starts a new chat instead of keeping the selected conversation. Must be a positive integer. Saved history is not deleted. |
| `chat_celery_queue` | `None` | Optional queue for chat tasks. `None` uses Nautobot's default queue. A worker must consume the selected queue. |
| `chat_max_model_calls` | `40` | Maximum logical model calls per run, including scope classification, delegates, and summarization. Positive integer. |
| `chat_max_tool_calls` | `100` | Maximum graph tool invocations per run, including delegation and interpreter invocations. Positive integer. |
| `chat_soft_time_limit` | `300` seconds | Cooperative task time limit. Positive integer. |
| `chat_hard_time_limit` | `600` seconds | Celery hard task time limit. Positive integer and must exceed the soft limit plus cleanup allowance and shutdown margin. |
| `chat_cleanup_timeout` | `20` seconds | Cleanup allowance after cooperative shutdown begins. Positive integer; soft limit + cleanup allowance + fixed 10-second margin must be less than the hard limit. |
| `chat_queue_wait_timeout` | `600` seconds | Maximum time an admitted run waits for a worker to claim it. Positive integer; changing it affects new submissions. Expired queued runs do not start. See [Operations](operations.md#worker-and-capacity-planning). |

All four chat timing settings must be no greater than 2147483647 seconds. These limits do not guarantee that in-flight provider or external work has stopped. Call ceilings count model and tool calls, not tokens, cost, provider-wide quotas, or remote retries. Failed calls still count; calls already in progress may finish. They do not change provider timeout or retry settings.

Example partial overrides:

```python
PLUGINS_CONFIG = {
    # Retain other plugin and app settings.
    "nautobot_agent": {
        "blocked_models": ["dcim.Location"],
        "builtin_skills": False,
        "chat_celery_queue": "agent",
        "interpreter": {"max_host_calls": 10},
        "query": {"max_page_bytes": 2_000_000},
    },
}
```

`blocked_models` applies to everyone, including superusers. It prevents lookups of the listed records and their details, but related names and IDs can still appear in other results. Reload web and worker processes after changes. See [Permissions](permissions.md#live-nautobot-data) for an example and [Privacy](../user/privacy.md) for the limits.

### Per-run call ceilings

Each run, including follow-up turns, may make the configured number of calls; reaching a limit fails the run and prevents further calls. Model calls include scope classification, Agent and delegate calls, and summarization. Tool calls include built-in, installed-source, MCP, delegation, and interpreter invocations; interpreter host queries have their separate `interpreter.max_host_calls` limit. The limits do not cancel work already in progress or undo external effects.

### Trace capture and inspection

`tracing` is the only trace setting. Traces may retain full supported callback data; previews in the interface do not redact or alter stored data. Users granted full trace inspection can open or download retained fields. Review [Privacy and data handling](../user/privacy.md) before enabling trace access, and see [Operations](operations.md#trace-based-investigation) for trace administration.

## LLM configurations and model roles

Only superusers can save LLM configurations. In **Nautobot Agent → LLM Configurations**, enter a model as `provider:model`, select a **Credential profile**, and use **Parameters** for non-secret provider constructor options as a JSON object. Profile values override matching parameter values. Reserved model-initialization keys cannot be set in either place.

Bundled native provider examples:

| Provider ID | Example model | Profile entry |
| --- | --- | --- |
| `openai` | `openai:gpt-5.4` | `credentials["openai"]["api_key"]` |
| `anthropic` | `anthropic:claude-sonnet-4-5` | `credentials["anthropic"]["api_key"]` |
| `google_genai` | `google_genai:gemini-2.5-flash` | `credentials["google"]["api_key"]`; Gemini Developer API may require `{"vertexai": false}` in Parameters. |
| `ollama` | `ollama:qwen3:8b` | Often no key for a local service; set a reachable `base_url` in Parameters, for example `{"base_url": "http://ollama:11434"}`. |
| `openrouter` | `openrouter:anthropic/claude-sonnet-4` | `credentials["openrouter"]["api_key"]` |
| `deepseek` | `deepseek:deepseek-flash` | `credentials["deepseek"]["api_key"]` |

Examples do not guarantee current model availability or compatibility. Agent models need tool-calling support. Classifier models need structured-output support (DeepSeek requires JSON mode). The Agent model must expose a positive integer `profile.max_input_tokens` through provider metadata or an explicit profile parameter, for example `{"profile": {"max_input_tokens": 32768}}`; otherwise the run fails before Agent tools run. With Ollama, `num_ctx` configures model context while `profile.max_input_tokens` provides the app's context metadata and should match the deployment. A classifier-only model does not require this context setting. Other provider integrations may require separate installation and are not covered by the bundled integrations.

Assign exactly one configuration the default Agent role. A scope-classifier configuration is optional; if none is assigned, the Agent configuration is used for classification. One configuration may have both roles. Chat is unavailable until a default Agent model is selected. To change a role, assign it to the replacement configuration and save.

## Scope policy

The scope gate controls whether the general-purpose Agent may answer requests unrelated to Nautobot; it does not control data access or grant permissions.

- `strict`: classify every request for Nautobot relevance, using conversation context to interpret follow-ups. Requests not classified as relevant are refused; classifier failures are not treated as permission to proceed.
- `conversation`: classify the first request and reject clear unrelated topic switches in an ongoing conversation while permitting follow-ups. This is the default.
- `off`: disable the relevance gate, permitting general-purpose requests. Nautobot object permissions, source authorization, and other tool safeguards still apply.

The classifier has no Nautobot data or MCP tool catalog. The answering Agent determines what available capabilities can fulfill a request.

## Permissions, skills, and integrations

`builtin_skills` controls packaged skill instructions deployment-wide, not per user. Disabling them does not disable chart or Mermaid rendering or remove the `render_chart` tool. Built-in skill identifiers remain reserved even when disabled.

`source_apps_by_group` controls which installed App source members may inspect. MCP profiles provide HTTP headers and stdio environment values. See [Permissions and source access](permissions.md), [Integrations](integrations.md), and [Operations](operations.md) for setup and operation.