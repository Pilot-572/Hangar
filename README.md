# Hangar

A clean, mobile-first control panel for Proxmox. Tap to start a VM, hold to stop one. Works across a cluster (or multiple clusters) from one screen.

![demo](docs/demo.gif)

## Security (read first)

Hangar is a LAN tool. Login is opt-in but on by default from v0.1.3. Set username and password at install to gate access.

- Do not expose port 8080 to the internet. No port-forwarding, no naked reverse proxy without auth. Use Tailscale, a reverse proxy with auth (Authelia, Caddy basic-auth), or Cloudflare Access.
- The Proxmox API token uses a custom **HangarOps** role with only the privileges Hangar actually calls (`Sys.Audit, VM.Audit, VM.PowerMgmt, VM.Config.Options, VM.GuestAgent.Audit`). A compromise still lets an attacker start/stop every VM and read guest metadata, but not create/delete VMs, change disks, mount storage, or edit ACLs. Rotate the token if you stop using Hangar.
- `verify_ssl: false` is the default because most homelabs run PVE on a self-signed cert. On a shared LAN this is MITM-able. Set `verify_ssl: true` (or `HANGAR_NODE_VERIFY_SSL=true`) if your Proxmox has a real cert.
- Cross-origin POSTs are blocked (as of v0.1.1). A single-account login gates every route by default (v0.1.3). Without login, port 8080 is fully trusted. With login enabled, only users holding the password can access the port. Set `HANGAR_DISABLE_AUTH=1` to bypass built-in login if you already run an auth proxy.

## Login

v0.1.3 added a single-account login. Set username + password at first-run install; log in at `http://<host>:8080/login` after. Session lives 30 days sliding.

**Forgot the password?**

```bash
pct exec <CTID> -- /opt/hangar/.venv/bin/python -c "from app import set_credentials; set_credentials('admin', 'newpassword')"
systemctl restart hangar
```

**Reverse-proxy auth already in front?**

Set `HANGAR_DISABLE_AUTH=1` to skip the built-in login. Any auth in front of Hangar (Authelia, Caddy basic-auth, Cloudflare Access, Tailscale) should be enough.

## One-command install (Proxmox host)

The fastest path. Run this on **any node's Proxmox shell** and it'll create an LXC, install Hangar as a systemd service inside it, generate the API token, and print the URL:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Pilot-572/hangar/main/install.sh)"
```

Run this **on the Proxmox host shell**. It creates the LXC for you.

Tunables (set as env before running): `CTID`, `LXC_HOSTNAME`, `STORAGE`, `TEMPLATE_STORAGE`, `NET_BRIDGE`, `MEMORY`, `CORES`, `ACCENT`, `REPO`, `REF`.

## Manual quickstart (Docker on any host)

You need:

- A Proxmox host (or cluster).
- One API token. On any Proxmox node's shell:

  ```bash
  pveum user add hangar@pam
  pveum role add HangarOps -privs "Sys.Audit,VM.Audit,VM.PowerMgmt,VM.Config.Options,VM.GuestAgent.Audit"
  pveum acl modify / -user hangar@pam -role HangarOps
  pveum user token add hangar@pam hangar-token --privsep 0
  ```

  Copy the `value` field it prints. That's your token secret, and it's only shown once.

  > Upgrading from v0.1.2 or earlier? Your token has `Administrator` role. Downgrade with:
  > `pveum role add HangarOps -privs "Sys.Audit,VM.Audit,VM.PowerMgmt,VM.Config.Options,VM.GuestAgent.Audit" && pveum acl modify / -user hangar@pam -role HangarOps && pveum acl delete / -user hangar@pam -role Administrator`

Then run Hangar with three env vars, no config file needed:

```bash
docker run -d -p 8080:8080 \
  -e HANGAR_NODE_URL="https://192.168.1.10:8006" \
  -e HANGAR_NODE_TOKEN_ID="hangar@pam!hangar-token" \
  -e HANGAR_NODE_TOKEN="<paste-secret-here>" \
  --name hangar --restart unless-stopped \
  ghcr.io/pilot-572/hangar:latest
