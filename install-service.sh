#!/usr/bin/env bash
# Install screen-stream as a systemd user service.
# After running this, the server starts automatically on login —
# no terminal needed. Just open the tablet and it's already up.
#
# Usage:
#   ./install-service.sh          — install & enable
#   ./install-service.sh remove   — uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/screen-stream.service"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'; NC='\033[0m'

# ── Remove ────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "remove" ]; then
  echo -e "${CYN}Removing screen-stream service...${NC}"
  systemctl --user stop    screen-stream 2>/dev/null || true
  systemctl --user disable screen-stream 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl --user daemon-reload
  echo -e "  ${GRN}✓${NC} Service removed"
  exit 0
fi

# ── Install ───────────────────────────────────────────────────────────────────
echo -e "${CYN}Installing screen-stream systemd user service...${NC}\n"

# Detect current DISPLAY (save it for the service)
DISPLAY_VAL="${DISPLAY:-:1}"
echo -e "  ${GRN}✓${NC} Using DISPLAY=${DISPLAY_VAL}"

# Detect XAUTHORITY
XAUTH_VAL="${XAUTHORITY:-$HOME/.Xauthority}"
echo -e "  ${GRN}✓${NC} Using XAUTHORITY=${XAUTH_VAL}"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Screen Stream (tablet monitor streaming)
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${SCRIPT_DIR}/start.sh
Restart=on-failure
RestartSec=5

Environment=DISPLAY=${DISPLAY_VAL}
Environment=XAUTHORITY=${XAUTH_VAL}

# Prevent systemd from eating stdout (so logs work)
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

echo -e "  ${GRN}✓${NC} Service file written → $SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user enable screen-stream
systemctl --user start  screen-stream

echo ""
echo -e "  ${GRN}✓${NC} Service enabled and started"
echo ""
echo -e "  ${CYN}Useful commands:${NC}"
echo -e "    systemctl --user status  screen-stream   # check status"
echo -e "    systemctl --user logs -f screen-stream   # live logs"
echo -e "    systemctl --user restart screen-stream   # restart"
echo -e "    ./install-service.sh remove              # uninstall"
echo ""
echo -e "  ${YLW}Note:${NC} If your DISPLAY number changes after a reboot, re-run this script."
echo -e "  ${YLW}Note:${NC} Run 'loginctl enable-linger \$USER' to start on boot before login."
