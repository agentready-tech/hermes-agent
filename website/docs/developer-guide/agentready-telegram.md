# Telegram in the managed AgentReady runtime

A managed employee uses the existing shared BotMode conversation. Telegram is a chat
surface, not an administration console. Ordinary Hermes installations keep their
native menu and slash commands; the restriction follows the existing managed-process
and profile activation boundary, without a second configuration flag.

Managed bots publish an empty command menu in the default, private, group and lazy
forum scopes. The inline command/skill picker also returns no catalog. Clearing the
menu alone is insufficient: typed commands are checked before native batching,
queueing or dispatch. Only `/start`, `/stop`, `/approve` and `/deny` reach their existing
native handlers. The native sender authorization still runs first. The model never
receives a blocked technical command as a replacement task.

Old model, configuration, update and destructive slash-confirmation buttons are
refused before their handlers run. Pending action approvals, clarifications, mail
triage and protected secret cancellation retain their native ownership checks and
transport. Secret replies continue to be consumed before ordinary chat admission.
No session reset, new conversation, provider change or history rewriting is involved.

The command menu must be cleared on every reconnect: a one-off Bot API update is
otherwise overwritten by native startup registration. A rollout verifies the bot's
stored menu metadata separately from isolated command/callback tests. No real
administrative command is run against a customer's employee to demonstrate rejection.
