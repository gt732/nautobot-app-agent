# Upgrade

Upgrade Nautobot Agent as a coordinated Nautobot deployment change. Follow your deployment's normal backup, approval, and change-control procedures before changing the app or database.

1. Stop accepting new Agent requests using your normal operational procedure. Review queued and running requests, including cancellation requests. Allow work to finish or handle uncertain outcomes deliberately; a missing Redis update does not mean execution stopped. See [Operations](operations.md#run-statuses).
2. Update the approved app package or image through your deployment's normal process. Ensure web and worker environments use the same app version and configuration; do not rely on a change to a disposable container layer.
3. Run Nautobot's post-upgrade command:

   ```shell
   nautobot-server post_upgrade
   ```

4. Reload or restart the affected web and worker processes using your deployment's procedure. Confirm the Agent model and chat queue are configured and that a worker consumes that queue.
5. Using your established deployment verification procedure, confirm an authorized user can open the Agent page and complete a controlled request before accepting normal traffic. Check durable run status and logs; successful configuration loading alone does not prove the provider or integrations are reachable.

Keep trace access restricted because trace payloads may contain unredacted prompts, tool inputs, and results; see [Permissions](permissions.md#execution-traces-and-spans). Follow the selected deployment's Nautobot packaging and persistence procedures when building or updating artifacts.
