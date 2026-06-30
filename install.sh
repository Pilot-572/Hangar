#!/usr/bin/env bash
# Hangar one-shot installer for Proxmox.
# Usage (on any Proxmox host shell):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Pilot-572/hangar/main/install.sh)"
#
# Creates a small Debian LXC, installs Hangar as a systemd service inside it,
# generates a scoped Proxmox API token, and prints the URL. Idempotent: if the
# user/token already exist, it reuses them.
#
# Tunables (set as env before running):
#   CTID, HOSTNAME, STORAGE, TEMPLATE_STORAGE, NET_BRIDGE, MEMORY, CORES, ACCENT, REPO

set -euo pipefail

CTID="${CTID:-}"
HOSTNAME="${HOSTNAME:-hangar}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
NET_BRIDGE="${NET_BRIDGE:-vmbr0}"
MEMORY="${MEMORY:-256}"
CORES="${CORES:-1}"
ACCENT="${ACCENT:-#3b82f6}"
REPO="${REPO:-https://github.com/Pilot-572/hangar}"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
info()  { printf "\033[36m• %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m! %s\033[0m\n" "$*" >&2; }
fail()  { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }
done_() { printf "\033[32m✓ %s\033[0m\n" "$*"; }

command -v pveversion >/dev/null || fail "This installer must be run on a Proxmox host."
command -v pct       >/dev/null || fail "pct not found — needs Proxmox 7+."
command -v pveum     >/dev/null || fail "pveum not found."

bold "Hangar installer"
info "Proxmox: $(pveversion -v 2>/dev/null | head -1)"

# Pick a free CTID
if [[ -z "$CTID" ]]; then
  CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo "")
fi
[[ -z "$CTID" ]] && fail "Couldn't pick a free CTID. Set CTID=NNN and retry."
info "Container ID: $CTID"

# Make sure the Debian template is available locally
TEMPLATE_NAME=$(pveam available -section system 2>/dev/null \
  | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
[[ -z "$TEMPLATE_NAME" ]] && fail "No Debian 12 standard template listed by pveam."

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' | grep -q "$TEMPLATE_NAME"; then
  info "Downloading template $TEMPLATE_NAME to $TEMPLATE_STORAGE…"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_NAME"
fi
TEMPLATE_REF="$TEMPLATE_STORAGE:vztmpl/$TEMPLATE_NAME"

# Proxmox API user + token (idempotent)
info "Ensuring hangar@pam user + token exist…"
pveum user add hangar@pam --comment "Hangar API user" 2>/dev/null || true
pveum acl modify / -user hangar@pam -role Administrator >/dev/null

TOKEN_JSON=$(pveum user token add hangar@pam hangar-token --privsep 0 --output-format json 2>/dev/null || true)
if [[ -z "$TOKEN_JSON" ]]; then
  warn "Token 'hangar-token' already exists — rotating."
  pveum user token remove hangar@pam hangar-token >/dev/null
  TOKEN_JSON=$(pveum user token add hangar@pam hangar-token --privsep 0 --output-format json)
fi
TOKEN_SECRET=$(printf '%s' "$TOKEN_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['value'])")
[[ -z "$TOKEN_SECRET" ]] && fail "Couldn't extract token secret from pveum output."
done_ "Token created"

# Create the LXC
if pct status "$CTID" >/dev/null 2>&1; then
  fail "CTID $CTID already exists. Set CTID=NNN and retry."
fi
info "Creating LXC $CTID ($HOSTNAME) on $STORAGE…"
pct create "$CTID" "$TEMPLATE_REF" \
  --hostname "$HOSTNAME" \
  --memory "$MEMORY" \
  --cores "$CORES" \
  --net0 "name=eth0,bridge=$NET_BRIDGE,ip=dhcp" \
  --storage "$STORAGE" \
  --rootfs "$STORAGE:2" \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --start 1 >/dev/null

# Wait for network
info "Waiting for container network…"
for i in $(seq 1 30); do
  if pct exec "$CTID" -- bash -c "getent hosts deb.debian.org >/dev/null 2>&1"; then break; fi
  sleep 1
done

# Detect the Proxmox host's own API URL (from the container's perspective)
NODE_IP=$(hostname -I | awk '{print $1}')
PROXMOX_URL="https://${NODE_IP}:8006"
NODE_NAME=$(hostname -s)

# Provision Hangar inside the LXC
info "Installing Hangar…"
pct exec "$CTID" -- bash -lc "
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl ca-certificates >/dev/null

git clone --depth 1 '$REPO' /opt/hangar
python3 -m venv /opt/hangar/.venv
/opt/hangar/.venv/bin/pip install --no-cache-dir -q -r /opt/hangar/requirements.txt

install -d -m 750 /etc/hangar
cat > /etc/hangar/hangar.yaml <<EOF
theme:
  accent: \"$ACCENT\"
nodes:
  - name: $NODE_NAME
    url: $PROXMOX_URL
    token_id: hangar@pam!hangar-token
    token_secret: $TOKEN_SECRET
    verify_ssl: false
EOF
chmod 600 /etc/hangar/hangar.yaml

cat > /etc/systemd/system/hangar.service <<'EOF'
[Unit]
Description=Hangar — mobile control panel for Proxmox
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/hangar
Environment=HANGAR_CONFIG=/etc/hangar/hangar.yaml
Environment=PORT=8080
ExecStart=/opt/hangar/.venv/bin/python /opt/hangar/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now hangar >/dev/null
"

# Wait for Hangar to come up
info "Waiting for Hangar to start…"
LXC_IP=""
for i in $(seq 1 30); do
  LXC_IP=$(pct exec "$CTID" -- bash -c "ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1" 2>/dev/null || true)
  if [[ -n "$LXC_IP" ]] && curl -fsS "http://$LXC_IP:8080/" -o /dev/null --max-time 2 2>/dev/null; then
    break
  fi
  sleep 1
done

[[ -z "$LXC_IP" ]] && fail "Container has no IP yet — check DHCP on $NET_BRIDGE."

echo
done_ "Hangar is running"
echo
bold "  →  http://$LXC_IP:8080"
echo
echo "  Add it to your phone's home screen for a fullscreen PWA."
echo "  Config:  pct exec $CTID -- nano /etc/hangar/hangar.yaml"
echo "  Logs:    pct exec $CTID -- journalctl -u hangar -f"
echo "  Remove:  pct stop $CTID && pct destroy $CTID && pveum user token remove hangar@pam hangar-token"
echo
