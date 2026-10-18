# Autonomous Hermes fork

This fork runs its dashboard, messaging gateway, native scheduler, persistent
main inbox and optional Gmail ingress without an AgentReady application,
Control Plane, PostgreSQL, external config generator or mandatory MCP server.
The fork's session adapters and autonomous services live in `hermes_runtime`.
Delegation creates reusable persistent children by default; only an explicit
`ephemeral=true` call creates one-off workers. A missing project name receives
an automatic saved identity. Use `send` with its returned subagent ID to continue
the same history; `project` is an optional stable name for finding/reusing it.

Parents and subagents can exchange intermediate questions, findings and replies
through `delegate_task(action='message')`; `peers` discovers recipients and `wait`
waits for replies without finishing the task. Use `subagent_id='parent'` for the
immediate parent. Coordination mail is durable in `state.db` and enters the same
native history at a safe boundary. Idle persistent recipients reuse their saved
session and queue; explicit pauses and archives remain effective.

## Start

Use Python 3.14 and the native Hermes PM installation, or the fork's Docker
image. An installed fork exposes `hermes-autonomous`; from a checkout the
equivalent is `python -m hermes_runtime.standalone.cli`.
Foreground supervision supports Linux and macOS; use the Linux container on Windows.

```sh
hermes-autonomous init
hermes setup
hermes-autonomous run
```

`init` merges service defaults into native `config.yaml`, creates missing native
dashboard credentials in `.env` and leaves existing settings, secrets and data
in place. Select the model/provider and credentials through native `hermes setup`
or those same files. Edit `SOUL.md`, `skills/`, native `mcp_servers`, gateway
platforms and cron as usual. New homes select the bundled Holographic memory;
an existing memory provider is preserved. No AgentReady URLs or tokens are
created. Existing optional integrations remain the operator's configuration.
Fresh homes use native SQLite DELETE journals, which also work on Docker
Desktop bind mounts. An existing journal setting is preserved. The foreground
owner initializes the home schema before starting concurrent native children.
The transient IPC socket uses a private OS runtime directory; durable locking,
history and the inbox stay in the home. This also avoids Docker Desktop's socket
permission limitations on host bind mounts.

The foreground owner supervises one backend, one native gateway and, when
configured, one Gmail poller. A per-home OS lock prevents a second owner; the
native gateway lock still protects the gateway. Only a failed child restarts,
with backoff. SIGTERM drains children before exiting. Status validates process
start times and HTTP health, so a reused PID cannot count as a healthy service.

On Linux, install the foreground owner under systemd:

```sh
hermes-autonomous install-service --enable
```

`--enable` also enables user lingering, so the service starts after reboot without
an SSH login. It registers an XDG autostart entry for Xfce/Xubuntu under this Unix
account. Run the installer as the desktop user, with the same `--home` as the
running agent. Installing from SSH discovers that user's existing Xfce session;
multiple sessions require installing from the intended desktop instead of guessing.
The login hook captures the current display, Xauthority path and session D-Bus,
then reconnects the enabled service with a normal restart. Reinstalling the service
refreshes these files and restarts only its existing home owner.

For a VM supplied with Xubuntu/Xfce, Hermes uses that existing desktop and browser.
The VM must start this user's graphical session at boot without a human connection,
for example through the provider's preconfigured desktop or LightDM autologin.
Installing Xfce packages alone does not start a logged-in desktop. Check the VM's
boot/login setup once; this installer does not change the display manager's login
policy. Hermes creates no X server, Xfce session, VNC server or RDP session.

Use remote access that shows this same X11 desktop: you and Hermes should see the
same windows and browser. You can connect to observe or interact, then disconnect;
the desktop and its applications must stay running. A separate desktop created
only for a remote login will not show what Hermes is doing on the shared desktop.

Disconnecting an SSH/RDP/viewer connection does not stop the Hermes service. GUI
control remains available while the existing desktop stays running independently
of that connection. If the provider destroys the desktop at disconnect, that host
configuration must be changed; service attachment cannot keep a destroyed desktop
alive. Reconnecting a viewer alone does not restart the agent. A new graphical
login updates the binding through the Xfce hook. Until the desktop starts, the
agent's inbox, scheduler and recovery remain available. SSH-forwarded displays and
another user's session are never selected. A saved binding is private to this
account and rejected after a different boot; service startup can rediscover a live
Xfce session when the login hook has not run. Headless services still work normally.

Prepare native GUI support once with `hermes computer-use install` and enable
Computer Use in `hermes tools`; check it in the desktop with `hermes computer-use
doctor`. The service uses the same host desktop and local browser when configured
for local terminal/browser execution. No Bot Desktop or second virtual display is
created by this installer. Existing history, memory, main-session identity,
subagent queues and recovery records stay in the same home through reconnects.

Do not also run a native gateway or dashboard service for the same home.
Inside the native s6 image, the autonomous
owner parks the root gateway/dashboard slots before taking ownership.
Explicit native image commands still work, including `python -m
hermes_runtime serve` for existing integrations.

Custom launchers use `python -m hermes_runtime` with this fork revision.
After updating a source installation, regenerate an existing systemd user
service with `hermes-autonomous install-service --enable`. Update the host's
fork installation before upgrading a Docker deployment; its stable
`hermes-autonomous` command can manage and roll back previous fork images.

## Docker and durable data

On a host with the installed fork and Docker:

```sh
hermes-autonomous --home /srv/hermes up --image hermes-fork:COMMIT
```

This creates a container with `unless-stopped` restart policy, bounded Docker
logs and localhost port 9119. The complete home is bind-mounted at `/opt/data`.
Open the native dashboard at `http://localhost:9119`; credentials are in the
home's `.env`. Configure the model before submitting work. To use only Docker,
the equivalent foreground image command is:

