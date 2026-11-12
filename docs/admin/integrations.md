# Integrations

Nautobot Agent includes built-in chart and Mermaid skills, optional custom skills, bundled native model integrations, and optional Model Context Protocol (MCP) servers. See [Configuration](configuration.md) for the complete settings reference and provider examples.

## Custom skills

Custom skills are global instructions sent to the configured model provider. They do not grant Nautobot permissions or add tools. Do not include secrets or instructions that imply broader authorization than users already have.

1. Confirm the intended administrators have the native Skill permissions; see [Permissions](permissions.md).
2. Open **Nautobot Agent → Skills** and add a name, description, Markdown instructions, and enabled state.
3. Review the full text before enabling it; enabled custom instructions are included in execution.
4. Confirm through normal operation that data and tool access remain governed by Nautobot permissions.

The packaged `nautobot-charts` and `nautobot-mermaid` skills are included by default and cannot be edited or disabled in the Skills interface. Administrators can exclude both by setting `PLUGINS_CONFIG["nautobot_agent"]["builtin_skills"] = False`; see [Configuration](configuration.md). Enabled custom skills remain available, but cannot use the reserved built-in identifiers. Disabling packaged skills removes their instructions, not chart or Mermaid rendering capabilities.

## LLM configuration and credential profile

Only superusers can save LLM configurations because provider options determine where deployment credentials are sent.

1. Put provider credentials in a named `credentials` profile in deployment configuration, using the deployment's secret-management mechanism. Do not put credentials in the model's **Parameters** field.
2. In **Nautobot Agent → LLM Configurations**, enter `provider:model`, choose the matching **Credential profile**, and enter non-secret provider options in **Parameters** as a JSON object.
3. Assign exactly one configuration as the default Agent model. A separate scope-classifier configuration is optional; without one, the Agent model is used for classification.
4. Ensure the Agent model supports tool calling and exposes a positive integer `profile.max_input_tokens`. Missing context metadata prevents Agent tools from running. Classifier-only configurations are exempt.
5. Save, then reload the Agent page. Reload web and worker processes after deployment configuration changes.

Profile values take precedence over matching Parameters values. Model IDs and saved configurations do not guarantee that a provider/model combination is available or compatible. See [LLM configurations and model roles](configuration.md#llm-configurations-and-model-roles).

## MCP servers

Only enable MCP servers that the Nautobot operator trusts. MCP tools can access systems or cause external side effects using the configured credentials and worker identity. Every user who can chat can call every enabled server; there are no per-user or per-group MCP access restrictions. Nautobot permissions and `blocked_models` do not restrict external MCP servers. Review its operator, available tools, credentials, worker identity, network access, and possible effects before enabling it.

1. Define required profiles in `PLUGINS_CONFIG["nautobot_agent"]`: use `mcp_credentials` for HTTP header values and `mcp_environments` for stdio environment variables. Store secrets in deployment secret management.
2. Ensure the endpoint or executable is available to every worker that can run chat, then reload processes after deployment configuration changes.
3. In **Nautobot Agent → MCP Servers**, enter a descriptive name, select the transport, provide its settings, select the applicable profile if needed, and leave **Enabled** off during review.
4. Test from the worker environment and review the server's tools and effects. Enable it only after review; repeat the review when its endpoint, executable, profile, or worker identity changes.

MCP records contain profile names and connection or launch settings, not deployment secrets. Every value in a selected profile must be set, even when the server is disabled. A server's name is shown to chat users when the integration is unavailable and sent to the model provider; use a non-sensitive label.

### HTTP transport

1. Select **HTTP** and provide the server's HTTP(S) URL. It must include a hostname and must not include user information or a fragment. Do not put credentials in the URL or query string.
2. Leave the stdio launch field empty.
3. If authentication headers are needed, select a named `mcp_credentials` profile.
4. Confirm that workers can reach the endpoint and that its TLS and network access meet deployment requirements.

### Standard input/output (stdio) transport

1. Select **Standard input/output** and leave the HTTP URL empty.
2. Enter a JSON launch object with a nonblank `command` and optional `args` (string array), `cwd` (absolute path), and `env` (string-to-string object). Do not include an `mcpServers` wrapper or `type`/`transport` keys. For example:

   ```json
   {"command": "runner", "args": ["--serve"]}
   ```

3. Install the executable and its dependencies on every eligible worker. It runs with the Nautobot worker account's filesystem and network access; saving the form does not check that the executable exists.
4. If needed, select an `mcp_environments` profile. Profile values override matching values in launch `env`; keep secrets in the deployment profile.
5. Test the command as the worker account before enabling the server.

For a Python-packaged server, install it into a dedicated environment accessible to the worker. Example for the reference time server:

```shell
python -m venv /opt/mcp/time-venv
/opt/mcp/time-venv/bin/pip install mcp-server-time
/opt/mcp/time-venv/bin/python -m mcp_server_time --help
```

Example launch JSON on Linux:

```json
{"command": "/opt/mcp/time-venv/bin/python", "args": ["-m", "mcp_server_time"]}
```

On Windows, use forward slashes or escape backslashes as `\\` in JSON:

```json
{"command": "C:/mcp/time-venv/Scripts/python.exe", "args": ["-m", "mcp_server_time"]}
```

Use the interpreter where the package is installed. Paths are resolved on the worker, not the browser's computer.

If the form reports a missing command, provide the bare launch object above rather than client configuration wrapped in `mcpServers`. For invalid JSON on Windows, use forward slashes or double backslashes. If a saved server is unavailable, check that the command and package are accessible to the worker and that any `cwd` exists on that worker.

A lost response, timeout, or cleanup warning does not establish whether an external operation happened or was rolled back. Nautobot Agent does not automatically retry MCP operations. Reconcile the external system before retrying an interrupted or uncertain operation; see [Operations](operations.md#interrupted-runs-and-diagnostic-warnings).
