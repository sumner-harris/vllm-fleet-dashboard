#!/usr/bin/env bash
# Install the fleet agent on one node. Run ON the node, as root:
#   sudo bash install-agent.sh [PORT] [TOKEN]
set -euo pipefail

PORT="${1:-9900}"
TOKEN="${2:-}"
DEST=/opt/vllm-fleet-agent
HERE="$(cd "$(dirname "$0")" && pwd)"

install -d "$DEST"
install -m 0755 "$HERE/spark_agent.py" "$DEST/spark_agent.py"

UNIT=/etc/systemd/system/vllm-fleet-agent.service
sed "s|--port 9900|--port ${PORT}|" "$HERE/vllm-fleet-agent.service" > "$UNIT"
if [ -n "$TOKEN" ]; then
  sed -i "s|# Environment=FLEET_AGENT_TOKEN=change-me|Environment=FLEET_AGENT_TOKEN=${TOKEN}|" "$UNIT"
fi

systemctl daemon-reload
systemctl enable --now vllm-fleet-agent
sleep 1
systemctl --no-pager --lines=5 status vllm-fleet-agent || true

echo
echo "Agent listening on port ${PORT}. Quick check:"
echo "  curl -s localhost:${PORT}/health"
echo "Remember to allow the port from the dashboard host, e.g.:"
echo "  sudo ufw allow from <DASHBOARD_IP> to any port ${PORT} proto tcp"