```sh
docker run -d --name hermes --restart unless-stopped --stop-timeout 60 \
  --log-driver local --log-opt max-size=10m --log-opt max-file=3 \
  -p 127.0.0.1:9119:9119 --mount type=bind,src=/srv/hermes,dst=/opt/data \
  -e HERMES_HOME=/opt/data hermes-fork:COMMIT
```

The `up` controller owns the topology it creates; its update commands do not
adopt arbitrary existing containers. Store new agent workspaces under the home
or add explicit persistent mounts at creation:

```sh
hermes-autonomous --home /srv/hermes up --image hermes-fork:COMMIT \
  --mount /srv/projects:/projects
```

The host deployment record and snapshots default to `/srv/hermes-deployment`,
outside the agent's home. Override with the global `--deployment` option.
Host `event`, `schedule` and `status` commands enter the owned container; they
never open its live SQLite files from the host OS. Backup reads the databases
only after all container writers have stopped.

## One main inbox for external events and schedules

The main agent receives mandatory scheduling instructions through Hermes' native
per-agent system suffix, even without skills tools. Schedules target the main inbox
by default; isolated execution requires an explicit user request. The suffix is
installed when the main agent is constructed, including on resume, and retained
when its personality overlay changes. Existing
saved system prompts, histories and recovery records are retained. The policy is
installed only on the owning main agent. Native prompt inheritance for background
reviews remains intact so those forks retain the parent's cached prefix.

```sh
hermes-autonomous event --source webhook --id delivery-42 'Supplier moved delivery to Monday'
hermes-autonomous schedule --when 'every 1h' --name 'Order review' 'Review outstanding orders and delegate project work as needed'
hermes cron list
```

An event is acknowledged after its receipt is committed to the existing
`state.db`. `(source, event_id)` is the deduplication key. The already-bound
backend watches that inbox and wakes the current main-session head, including
after a restart and without a human opening a WebSocket. External content keeps
the existing untrusted-data envelope.

`schedule` creates a normal native cron record with an explicit main-inbox
origin. The native occurrence/execution ledgers still own scheduling and crash
catch-up. A fire's deterministic execution ID deduplicates admission. A successful
cron run means **accepted into the main inbox**; its model outcome appears in the
main history. Ordinary native cron jobs, scripts, monitor jobs and their result
delivery are unchanged. Manage both kinds with native cron commands. Main-inbox
jobs accept prompts, without per-job models, scripts or skill overrides. Put work
the main agent should perform into its prompt; unsupported options do not silently
change execution scope. Use an ordinary native cron job only when separate-session
execution was explicitly requested. Native cron edit/pause/resume/remove commands
manage main-inbox jobs while retaining their origin routing.

Main-session compression, recovery, manual session commands, memory source and
revision history, automatic recall/consolidation and persistent project subagents
are the same fork implementations. Main-session work can use the existing
delegation tools and reuse each project's own session and durable queue.
The bundled `hermes-autonomous` skill teaches this scheduling and project-reuse
workflow through native `terminal` and `delegate_task` tools.

## Mail

Native IMAP/SMTP remains available through the shipped email gateway adapter.
For Gmail OAuth ingress, add secrets to the same `.env`:

```dotenv
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
```

Use a refresh token authorized to read the desired Gmail account. Set in native
`config.yaml`, then restart the autonomous owner:

```yaml
autonomous:
  enabled: true
  host: 127.0.0.1
  port: 9119
  gmail:
    enabled: true
    query: in:inbox
    poll_seconds: 60
    allowed_senders:
      - supplier@example.com
```

An explicit `['*']` accepts all senders. The poller pages through the selected
mailbox, skips already accepted IDs using the existing inbox and commits the
message before treating it as accepted. A worker crash after acceptance does
not duplicate it. Mail labels/read status are untouched. Body, sender, thread
and Message-ID evidence accompany the event. Attachments are saved under
`workspace/mail/`; individual parts are bounded at 20 MiB and total decoded
message data at 25 MiB. Oversized messages remain in Gmail and produce a poll
failure in service logs. Sender filtering uses the message's From header and
does not establish cryptographic sender identity. This OAuth adapter ingests
mail; outbound mail uses the native email channel/tool configuration.

## Updates and rollback

Run these commands on the Docker host, with the same home/deployment options:

```sh
hermes-autonomous --home /srv/hermes backup
hermes-autonomous --home /srv/hermes upgrade --image hermes-fork:NEW_COMMIT
hermes-autonomous --home /srv/hermes rollback /srv/hermes-deployment/snapshots/SNAPSHOT
hermes-autonomous --home /srv/hermes recover
hermes-autonomous status
```

Images must already be pulled/built on the host. The controller resolves the
immutable local image ID and preflights the candidate before stopping the old
container. It stops all agent writers, snapshots every persistent mount,
including secrets, full history, facts and corrections, subagent registries and
queues, recovery checkpoints, workspaces and Git metadata. SQLite stores use the
native WAL-safe backup operation. Uncovered project/memory/symlink paths cause
an explicit refusal; include their persistent mount rather than silently lose
data. Snapshots are private and checksummed; keep them outside persistent mounts.

After replacement, health must pass for the owner, all children and native HTTP
backend. A failed candidate restores the old image **and its matching data**.
Replaced directories are retained alongside their originals as
`*.before-restore-*`; no automatic pruning removes them or snapshots. A durable
transition record supports explicit `recover` after a host/controller crash.
Run only one controller for the deployment. Stop other writers to additional
workspace mounts during updates. Rollback restores local state; it cannot undo
external effects already performed by an agent during the candidate's lifetime.

The main service and Gmail adapter are exercised with local provider/Gmail HTTP
fixtures; configuring a real mailbox still requires its own OAuth credentials.
