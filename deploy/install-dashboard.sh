#!/usr/bin/env bash
# Install the dashboard as a system service on one always-on host.
#   sudo bash deploy/install-dashboard.sh [PORT]
# Run it from the root of this project (the folder containing server/ and config.yaml).
set -euo pipefail

PORT="${1:-8080}"
DEST=/opt/vllm-fleet-dashboard
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$SRC/config.yaml" ] || {
  echo "config.yaml not found in $SRC — copy config.example.yaml to config.yaml and fill in your machines first." >&2
  exit 1
}

id -u fleetdash >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin fleetdash

install -d "$DEST"
cp -r "$SRC/server" "$DEST/"
install -d "$DEST/agent"
install -m 0644 "$SRC/agent/spark_agent.py" "$DEST/agent/spark_agent.py"
install -m 0640 "$SRC/config.yaml" "$DEST/config.yaml"
install -m 0644 "$SRC/requirements.txt" "$DEST/requirements.txt"

python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

# One admin token per install, kept out of the config file others can read.
TOKEN_FILE=/etc/vllm-fleet-dashboard.token
if [ ! -f "$TOKEN_FILE" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

UNIT=/etc/systemd/system/vllm-fleet-dashboard.service
sed -e "s|--port 8080|--port ${PORT}|" \
    -e "s|# Environment=FLEET_ADMIN_TOKEN=change-me|Environment=FLEET_ADMIN_TOKEN=${TOKEN}|" \
    "$SRC/deploy/vllm-fleet-dashboard.service" > "$UNIT"
chmod 640 "$UNIT"

chown -R fleetdash:fleetdash "$DEST"
systemctl daemon-reload
systemctl enable --now vllm-fleet-dashboard
sleep 2
systemctl --no-pager --lines=8 status vllm-fleet-dashboard || true

HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<TXT

────────────────────────────────────────────────────────────
Dashboard is running.

  Share this with colleagues:   http://$(hostname -f 2>/dev/null || echo "$HOSTIP"):${PORT}/
  Your admin link (keep private): http://$(hostname -f 2>/dev/null || echo "$HOSTIP"):${PORT}/?admin=${TOKEN}

Open the admin link once in your browser — the token is remembered locally and
the Refresh control appears. Everyone else gets the same read-only page.

Allow the port from the lab network, e.g.:
  sudo ufw allow from 192.168.0.0/16 to any port ${PORT} proto tcp

Logs:    journalctl -u vllm-fleet-dashboard -f
Restart: sudo systemctl restart vllm-fleet-dashboard   (after editing ${DEST}/config.yaml)
────────────────────────────────────────────────────────────
TXT
