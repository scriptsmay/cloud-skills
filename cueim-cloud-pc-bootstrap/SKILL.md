---
name: cueim-cloud-pc-bootstrap
description: "Provision and recover a Cue.im Linux cloud PC: interactively collect required fields, install the CFSM probe, configure an outbound Cloudflare SSH Tunnel, and schedule a 30-minute recovery check."
---

# Cue.im Cloud PC Bootstrap

Guide an agent and user through first-time setup of a Cue.im Linux cloud PC, then leave it recoverable after suspend/resume. The setup is interactive because Cue.im wake scheduling and Cloudflare dashboard actions require the user's account and approval.

## Mandatory host eligibility gate — run this first

Before installing packages, writing files, enabling SSH, enrolling CFSM, or creating/configuring a Tunnel, confirm the user is authorized to administer the target and the relevant Cue.im account.

- A Cue.im cloud PC shared by multiple Agents **under the same user's Cue.im account is supported**. “Shared by Agents” alone is not a reason to stop; this setup has been validated on another Cue.im account.
- If “shared” may mean unrelated users/accounts or a third-party managed host, ask who can access the machine and who controls its credentials. Continue when the user confirms authorization and accepts the host's access scope; otherwise pause before making changes.
- Check whether other Agents or users rely on existing SSH, systemd units, ports, or workspace data. Preserve existing configuration, avoid broad changes, and get approval before replacing or exposing anything.
- Do not assume that Linux, `systemd`, or sudo alone proves authorization. Conversely, do not reject an authorized same-account multi-Agent cloud PC merely because the UI calls it shared.

## Collect fields before changing the machine

Ask for each value and confirm it before use. Do not ask users to paste private keys or account-wide API tokens into chat.

| Field | Required | Safe handling |
|---|---:|---|
| Linux login user and sudo availability | Yes | Read from the host |
| CFSM node display name | Yes | Avoid personal names; use a role or short host label. This is the **name**, never the server ID |
| CFSM server ID | Optional | Either a panel-generated UUID, or leave empty and let the panel auto-discover the node. Never paste the display name here |
| CFSM install secret | Yes | User enters it directly in a hidden prompt or protected file; never echo or log it |
| Cloudflare Tunnel hostname | Yes | User chooses a hostname in a domain they control |
| Cloudflare Tunnel token | Yes | Enter directly on the host via a hidden prompt; never place in chat, shell history, or repository |
| SSH client public key | Yes | User transfers the public key through a trusted channel; never request the private key |
| Persistent data path | Yes | Confirm it survives Cue.im suspend/resume and identify whether rebuilds preserve it |
| CFSM schedule | Default 30 minutes | Confirm Cue.im Agent repeating tasks support this interval; if not, explain the platform limit |

Pause if the user cannot confirm authorization to administer the host or control the CFSM and Cloudflare accounts. Same-account Agent sharing by itself is not a blocker.

## Procedure

