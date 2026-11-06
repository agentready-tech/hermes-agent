# Optional standalone Dashboard

The autonomous service can run without a separate `hermes dashboard` / `serve`
process. Select this per agent in its existing `config.yaml`:

```yaml
autonomous:
  dashboard:
    enabled: false
```

An omitted setting preserves the existing behaviour (`true`). This flag is a
startup option; applying a changed value requires restarting the autonomous
service. It does not change an image, provider, Panel address or enrollment.

With the Dashboard disabled:

- The supervisor starts the gateway and configured workers, with no `backend`
  child, Dashboard health probe or new Dashboard login credentials.
- Panel keeps its existing outbound registration and channels. Chat uses the
  gateway-owned main conversation; no independent chat writer is started.
- Screen RPC/RFB and subscription configuration reuse the live gateway API.
  Private same-home bootstrap mints fresh native tickets; credentials and WS
  tickets are not placed into public Panel configuration or logged.
- API endpoint discovery follows gateway restarts. An unavailable owner is a
  refusal, never a reason to start a Dashboard or another gateway.

The main conversation must already have its native gateway route for managed
chat. The headless connector does not fall back to the old independent UI chat.
The gateway still loads API handlers when used; removing a process does not mean
all of its previous memory consumption disappears.

Tests use local fixture services only. They do not log into a real provider,
operate a real desktop, alter production agents or exercise real model requests.

## Browser working tabs

Browser instructions default each executor to one working tab, reusing it when
navigating to another website. A subagent may keep its own tab; extra tabs are
for simultaneous work and should close as soon as they are no longer needed.

AgentReady profiles can also request cleanup of ordinary user-opened tabs after
successful browser work:

```yaml
browser:
  close_idle_user_tabs: true
```

The setting lives in each agent's existing configuration. Unconfigured Hermes
profiles retain user tabs. Idle user-tab cleanup waits for other known browser
executors and running desktop operations. Tabs owned by another task, retained
results, unfinished forms and active human control remain protected. A change
of human control during cleanup invalidates that cleanup admission. Native
driver shutdown prevents its background startup tab from being recreated.
