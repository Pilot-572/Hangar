# Hangar

A clean, mobile-first control panel for Proxmox. Tap to start a VM, hold to stop one. Works across a cluster (or multiple clusters) from one screen.

![demo](docs/demo.gif)

## One-command install (Proxmox host)

The fastest path. Run this on **any node's Proxmox shell** — it creates an LXC, installs Hangar as a systemd service inside it, generates the API token, and prints the URL:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Pilot-572/hangar/main/install.sh)"
```

Tunables (set as env before running): `CTID`, `HOSTNAME`, `STORAGE`, `NET_BRIDGE`, `MEMORY`, `CORES`, `ACCENT`.

## Manual quickstart (Docker on any host)

You need:

- A Proxmox host (or cluster).
- One API token. On any Proxmox node's shell:

  ```bash
  pveum acl modify / -user hangar@pam -role Administrator
  pveum user token add hangar@pam hangar-token --privsep 0
  ```

  Copy the `value` field it prints — that's your token secret. Shown once.

Then run Hangar with three env vars — no config file needed:

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

## Configuration

### Environment variables (recommended)

| Variable                    | Required | Default     | What                                                  |
|----------------------------|----------|-------------|-------------------------------------------------------|
| `HANGAR_NODE_URL`          | yes      | —           | Proxmox API URL, e.g. `https://192.168.1.10:8006`     |
| `HANGAR_NODE_TOKEN_ID`     | yes      | —           | Full token id, e.g. `hangar@pam!hangar-token`         |
| `HANGAR_NODE_TOKEN`        | yes      | —           | Token secret (the `value` field from `pveum`)         |
| `HANGAR_NODE_NAME`         | no       | `proxmox`   | Display name in the UI                                |
| `HANGAR_NODE_VERIFY_SSL`   | no       | `false`     | `true` if your Proxmox has a CA-signed cert           |
| `HANGAR_ACCENT`            | no       | `#3b82f6`   | Any hex color — themes the UI accent                  |

### YAML (advanced — multi-cluster)

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

> A single Proxmox cluster only needs **one** node in `nodes:` — the Proxmox API federates and Hangar will see all cluster members through that one endpoint. Add a second entry only when you have a genuinely separate cluster.

## Theme

Any hex color in `HANGAR_ACCENT` or `theme.accent`. Default is `#3b82f6` (Proxmox blue). The whole UI — buttons, meters, active filter, app icon — picks it up automatically.

## Auth

Hangar has no app-level login. Put it behind Tailscale, a reverse proxy with auth (Authelia, Caddy basic-auth, nginx-auth-request), or Cloudflare Tunnel.

## Roadmap

- v0.1 — what you see here: list, start/hold-to-stop, per-node and per-VM CPU/RAM, dark mode, PWA, theme color.
- v0.2 — feedback-driven. Likely: WoL, backup status, settings UI, snapshots.

Open an issue with what you want next.

## FAQ

**Why not just use the Proxmox web UI?**
On a desktop, do. On a phone, the Proxmox UI is awkward — Hangar is one screen, one tap.

**iOS / Android / Windows app?**
The PWA installs on all four. No App Store, no Play Store.

**Why hold-to-stop?**
A single tap on a phone is too easy to mis-fire — a 1-second press confirms intent without a modal. The button shows a small progress bar while held; release any time before 1s cancels.

## License

[AGPL-3.0](LICENSE). If you host Hangar as a service, you must publish your modifications.
