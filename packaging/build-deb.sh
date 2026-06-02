#!/usr/bin/env bash
# Build screen-share-tab_X.X.X_amd64.deb
# Usage: ./packaging/build-deb.sh [version]
# Output: screen-share-tab_X.X.X_amd64.deb in the repo root
set -euo pipefail

PACKAGE="screen-share-tab"
VERSION="${1:-1.0.0}"
ARCH="amd64"
DEB="${PACKAGE}_${VERSION}_${ARCH}.deb"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)/${PACKAGE}_${VERSION}_${ARCH}"

RED='\033[0;31m'; GRN='\033[0;32m'; CYN='\033[0;36m'; NC='\033[0m'

echo -e "${CYN}Building ${DEB}...${NC}\n"

# ── Check dpkg-deb is available ───────────────────────────────────────────────
if ! command -v dpkg-deb &>/dev/null; then
    echo -e "${RED}ERROR${NC}: dpkg-deb not found. Install with: sudo apt install dpkg"
    exit 1
fi

# ── Staging directory structure ───────────────────────────────────────────────
mkdir -p "$STAGE/DEBIAN"
mkdir -p "$STAGE/opt/screen-share-tab"
mkdir -p "$STAGE/usr/local/bin"

# ── Copy app files ────────────────────────────────────────────────────────────
for f in server.py index.html start.sh install-service.sh; do
    cp "$REPO/$f" "$STAGE/opt/screen-share-tab/"
done
chmod +x "$STAGE/opt/screen-share-tab/start.sh"
chmod +x "$STAGE/opt/screen-share-tab/install-service.sh"

# ── The 'screen-share-tab' command ───────────────────────────────────────────
cat > "$STAGE/usr/local/bin/$PACKAGE" << 'EOF'
#!/usr/bin/env bash
exec /opt/screen-share-tab/start.sh
EOF
chmod 755 "$STAGE/usr/local/bin/$PACKAGE"

# ── DEBIAN control files ──────────────────────────────────────────────────────
for f in control postinst prerm postrm; do
    cp "$REPO/packaging/DEBIAN/$f" "$STAGE/DEBIAN/"
done
# Inject correct version
sed -i "s/^Version:.*/Version: $VERSION/" "$STAGE/DEBIAN/control"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

# ── Build ─────────────────────────────────────────────────────────────────────
dpkg-deb --build --root-owner-group "$STAGE" "$REPO/$DEB"
rm -rf "$(dirname "$STAGE")"

echo -e "\n${GRN}Built: $REPO/$DEB${NC}\n"
echo -e "Share this file with your team. They install it with:\n"
echo -e "  sudo apt install ./$DEB\n"
echo -e "Then from any terminal:\n"
echo -e "  screen-share-tab\n"
echo -e "To uninstall:\n"
echo -e "  sudo apt remove screen-share-tab\n"