```

Open `http://<host>:8080`. On your phone, use "Add to Home Screen" / "Install app" for a fullscreen PWA.

> Run Hangar **in an LXC or VM** on your cluster, not on the Proxmox host itself. Keep the hypervisor clean.

## Updating

**Do NOT re-run `install.sh` to update.** Every run creates a new LXC and rotates the Proxmox API token, which breaks whichever install you had before.

For the LXC install path, `git pull` inside the existing container:

```bash
pct exec <CTID> -- bash -c "cd /opt/hangar && git fetch --tags && git checkout v0.1.3 && /opt/hangar/.venv/bin/pip install -r requirements.txt && systemctl restart hangar"
```

Swap `v0.1.3` for whichever tag you want. Your settings, login, and Proxmox token stay put, they live in `/etc/hangar/` (outside the repo), so the code changes but the config doesn't.

For the Docker install path, pull the new image tag and recreate the container. Your mounted `hangar.yaml` volume carries settings across.

## Configuration

### Environment variables (recommended)

| Variable                    | Required | Default     | What                                                  |
|----------------------------|----------|-------------|-------------------------------------------------------|
| `HANGAR_NODE_URL`          | yes      |             | Proxmox API URL, e.g. `https://192.168.1.10:8006`     |
| `HANGAR_NODE_TOKEN_ID`     | yes      |             | Full token id, e.g. `hangar@pam!hangar-token`         |
| `HANGAR_NODE_TOKEN`        | yes      |             | Token secret (the `value` field from `pveum`)         |
| `HANGAR_NODE_NAME`         | no       | `proxmox`   | Display name in the UI                                |
| `HANGAR_NODE_VERIFY_SSL`   | no       | `false`     | `true` if your Proxmox has a CA-signed cert           |
| `HANGAR_ACCENT`            | no       | `#3b82f6`   | Any hex color, themes the UI accent                   |

### YAML (advanced, multi-cluster)

If you want to point Hangar at more than one cluster, mount a YAML config:

```yaml
# /config/hangar.yaml
theme:
  accent: "#10b981"   # any hex color
nodes:
  - name: home
    url: https://192.168.1.10:8006
    token_id: hangar@pam!hangar-token
    token_secret: <secret>
    verify_ssl: false
  - name: office
    url: https://10.0.0.10:8006
    token_id: hangar@pam!hangar-token
    token_secret: <other-secret>
    verify_ssl: false
```

Then point Hangar at it:

```bash
docker run -d -p 8080:8080 \
  -v $(pwd)/hangar.yaml:/config/hangar.yaml:ro \
  --name hangar --restart unless-stopped \
  ghcr.io/pilot-572/hangar:latest
```

`chmod 600 hangar.yaml`. Don't commit it.

> A single Proxmox cluster only needs **one** node in `nodes:`. The Proxmox API federates, so Hangar will see all cluster members through that one endpoint. Add a second entry only when you have a genuinely separate cluster.

## Theme

Any hex color in `HANGAR_ACCENT` or `theme.accent`. Default is `#3b82f6` (Proxmox blue). The whole UI (buttons, meters, active filter, app icon) picks it up automatically.

## Roadmap

- v0.1: what you see here. List, start/hold-to-stop, per-node and per-VM CPU/RAM, dark mode, PWA, theme color.
- v0.2: feedback-driven. Likely WoL, backup status, settings UI, snapshots.

Open an issue with what you want next.

## FAQ

**Why not just use the Proxmox web UI?**
On a desktop, do. On a phone the Proxmox UI is awkward. Hangar is one screen, one tap.

**iOS / Android / Windows app?**
The PWA installs on all four. No App Store, no Play Store.

**Why hold-to-stop?**
A single tap on a phone is too easy to mis-fire, so a 1-second press confirms intent without needing a modal. The button shows a small progress bar while held. Release any time before 1s cancels.

## License

[AGPL-3.0](LICENSE). If you host Hangar as a service, you must publish your modifications.
