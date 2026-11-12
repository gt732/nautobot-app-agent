"""Template for local, container-only Nautobot Agent development settings."""

import os

from nautobot.core.settings import *  # noqa: F403

PLUGINS = ["nautobot_agent"]

# All deployment settings, with the defaults from nautobot_agent/app_settings.py.
# Credential profiles are populated from variables forwarded by Compose.
# See docs/admin/configuration.md for constraints and provider-specific guidance.
# Restart web and worker services after editing these settings.
PLUGINS_CONFIG = {
    "nautobot_agent": {
        # Exact installed model labels, e.g. ["dcim.Location"]; no wildcards.
        "blocked_models": [],
        # Include packaged chart and Mermaid skills; custom skills are unaffected.
        "builtin_skills": True,
        # Only configured providers appear in the credential-profile selector.
        # Keep actual values in the ignored root .env, never in this file.
        "credentials": {
            **{
                profile: {"api_key": value}
                for profile, variable in {
                    "openai": "OPENAI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "google": "GOOGLE_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                    "deepseek": "DEEPSEEK_API_KEY",
                }.items()
                if (value := os.getenv(variable))
            },
            # Local Ollama needs no API key; its endpoint is optional.
            **({"ollama": {"base_url": url}} if (url := os.getenv("OLLAMA_BASE_URL")) else {}),
        },
        # Named HTTP header profiles for MCP servers.
        # Example entry: "internal": {"Authorization": os.environ["MCP_AUTHORIZATION"]}.
        "mcp_credentials": {},
        # Named environment-variable profiles for stdio MCP servers.
        # Example entry: "internal": {"API_KEY": os.environ["MCP_API_KEY"]}.
        "mcp_environments": {},
        # Exact Nautobot group names mapped to enabled App Python packages.
        # Example entry: "Agent Developers": ["nautobot_agent"].
        "source_apps_by_group": {},
        # "strict", "conversation", or "off"; does not change data permissions.
        "scope_mode": "conversation",
        # Traces may contain unredacted prompts, tool results, and sensitive data.
        "tracing": True,
        # Limits for each stateless Python interpreter execution.
        "interpreter": {
            "max_duration_secs": 5.0,  # Excludes time waiting for host tools.
            "max_memory_bytes": 64 * 1024 * 1024,
            "max_host_calls": 20,
            "max_output_chars": 16000,
        },
        # Maximum UTF-8 JSON response size for live-data tools.
        "query": {
            "max_page_bytes": 1_048_576,
        },
        "query_statement_timeout_ms": 30_000,  # Per SQL statement, not per tool call.
        # Seconds after departure before a plain page visit starts a new chat.
        "chat_return_timeout": 600,
        # Per-run ceilings, including delegated calls.
        "chat_max_model_calls": 40,
        "chat_max_tool_calls": 100,
        # None uses Nautobot's default queue. Workers must consume a custom queue.
        "chat_celery_queue": None,
        # Seconds. Soft limit + cleanup + 10 must be less than the hard limit.
        "chat_soft_time_limit": 300,
        "chat_hard_time_limit": 600,
        "chat_cleanup_timeout": 20,
        "chat_queue_wait_timeout": 600,
    },
}
