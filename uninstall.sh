#!/bin/bash
# Lusty Library Installer — uninstaller.
#
# Removes everything install.sh / the setup wizard created, so you can test
# a fresh install again without re-flashing the SD card. By default this
# leaves your actual library data (books/audiobooks) alone — pass
# --purge-data to also wipe those.
#
# Usage:
#   sudo bash uninstall.sh                 # remove the install, keep library data
#   sudo bash uninstall.sh --purge-data    # remove the install AND all books/audiobooks/config
set -uo pipefail

INSTALL_DIR="/opt/lustylibrary-installer"
COMPOSE_DIR="/home/pi/library-server"
LOG_FILE="/var/log/lustylibrary-install.log"
SYNC_LOG_FILE="/var/log/sync_from_server.log"
PURGE_DATA=0

for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "This must be run as root, e.g.: sudo bash uninstall.sh" >&2
  exit 1
fi

step() { echo; echo "==> $*"; }
note() { echo "    $*"; }

# ---------------------------------------------------------------------------
# 1. Stop and remove every systemd service the wizard installs
# ---------------------------------------------------------------------------
step "Stopping and removing systemd services"
SERVICES=(
  lustylibrary-setup.service
  lustylibrary-requests.service
  lustylibrary-sync.service
  lustylibrary-leds.service
  lustylibrary-shutdown-button.service
)
for svc in "${SERVICES[@]}"; do
  if systemctl list-unit-files "$svc" >/dev/null 2>&1; then
    systemctl disable --now "$svc" >/dev/null 2>&1
    note "Stopped/disabled $svc"
  fi
  rm -f "/etc/systemd/system/$svc"
done
systemctl daemon-reload

# ---------------------------------------------------------------------------
# 2. Bring down the Docker stack (Audiobookshelf / Calibre-Web containers)
# ---------------------------------------------------------------------------
step "Bringing down Docker containers"
if [[ -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
  if command -v docker >/dev/null 2>&1; then
    (cd "$COMPOSE_DIR" && docker compose down 2>/dev/null || docker-compose down 2>/dev/null)
    note "Containers stopped and removed (images/library data left alone)."
  fi
  rm -rf "$COMPOSE_DIR"
else
  note "No docker-compose.yml found at $COMPOSE_DIR, skipping."
fi

# ---------------------------------------------------------------------------
# 3. Remove the Wi-Fi hotspot connection
# ---------------------------------------------------------------------------
step "Removing the Wi-Fi hotspot connection"
if command -v nmcli >/dev/null 2>&1; then
  if nmcli -t -f NAME connection show | grep -qx "LustyLibraryAP"; then
    nmcli connection delete LustyLibraryAP >/dev/null 2>&1
    note "Deleted the NetworkManager 'LustyLibraryAP' connection."
  else
    note "No 'LustyLibraryAP' NetworkManager connection found."
  fi
fi
if systemctl list-unit-files hostapd.service >/dev/null 2>&1 && systemctl is-enabled hostapd >/dev/null 2>&1; then
  systemctl disable --now hostapd >/dev/null 2>&1
  note "Disabled hostapd. If you used the older dhcpcd network stack, you'll"
  note "want to hand-remove the 'interface wlan0' block this wizard added to"
  note "/etc/dhcpcd.conf and /etc/hostapd/hostapd.conf — left alone here since"
  note "those files may have other content you don't want touched automatically."
fi

# ---------------------------------------------------------------------------
# 4. Remove the sync trigger hook and its credentials
# ---------------------------------------------------------------------------
step "Removing the auto-sync trigger"
rm -f /etc/NetworkManager/dispatcher.d/99-lustylibrary-sync
rm -f /etc/udev/rules.d/99-lustylibrary-sync.rules
rm -f /etc/lustylibrary-sync-credentials
udevadm control --reload-rules >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 5. Remove generated scripts
# ---------------------------------------------------------------------------
step "Removing generated scripts"
rm -f /usr/local/bin/library_requests.py
rm -f /usr/local/bin/status_leds.py
rm -f /usr/local/bin/shutdown_button.py

# ---------------------------------------------------------------------------
# 6. Remove the installer itself (config.yml, qr_cache.json, the repo clone)
# ---------------------------------------------------------------------------
step "Removing $INSTALL_DIR"
MEDIA_ROOT=""
if [[ -f "$INSTALL_DIR/config.yml" ]] && command -v python3 >/dev/null 2>&1; then
  MEDIA_ROOT="$(python3 -c "
import yaml
try:
    with open('$INSTALL_DIR/config.yml') as f:
        print(yaml.safe_load(f).get('storage', {}).get('media_root', ''))
except Exception:
    pass
" 2>/dev/null)"
fi
rm -rf "$INSTALL_DIR"
rm -f "$LOG_FILE" "$SYNC_LOG_FILE"

# ---------------------------------------------------------------------------
# 7. Optionally wipe library data (books/audiobooks/config/welcome page)
# ---------------------------------------------------------------------------
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/media}"
step "Library data at $MEDIA_ROOT"
if [[ "$PURGE_DATA" -eq 1 ]]; then
  if [[ -d "$MEDIA_ROOT" ]]; then
    rm -rf "${MEDIA_ROOT:?}/books" "${MEDIA_ROOT:?}/audiobooks" "${MEDIA_ROOT:?}/config"
    rm -f "$MEDIA_ROOT/welcome.html" "$MEDIA_ROOT/welcome.pdf" "$MEDIA_ROOT/requests.csv"
    note "Wiped books/audiobooks/config/welcome page/requests under $MEDIA_ROOT."
  else
    note "$MEDIA_ROOT doesn't exist, nothing to purge."
  fi
else
  note "Left as-is (pass --purge-data to also wipe this)."
fi

echo
echo "======================================================"
echo " Lusty Library uninstalled."
echo " Re-install with:"
echo "   curl -sSL https://raw.githubusercontent.com/ryanmnolan/lustylibrary-installer/<branch>/install.sh | sudo bash"
echo "======================================================"
