#!/bin/bash
# Lusty Library Installer — uninstaller.
#
# Removes what install.sh / the setup wizard created, so you can test a fresh
# install again without re-flashing the SD card.
#
# Usage:
#   sudo bash uninstall.sh                 # remove the install, KEEP books/audiobooks and Docker
#   sudo bash uninstall.sh --purge-data    # same, but also wipe books/audiobooks/config/welcome page
#   sudo bash uninstall.sh --full          # remove EVERYTHING the installer added: all of the above,
#                                          #   the library data, the Docker images AND Docker Engine,
#                                          #   hostapd/dnsmasq/cifs-utils/GPIO packages, pip packages,
#                                          #   the USB-drive mount, and the hotspot config
#   sudo bash uninstall.sh --full --yes    # same, without the "are you sure" prompt
#
# What --full deliberately does NOT touch (the OS itself relies on them):
#   git, python3, pip, curl, and the pip copies of requests / pyyaml / Pillow.
#   It also can't "un-format" a USB drive the wizard formatted — the drive is
#   left as an empty ext4 disk (its contents are wiped).
#
# Everything lives inside main() so bash reads the whole script before it
# starts deleting files (including this one, from /opt/lustylibrary-installer).
set -uo pipefail

INSTALL_DIR="/opt/lustylibrary-installer"
COMPOSE_DIR="/home/pi/library-server"
LOG_FILE="/var/log/lustylibrary-install.log"
SYNC_LOG_FILE="/var/log/sync_from_server.log"
PURGE_DATA=0
FULL=0
ASSUME_YES=0

step() { echo; echo "==> $*"; }
note() { echo "    $*"; }

