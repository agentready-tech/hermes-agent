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
equivalent is `python -m hermes_runtime.standalone.cli`. The source installer
publishes `hermes-autonomous` on PATH alongside `hermes`; updates and launcher
repair refresh the same command automatically.
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

On a Linux VM with Xfce, start the shared desktop at boot before the first RDP
connection and install the foreground Hermes owner:

```sh
hermes-autonomous install-service --rdp --enable
```

Run this as the same local Unix account that will log in through RDP. This mode
requires systemd and an existing Xfce installation (`startxfce4`, `xfce4-session`,
`xfconf-query`). Xfce is the desktop; xrdp is the RDP server and xorgxrdp is its
Xorg display driver. They are separate packages and need not come with Xfce.
On Ubuntu/Debian the command automatically installs compatible xrdp/xorgxrdp
0.10+ and the X11/D-Bus utilities. A compatible installed stack is reused.
If the OS repository only offers 0.9, it builds xrdp 0.10.6.1 and xorgxrdp 0.10.5
as native `.deb` packages against that VM's libraries. Both upstream sources and
Debian packaging are pinned and SHA256 checked before extraction. This fallback
installs build dependencies and needs several minutes and internet access; it
does not add a PPA or mix repositories from other OS releases. Package upgrades
preserve existing configuration and back up the original RDP INI files first.
For the standard Debian/Ubuntu certificate, it grants the unprivileged xrdp daemon
membership in `ssl-cert` and refreshes the daemon when needed, so TLS works too.
Custom certificate permissions are preserved.
The installer uses `sudo` for host configuration; do not run the whole command as a
different account or it will install that account's desktop and Hermes home.

The installer adds one `hermes-xfce-rdp-<uid>.service` system unit per Unix account.
It uses the current Hermes interpreter and home to call native `xrdp-sesrun` as
that account, starts Xorg and Xfce, and captures the live display credentials.
The Hermes owner waits for the desktop's X11 readiness before launching the
backend and gateway. A running RDP desktop is reused; an existing console desktop
or several RDP desktops causes an error instead of creating another session.
Do not enable a second LightDM autologin for this same account in this mode.

Connect using the same account and the **Xorg** backend in the RDP client. xrdp
reconnects to that desktop; closing the RDP client leaves Xfce, applications and
Hermes running. The installer sets `Policy=Default`, `KillDisconnected=false`,
`DisconnectedTimeLimit=0`, `IdleTimeLimit=0`, and selects Xorg for RDP autologin.
It preserves other xrdp settings and saves original host files beside them with
the `.hermes-before-rdp` suffix. It masks Xfce screensaver, light-locker and power
manager autostarts for this account, saving any existing user overrides first,
so the unattended desktop remains available to the agent.
This mode selects local terminal execution and a local headed Chrome browser,
clears a pinned CDP target, and disables automatic Bot Desktop creation. Browser
windows launched by Hermes therefore appear on the same shared RDP screen.

`autonomous.rdp: true` is remembered in this home's `config.yaml`; reinstalling
or upgrading with the ordinary install command keeps the selected mode. The
original `config.yaml` is saved with the same `.hermes-before-rdp` suffix. Main
history, saved prompts, memory, delegation mail and recovery records are not
replaced. After a VM reboot xrdp starts a fresh desktop and Hermes resumes its
durable work; processes and unsaved GUI application state do not survive reboot.

For a headless service or attachment to an independently managed existing desktop,
install the owner without selecting RDP startup:

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
Without managed RDP startup, the VM must start this user's graphical session at boot without a human connection,
for example through the provider's preconfigured desktop or LightDM autologin.
Installing Xfce packages alone does not start a logged-in desktop. Check the VM's
boot/login setup once; this installer does not change the display manager's login
policy. Plain attachment creates no X server, Xfce session, VNC server or RDP session.

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

### Compose with an existing sandbox

`docker/compose.agentsee.yaml` runs one autonomous owner and a private,
sandbox-scoped Docker API socket. Use a unique Compose project and a separate
absolute home per agent. The home is mounted at the **same absolute path** on
both sides, preserving existing workspace, credential and sandbox bind paths.
The image's upstream s6 entrypoint is intentionally replaced by the autonomous
foreground supervisor; production homes are never reseeded or recursively chowned.
This migration template deliberately keeps the existing root UID and sets the
native `HERMES_ALLOW_ROOT_GATEWAY=1` opt-in. The runtime drops all Linux
capabilities and receives only its own data and sandbox-scoped Docker socket.

Set `terminal.docker_container` in the native `config.yaml` to the existing
sandbox name. This selects the ordinary Docker backend's terminal, files and
browser tools, but disables automatic container creation/replacement/removal.
It starts that container if stopped and refuses missing or mismatched images.
Mounts, capabilities and resource changes belong to the operator/Compose.

The host-owned `SANDBOX_POLICY` JSON contains a `sandbox` projection from
`docker inspect`: `Id`, `Name`, `Image`, `HostConfig`, and `Config.Image` only.
It must live outside the agent's writable home. Neither the policy nor the
real Docker socket is mounted into Hermes. The proxy permits inspect, start,
exec for that sandbox only; stop/remove, creation, image pulls, host
mount changes, networks, volumes, builds and access to other containers fail
closed. No request bodies or upstream output are logged.

Deployment variables are `HERMES_IMAGE` (an immutable image), `HERMES_DATA`,
`SANDBOX_POLICY`, and optionally `HERMES_PORT`/`HERMES_MEMORY`. Pass them through
a private Compose env file, distinct from the native home's secret `.env`.
Publish only the authenticated dashboard on localhost. Sandboxes and the
Docker API need no public port. Keep the current sandbox running during a
native-to-container migration; moving the owner does not require restarting
Chromium or VPN.

Before cutover, back up the complete native home and external credential
files, record history/session identity and sandbox process start times, then
stop the old owner and take the final consistent copy. Start the Compose
owner against the **same home**, verify native status, Telegram connectivity,
history preservation and real terminal execution in the same sandbox. Disable
the old service only after verification. On failure stop the new owner and
restart the old service; retain both backup and latest home without silently
discarding messages received during the attempted migration.

The ordinary `hermes-autonomous up` operator below remains available for
deployments that do not use this Compose template.

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
exclusively; separate-session agent cron runs are refused. The suffix is
installed when the main agent is constructed, including on resume, and retained
when its personality overlay changes. Existing
saved system prompts, histories and recovery records are retained. The policy is
installed only on the owning main agent. Native prompt inheritance for background
reviews remains intact so those forks retain the parent's cached prefix.

The agent uses the existing `cronjob_manage` tool to create, list, update, pause,
resume and remove schedules. It runs in the owning Hermes process regardless of
`terminal.backend`, so a Docker terminal needs no host CLI, inbox mount or proxy.
Its returned job view identifies `execution_scope: main`. Tool/script/skill and
delivery requirements belong in the prompt for the main agent to perform.

Schedules created from a messaging conversation retain its registered route;
their timer occurrences enter the native gateway automation queue with that route. The same gateway runs
the main turn and sends its final reply back to that chat/topic; the Docker
terminal needs no `hermes send`. These occurrences do not pass through the
internal UI inbox. Schedules created from web/local interfaces retain the
headless inbox path and do not switch to a registered messaging chat.

Host operators can still use the compatible CLI:

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
main history. Profiles outside the managed runtime retain native cron behavior;
managed agent schedules cannot start separate conversations. Main-inbox
jobs accept prompts, without per-job models, scripts or skill overrides. Put work
the main agent should perform into its prompt; unsupported options do not silently
change execution scope. Native cron edit/pause/resume/remove commands
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