1. **Inspect first.** Confirm Linux distribution, architecture, KVM/container type, `systemd`, login user, sudo, disk mount/persistence, and existing listeners. Do not overwrite existing SSH, tunnel, probe, or firewall configuration without showing the diff and getting user approval.
2. **Establish recovery boundaries.** Choose a persistent data directory. Keep credentials outside Git and workspace archives. Explain that suspend/resume persistence does not guarantee survival of instance deletion or provider rebuild.
3. **Prepare SSH.** Install/enable OpenSSH using the distribution package manager. Install only the user's public key into the selected account's `authorized_keys` with directory mode `0700`, file mode `0600`, correct ownership. Do not open public port 22 when using Tunnel access.
4. **Install CFSM probe.** Use the current official CFSM agent installation instructions. Run the installer with the secret supplied without printing it. Pass **only** `-name=<display name>` and omit `-id` so the panel creates the record on first report; use `-id` only when the user has an existing panel-generated UUID to bind to. Confirm a root-managed `cf-probe.service` exists, is enabled and active, then verify the node appears in the panel. Do not infer success from installer exit code alone.
5. **Create the Tunnel with the user.** In Cloudflare Zero Trust, the user creates a remotely managed Tunnel and Public Hostname for SSH (`ssh://localhost:22`). Explain the Access client requirement: SSH clients need `cloudflared access ssh` configured as ProxyCommand; a public hostname alone is not an SSH browser endpoint. Never automate dashboard login or ask for the user's Cloudflare account password.
6. **Install cloudflared.** Use the official package/release for the detected architecture. Resolve its absolute path with `command -v cloudflared`. Store the Tunnel token in a root-owned directory (`0700`) and file (`0600`); configure the system service to read it from a file, not a command-line argument. Back up the unit into persistent storage with mode `0600`.
7. **Install recovery configuration.** Copy `scripts/restore-infra.sh` to the host. Create a root-owned config file (`0600`) with `LOGIN_USER`, binary paths, token path, service backup paths, probe config path, and public-key file path. Store the public key separately (`0644` is sufficient); never put the private key or tunnel token in this script/config repo.
8. **Configure Cue.im wake check.** In the Cue.im Mac/Android client Agent, create a repeating task every 30 minutes that runs the recovery script after the computer wakes. Keep the task idempotent. If Cue.im's minimum interval or task semantics differ, ask the user to choose an available interval rather than claiming a 30-minute guarantee.
9. **Verify end to end.** Confirm all three units (`ssh`, `cloudflared`, `cf-probe`) enabled and active; inspect logs for secrets before sharing; confirm CFSM panel reports the node online; establish SSH from the user's client through `cloudflared access ssh`; run recovery twice and expect the first to report `RESTORED:` only if repair occurred, then `OK:`. If possible, ask the user to suspend/resume once and repeat checks.
10. **Report.** Provide sanitized host baseline, service state, Tunnel hostname, CFSM node label, schedule interval, persistence boundary, and unresolved issues. Redact tokens, secrets, private/public IPs when not operationally necessary, account IDs, user names, and personal domains from public notes.

## Recovery script configuration

The script expects a root-owned shell config file. Example with placeholders only:

```bash
LOGIN_USER='clouduser'
CLOUDFLARED_BIN='/usr/bin/cloudflared'
CLOUDFLARED_TOKEN_FILE='/etc/cloudflared/token'
CLOUDFLARED_UNIT_BACKUP='/var/lib/cueim-cloud-pc/cloudflared.service'
CF_PROBE_BIN='/usr/local/bin/cf-probe'
CF_PROBE_CONFIG='/etc/config/cf-probe/config.conf'
CF_PROBE_UNIT_BACKUP='/var/lib/cueim-cloud-pc/cf-probe.service'
AUTHORIZED_PUBKEY_FILE='/etc/cueim-cloud-pc/authorized.pub'
```

Create it with `install -o root -g root -m 0600`. Set paths to match verified host locations; do not assume package defaults.

## Failure handling

- `WARN:` means a prerequisite is missing or a service could not start. Report the warning and relevant redacted unit status; do not claim successful setup.
- A Tunnel that is healthy but SSH cannot connect usually indicates client-side Access/ProxyCommand configuration, hostname/route mismatch, or host key change. Diagnose those separately.
- If CFSM does not show the host, first check the probe log for `404 Server not found`. That error means the configured server ID does not exist on the panel — typically the display name was pasted into the ID field. `systemctl is-active` still reports `active` in this state, so service status alone is not evidence of enrollment. Fix by clearing the ID (or setting the real UUID) and restarting the service; the panel then creates the record within about 2 minutes and writes the UUID back into the config. Then verify outbound connectivity and the probe config without printing the secret.
- Never automatically delete a Tunnel, rotate credentials, disable firewall rules, or modify account-level Cloudflare policies.

## Security boundaries

- Never commit credentials, private keys, account IDs, personal domains, private hostnames, IP addresses, or user-specific paths.
- Never embed a real SSH public key in a reusable script. Public keys are still linkable identifiers; accept them as user-supplied input.
- Keep `/workspace` project repositories in child directories; exclude credential directories from archives and backups.
- The recovery script can install a user-provided public key and start named systemd units. Review its config and run it only on the host the user authorized.