# Purges only the packages from the given list that are actually installed.
purge_pkgs() {
  local installed=()
  for p in "$@"; do
    if dpkg -s "$p" >/dev/null 2>&1; then installed+=("$p"); fi
  done
  if [[ "${#installed[@]}" -gt 0 ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${installed[@]}" >/dev/null 2>&1 \
      && note "Removed: ${installed[*]}" \
      || note "Couldn't remove some of: ${installed[*]} (try 'sudo apt-get purge ${installed[*]}' by hand)."
  fi
}

main() {
  for arg in "$@"; do
    case "$arg" in
      --purge-data) PURGE_DATA=1 ;;
      --full) FULL=1; PURGE_DATA=1 ;;
      -y|--yes) ASSUME_YES=1 ;;
      *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
  done

  if [[ "$EUID" -ne 0 ]]; then
    echo "This must be run as root, e.g.: sudo bash uninstall.sh" >&2
    exit 1
  fi

  if [[ "$FULL" -eq 1 && "$ASSUME_YES" -ne 1 ]]; then
    echo "--full will remove EVERYTHING the Lusty Library installer added, including:"
    echo "  - all books, audiobooks and library config"
    echo "  - Docker Engine, its images, containers and volumes (ALL of Docker, not just Lusty Library's)"
    echo "  - hostapd, dnsmasq, cifs-utils and the GPIO/QR Python packages"
    echo
    answer=""
    # /dev/tty (not stdin) so this still works when piped from curl | sudo bash.
    if ! read -r -p "Type 'yes' to continue: " answer < /dev/tty 2>/dev/null; then
      echo "No terminal available to confirm — re-run with --yes to skip this prompt." >&2
      exit 1
    fi
    if [[ "$answer" != "yes" ]]; then
      echo "Cancelled, nothing was changed."
      exit 0
    fi
  fi

  # Work out where the library lives BEFORE we delete config.yml.
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
  MEDIA_ROOT="${MEDIA_ROOT:-/mnt/media}"

  # -------------------------------------------------------------------------
  # 1. Stop and remove every systemd service the wizard installs
  # -------------------------------------------------------------------------
  step "Stopping and removing systemd services"
  SERVICES=(
    lustylibrary-setup.service
    lustylibrary-requests.service
    lustylibrary-sync.service
    lustylibrary-leds.service
    lustylibrary-shutdown-button.service
  )
  for svc in "${SERVICES[@]}"; do
    if [[ -f "/etc/systemd/system/$svc" ]]; then
      systemctl disable --now "$svc" >/dev/null 2>&1
      note "Stopped/disabled $svc"
    fi
    rm -f "/etc/systemd/system/$svc"
  done
  systemctl daemon-reload

  # -------------------------------------------------------------------------
  # 2. Bring down the Docker stack (Audiobookshelf / Calibre-Web containers)
  # -------------------------------------------------------------------------
  step "Bringing down Docker containers"
  if command -v docker >/dev/null 2>&1; then
    if [[ -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
      (cd "$COMPOSE_DIR" && docker compose down 2>/dev/null || docker-compose down 2>/dev/null)
    fi
    # Belt and braces in case the compose file is already gone.
    docker rm -f audiobookshelf calibre-web >/dev/null 2>&1
    note "Containers stopped and removed."
    if [[ "$FULL" -eq 1 ]]; then
      docker rmi ghcr.io/advplyr/audiobookshelf:latest lscr.io/linuxserver/calibre-web:latest >/dev/null 2>&1
      note "Removed the Audiobookshelf and Calibre-Web images."
    else
      note "Images left alone (use --full to remove them)."
    fi
  else
    note "Docker isn't installed, nothing to stop."
  fi
  rm -rf "$COMPOSE_DIR"

  # -------------------------------------------------------------------------
  # 3. Remove the Wi-Fi hotspot configuration
  # -------------------------------------------------------------------------
  step "Removing the Wi-Fi hotspot configuration"
  if command -v nmcli >/dev/null 2>&1; then
    if nmcli -t -f NAME connection show | grep -qx "LustyLibraryAP"; then
      nmcli connection delete LustyLibraryAP >/dev/null 2>&1
      note "Deleted the NetworkManager 'LustyLibraryAP' connection."
    else
      note "No 'LustyLibraryAP' NetworkManager connection found."
    fi
  fi
  if systemctl is-enabled hostapd >/dev/null 2>&1 || systemctl is-active hostapd >/dev/null 2>&1; then
    systemctl disable --now hostapd >/dev/null 2>&1
    note "Disabled hostapd."
  fi
  if [[ "$FULL" -eq 1 ]]; then
    # Older dhcpcd/hostapd network stack: undo the files the wizard wrote.
    rm -f /etc/hostapd/hostapd.conf
    if [[ -f /etc/dhcpcd.conf ]]; then
      python3 - <<'PYEOF'
from pathlib import Path
p = Path("/etc/dhcpcd.conf")
lines = p.read_text().splitlines()
out, skip, i = [], False, 0
# Drop only the "interface wlan0" block this wizard added (the one that sets
# a static ip_address) — leave any other interface blocks alone.
while i < len(lines):
    line = lines[i]
    if line.startswith("interface wlan0"):
        j = i + 1
        block = [line]
        while j < len(lines) and not lines[j].startswith("interface "):
            block.append(lines[j])
            j += 1
        if any("static ip_address" in b for b in block):
            i = j
            continue
    out.append(line)
    i += 1
p.write_text("\n".join(out).rstrip("\n") + "\n")
PYEOF
      systemctl restart dhcpcd >/dev/null 2>&1 || true
      note "Removed the wlan0 static-IP block from /etc/dhcpcd.conf."
    fi
  else
    note "Left any older-stack /etc/dhcpcd.conf and /etc/hostapd/hostapd.conf edits alone (--full removes them)."
  fi

  # -------------------------------------------------------------------------
  # 4. Remove the sync trigger hook, script and credentials
  # -------------------------------------------------------------------------
  step "Removing the auto-sync trigger"
  if mountpoint -q /mnt/remote_data 2>/dev/null; then
    umount /mnt/remote_data >/dev/null 2>&1 && note "Unmounted /mnt/remote_data."
  fi
  rm -f /etc/NetworkManager/dispatcher.d/99-lustylibrary-sync
  rm -f /etc/udev/rules.d/99-lustylibrary-sync.rules
  rm -f /etc/lustylibrary-sync-credentials
  rm -f /usr/local/bin/sync_from_server.sh
  rm -f /run/sync_from_server.lock
  rmdir /mnt/remote_data >/dev/null 2>&1 || true
  udevadm control --reload-rules >/dev/null 2>&1 || true

  # -------------------------------------------------------------------------
  # 5. Remove generated scripts
  # -------------------------------------------------------------------------
  step "Removing generated scripts"
  rm -f /usr/local/bin/library_requests.py
  rm -f /usr/local/bin/status_leds.py
  rm -f /usr/local/bin/shutdown_button.py

  # -------------------------------------------------------------------------
  # 6. Library data (books/audiobooks/config/welcome page)
  # -------------------------------------------------------------------------
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
    note "Left as-is (pass --purge-data or --full to also wipe this)."
  fi

  # -------------------------------------------------------------------------
  # 7. (--full) Undo the USB-drive mount the wizard set up
  # -------------------------------------------------------------------------
  if [[ "$FULL" -eq 1 ]]; then
    step "Removing the storage mount"
    found_mount=0
    for unit in /etc/systemd/system/*.mount; do
      [[ -f "$unit" ]] || continue
      if grep -q "Lusty Library media" "$unit"; then
        name="$(basename "$unit")"
        systemctl disable --now "$name" >/dev/null 2>&1
        rm -f "$unit"
        note "Unmounted and removed $name (the drive keeps its now-empty ext4 filesystem)."
        found_mount=1
      fi
    done
    [[ "$found_mount" -eq 0 ]] && note "No Lusty Library mount unit found."
    rmdir "$MEDIA_ROOT" >/dev/null 2>&1 || true
    systemctl daemon-reload
  fi

  # -------------------------------------------------------------------------
  # 8. Remove the installer itself (config.yml, qr_cache.json, the repo clone)
  # -------------------------------------------------------------------------
  step "Removing $INSTALL_DIR"
  cd / || true
  rm -rf "$INSTALL_DIR"
  rm -f "$LOG_FILE" "$SYNC_LOG_FILE"

  # -------------------------------------------------------------------------
  # 9. (--full) Remove packages the installer added
  # -------------------------------------------------------------------------
  if [[ "$FULL" -eq 1 ]]; then
    step "Removing Python packages the installer added"
    for pkg in flask reportlab "qrcode" gpiozero rpi-lgpio; do
      pip3 uninstall -y -q --break-system-packages "$pkg" >/dev/null 2>&1 || pip3 uninstall -y -q "$pkg" >/dev/null 2>&1
    done
    note "Removed flask, reportlab, qrcode, gpiozero, rpi-lgpio (pip copies)."
    note "Left requests / pyyaml / Pillow alone — the OS can depend on those."

    step "Removing system packages the installer added"
    purge_pkgs hostapd dnsmasq cifs-utils python3-gpiozero python3-rpi-lgpio

    step "Removing Docker Engine"
    if command -v docker >/dev/null 2>&1; then
      systemctl disable --now docker.service docker.socket containerd.service >/dev/null 2>&1
      purge_pkgs docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
                 docker-ce-rootless-extras docker-compose docker.io
      rm -rf /var/lib/docker /var/lib/containerd
      rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.sources \
            /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg /usr/share/keyrings/docker-archive-keyring.gpg
      note "Docker Engine removed, along with its images, containers and volumes."
    else
      note "Docker isn't installed."
    fi
    DEBIAN_FRONTEND=noninteractive apt-get autoremove -y >/dev/null 2>&1 || true
    rm -f /tmp/get-docker.sh
  fi

  echo
  echo "======================================================"
  if [[ "$FULL" -eq 1 ]]; then
    echo " Lusty Library fully removed. Reboot recommended: sudo reboot"
  else
    echo " Lusty Library uninstalled."
  fi
  echo " Re-install with:"
  echo "   curl -sSL https://raw.githubusercontent.com/ryanmnolan/lustylibrary-installer/main/install.sh | sudo bash"
  echo "======================================================"
}

main "$@"
