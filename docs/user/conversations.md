# Conversations

Conversations are saved in Nautobot and do not expire automatically. Trace settings do not control conversation history. Deleting a conversation does not delete information already sent to a model provider or external MCP system; see [Privacy and data handling](privacy.md).

Long conversations may be compacted for model context. Earlier messages replaced by a generated summary no longer appear in restored chat or saved transcripts; the generated summary is not displayed as something you wrote.

## Personal Conversation History

Use **Nautobot Agent → Conversation History** to find conversations in your personal history. Open a row to see its read-only transcript. The list is limited to your own conversations even if you have permission to view other people's conversations elsewhere in Nautobot.

Available actions depend on your Nautobot permissions and whether you own the conversation:

- **Resume** is available only to the conversation owner. It opens that conversation in **Nautobot Agent → Agent**; a user who can read another person's conversation cannot resume it.
- Rename requires the applicable `change` permission; only the title can be changed.
- Delete requires the applicable `delete` permission. Confirmed deletion removes the conversation and its stored artifacts.

Personal history does not offer bulk deletion. A conversation with a queued or running request cannot be deleted.

## All Conversations

**Nautobot Agent → All Conversations** is available to users with the `nautobot_agent.viewall_agentconversation` permission and active superusers. It can list conversations across owners, subject to Nautobot's object-view restrictions. This access alone does not grant rename, delete, or Resume rights; **Resume** remains owner-only.

Bulk deletion is available in **All Conversations** and requires the applicable delete permissions. Confirm only the intended selection. Conversations with queued or running requests cannot be deleted; other ineligible conversations may be skipped. Review the result, since a bulk operation may remove only some selected conversations.

## Returning to a selected conversation

After a normal departure from the Agent page, a later visit to **Nautobot Agent → Agent** may start a new conversation if the configured return timeout has elapsed. This only changes which conversation is selected in that browser; it does not delete history or detect inactivity while a page remains open. Selecting **New chat** or **Resume** opens the requested conversation. Administrators can change the timeout in [Configuration](../admin/configuration.md).

If a detail transcript is unavailable, return to Conversation History and try again later; contact your administrator if it remains unavailable.
