---
title: Autonomous computer and Panel access
---

# Autonomous computer and Panel access

The autonomous installer uses Hermes' native Docker backend and its shipped desktop image.
A running Docker daemon, accessible to the Hermes service's Unix account, is required:

```bash
hermes-autonomous install-service --enable --computer
```

This configures the existing native mechanisms:

```yaml
terminal:
  backend: docker
  docker_image: nousresearch/hermes-sandbox:desktop
  container_persistent: true
  docker_persist_across_processes: true
bot_desktop:
  placement: auto
  auto_start: true
  idle_stop_minutes: 0
autonomous:
  computer:
    enabled: true
```

The image supplies Xfce, TigerVNC, Chromium and the browser/computer-use dependencies.
The installer does not install Xfce or a separate Google Chrome on the VM, add an apt
repository, or start a host browser. The existing RDP path remains an explicit separate
option; do not combine `--computer` and `--rdp`.

The native Bot Screen starts through the same persistent sandbox as the terminal.
The Browser launcher and agent use the native sandbox's Chromium profile. Hermes restarts
reuse the container; disconnecting the viewer does not stop the desktop or browser.

Configuration is backed up once to `config.yaml.before-computer`. Re-running the command
updates an older host-computer configuration to Docker and removes its host browser pins.
Existing host Chrome directories, cookies and user data are left untouched; they are not
automatically imported into Chromium. Sign in through the sandbox screen as needed.
The removed `--browser-profile` option is not used for the sandbox.

Agent history, memory, main session, schedules, persistent subagents and recovery state
remain in the existing Hermes home. This installation does not rewrite or recreate them.

## Register with AgentReady Panel

In an existing Panel environment, select **Открыть компьютер**, then issue an enrollment code as an
organization admin/owner. Run the displayed command on the VM:

```bash
hermes-autonomous install-service --enable --computer \
  --panel-url https://agent-ready.tech --environment-id <environment-id>
```

Enter the 15-minute code at the masked prompt. Automation may pipe the code using
`--enrollment-token-stdin` instead. The code is not retained in configuration or command output.

The machine authentication credential stays in this profile's `.env` as `HERMES_PANEL_CREDENTIAL`.
Only its hash is stored in Panel. Non-secret connection settings stay in `config.yaml`:

```yaml
autonomous:
  computer:
    enabled: true
  panel:
    url: https://agent-ready.tech
    environment_id: env_example
```

The connector establishes an outbound WSS connection to Panel. It bridges only native Bot Screen
RPCs and the native ticket-authorized RFB stream to the local dashboard listener. It never forwards
arbitrary tools or caller-selected profiles. The customer's browser needs HTTPS access to Panel;
no SSH tunnel, VPN installation, public VNC/CDP port or Hermes Desktop application is required.

Panel uses the existing native takeover lease and RFB input filter. A normal viewer close hands back
control. A network failure retains the human's exclusion so an agent cannot resume into an interrupted
login. Reconnect and explicitly take over/hand back to recover that lease.

An unavailable Panel does not restart the main agent: the connector retries independently. The native
dashboard remains loopback-only by default. A custom dashboard auth policy still applies; this feature
does not disable it. The supported automatic setup uses the standalone profile's existing local
dashboard session token.

Registration adds a computer capability to an existing Panel environment. It does not replace the
legacy Environment identity/SSO/release contract or provision a new VM. Keep using the normal VM
provisioning/install process before running this command.
