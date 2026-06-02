#!/usr/bin/env bash
# screen-stream launcher
# Run this once. After that your tablet can open the URL shown below.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
CYN='\033[0;36m'
NC='\033[0m'

echo -e "${CYN}Screen Stream${NC} — starting up...\n"

# ── 1. ffmpeg ────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo -e "${RED}ERROR${NC}: ffmpeg not found."
  echo "Install it with:  sudo apt install ffmpeg"
  exit 1
fi
echo -e "  ${GRN}✓${NC} ffmpeg found"

# ── 2. Python venv + packages ────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"

if [ ! -x "$PYTHON" ]; then
  if [ -w "$SCRIPT_DIR" ]; then
    # Dev mode — create venv in place
    echo -e "  ${YLW}→${NC} Creating local venv (.venv)..."
    python3 -m venv "$VENV_DIR"
  else
    # Packaged install — postinst should have created the venv already
    echo -e "${RED}ERROR${NC}: venv not found at $VENV_DIR"
    echo "  Try: sudo apt install --reinstall screen-share-tab"
    exit 1
  fi
fi

# Only attempt pip installs if the venv directory is writable (dev mode)
if [ -w "$VENV_DIR" ]; then
  PKGS=()
  "$PYTHON" -c "import websockets" &>/dev/null 2>&1 || PKGS+=(websockets)
  "$PYTHON" -c "import qrcode"     &>/dev/null 2>&1 || PKGS+=(qrcode)
  "$PYTHON" -c "import zeroconf"   &>/dev/null 2>&1 || PKGS+=(zeroconf)
  if [ ${#PKGS[@]} -gt 0 ]; then
    echo -e "  ${YLW}→${NC} Installing: ${PKGS[*]}"
    "$VENV_DIR/bin/pip" install --quiet "${PKGS[@]}"
  fi
fi
echo -e "  ${GRN}✓${NC} Python packages ready"

# ── 3. mpegts.js ─────────────────────────────────────────────────────────────
MPEGTS_JS="$SCRIPT_DIR/mpegts.min.js"
MPEGTS_URL="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.min.js"

if [ ! -f "$MPEGTS_JS" ]; then
  if [ -w "$SCRIPT_DIR" ]; then
    echo -e "  ${YLW}→${NC} Downloading mpegts.js (one-time)..."
    if command -v curl &>/dev/null; then
      curl -sL "$MPEGTS_URL" -o "$MPEGTS_JS"
    elif command -v wget &>/dev/null; then
      wget -q "$MPEGTS_URL" -O "$MPEGTS_JS"
    else
      echo -e "${RED}ERROR${NC}: need curl or wget to download mpegts.js"
      exit 1
    fi
  else
    echo -e "${RED}ERROR${NC}: mpegts.js not found at $MPEGTS_JS"
    echo "  Try: sudo apt install --reinstall screen-share-tab"
    exit 1
  fi
fi
echo -e "  ${GRN}✓${NC} mpegts.js ready"

# ── 4. xdotool (optional — enables cursor highlight feature) ─────────────────
if ! command -v xdotool &>/dev/null; then
  echo -e "  ${YLW}!${NC} xdotool not found — cursor highlight disabled"
  echo -e "       Install with:  sudo apt install xdotool"
else
  echo -e "  ${GRN}✓${NC} xdotool found (cursor highlight available)"
fi

# ── 5. DISPLAY env var ───────────────────────────────────────────────────────
if [ -z "${DISPLAY:-}" ]; then
  # Try to detect the active X display rather than blindly assuming :0
  for _d in :0 :1 :2; do
    if DISPLAY="$_d" xdpyinfo &>/dev/null 2>&1; then
      export DISPLAY="$_d"
      break
    fi
  done
  export DISPLAY="${DISPLAY:-:0}"
  echo -e "  ${YLW}!${NC} DISPLAY not set — auto-detected ${DISPLAY}"
else
  echo -e "  ${GRN}✓${NC} DISPLAY=${DISPLAY}"
fi

echo ""

# ── 6. Kill any previous instance still holding the ports ────────────────────
pkill -f "screen-share-tab/server.py\|screen-stream/server.py" 2>/dev/null || true

# Also kill anything still holding ports 8765/8766
for _port in 8765 8766; do
  _pid=$(lsof -ti tcp:"$_port" 2>/dev/null) && [ -n "$_pid" ] && kill "$_pid" 2>/dev/null || true
done

# Wait until both ports are confirmed free (up to 5 s)
_waited=0
while lsof -ti tcp:8765 &>/dev/null || lsof -ti tcp:8766 &>/dev/null; do
  if [ $_waited -ge 5 ]; then
    echo -e "  ${RED}!${NC} Ports still busy after 5 s — trying SIGKILL..."
    for _port in 8765 8766; do
      _pid=$(lsof -ti tcp:"$_port" 2>/dev/null) && [ -n "$_pid" ] && kill -9 "$_pid" 2>/dev/null || true
    done
    sleep 1
    break
  fi
  echo -e "  ${YLW}→${NC} Waiting for ports to free... (${_waited}s)"
  sleep 1
  _waited=$((_waited + 1))
done

# ── 7. Launch ────────────────────────────────────────────────────────────────
exec "$PYTHON" server.py
