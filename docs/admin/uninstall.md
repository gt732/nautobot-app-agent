# Removal

Disabling or uninstalling Nautobot Agent does not delete its database records. Reversing its migration is a separate, destructive action; it is not a routine uninstall step.

## Disable or uninstall while retaining data

1. Stop accepting new Agent requests. Allow queued and running work to finish or cancel it, and confirm no queued or running runs remain. Review interrupted runs and warnings; stopping the app does not prove a worker or external MCP action stopped. Reconcile affected external systems before any retry. See [Operations](operations.md#run-statuses) and [interruption guidance](operations.md#interrupted-runs-and-diagnostic-warnings).
2. Remove only `nautobot_agent` from the existing `PLUGINS` list and its own `PLUGINS_CONFIG` entry. Preserve other apps and deployment settings.
3. If removing the code, uninstall the `nautobot-agent` package through your deployment's package or image process. Reload/restart affected web and worker processes using your normal procedure, then confirm the app is no longer loaded.

This disables the app and optionally removes its code, but leaves its database tables and records in place. It does not delete data already sent to model providers or external MCP systems; provider retention and remote effects are outside the local database.

## Optional destructive data purge

Proceed only when the data owner explicitly intends to remove all Nautobot Agent data and has reviewed the deployment's backup and change-control procedures. Keep the app installed and enabled while reversing its migration. First review the plan in the target deployment:

```shell
nautobot-server migrate nautobot_agent zero --plan
```

The plan may include dependent migrations; inspect it before proceeding. The following command is destructive and drops Agent-owned tables and data, including LLM configurations, skills, MCP server records, conversations, transcripts, checkpoints, runs, traces, and spans:

```shell
nautobot-server migrate nautobot_agent zero
```

Do not treat migration reversal as a backup or export. It cannot remove provider-held copies, external MCP effects, backups, or every reference elsewhere in Nautobot. Decide how to handle deployment-specific references and records before the purge. After an approved purge, remove the package and configuration if they are no longer needed.

Disabling or uninstalling does not imply automatic retention cleanup, provider-side deletion, or reversal of external effects. Review [Operations](operations.md) before handling active or interrupted work.
