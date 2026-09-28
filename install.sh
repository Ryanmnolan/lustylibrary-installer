#!/bin/bash
set -euo pipefail

REPO_URL="https://github.com/ryanmnolan/lustylibrary-installer.git"
INSTALL_DIR="/opt/lustylibrary-installer"
LOG_FILE="/var/log/lustylibrary-install.log"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
  echo "$(timestamp) $*" | tee -a "$LOG_FILE"
}

step() {
  echo | tee -a "$LOG_FILE"
  echo "==> $*" | tee -a "$LOG_FILE"
}

# Echoes a command before running it, streams its output to the terminal AND
# the log file, and exits with a clear message if it fails.
run() {
  log "\$ $*"
  if ! "$@" 2>&1 | tee -a "$LOG_FILE"; then
    echo
    echo "!! Command failed: $*"
    echo "!! Full log: $LOG_FILE"
    exit 1
  fi
}

require_root() {
  if [[ "$EUID" -ne 0 ]]; then
    echo "This installer must be run as root, e.g.:" >&2
    echo "  curl -sSL https://raw.githubusercontent.com/ryanmnolan/lustylibrary-installer/main/install.sh | sudo bash" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# board / OS detection
# ---------------------------------------------------------------------------
detect_pi_model() {
  if [[ -f /proc/device-tree/model ]]; then
    tr -d '\0' < /proc/device-tree/model
  else
    echo "unknown"
  fi
}

detect_os_codename() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "${VERSION_CODENAME:-unknown}"
  else
    echo "unknown"
  fi
}

detect_arch() {
  uname -m
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
require_root
mkdir -p "$(dirname "$LOG_FILE")"
: > "$LOG_FILE"

PI_MODEL="$(detect_pi_model)"
OS_CODENAME="$(detect_os_codename)"
ARCH="$(detect_arch)"

step "Lusty Library Installer"
log "Board:  $PI_MODEL"
log "OS:     $OS_CODENAME"
log "Arch:   $ARCH"

case "$PI_MODEL" in
  *"Raspberry Pi 3"*|*"Raspberry Pi 4"*|*"Raspberry Pi 5"*)
    log "Supported Raspberry Pi model detected."
    ;;
  *)
    log "WARNING: this doesn't look like a Raspberry Pi 3, 4 or 5 (got: '$PI_MODEL')."
    log "Continuing anyway, but the Wi-Fi hotspot step may not work on this board."
    ;;
esac

step "Installing base packages (git, python3, pip, curl)"
run apt-get update
run apt-get install -y git python3 python3-pip python3-venv curl

step "Fetching Lusty Library Installer into $INSTALL_DIR"
if [[ ! -d "$INSTALL_DIR" ]]; then
  run git clone "$REPO_URL" "$INSTALL_DIR"
else
  log "Existing install found, updating..."
  cd "$INSTALL_DIR"
  run git pull --ff-only
fi

cd "$INSTALL_DIR"

step "Installing Python dependencies"
if ! pip3 install --break-system-packages -r requirements.txt 2>&1 | tee -a "$LOG_FILE"; then
  log "First attempt failed (often a distro-packaged library, e.g. python3-cryptography," \
      "with no pip RECORD file that pip won't touch) — retrying with --ignore-installed."
  run pip3 install --break-system-packages --ignore-installed -r requirements.txt
fi

step "Installing Docker Engine (if needed)"
if command -v docker >/dev/null 2>&1; then
  log "Docker already installed: $(docker --version)"
else
  log "Docker not found. Installing via get.docker.com (supports Pi 3/4/5, arm64 and armhf)."
  run curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  run sh /tmp/get-docker.sh
  if [[ -n "${SUDO_USER:-}" ]]; then
    run usermod -aG docker "$SUDO_USER"
    log "Added $SUDO_USER to the docker group (log out/in, or reboot, for it to take effect)."
  fi
fi

step "Recording board/OS info for the setup wizard"
cat > "$INSTALL_DIR/board_info.json" <<EOF
{
  "model": "$PI_MODEL",
  "os_codename": "$OS_CODENAME",
  "arch": "$ARCH"
}
EOF

step "Installing systemd service for the setup wizard"
run cp lustylibrary-setup.service /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable --now lustylibrary-setup.service

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo
echo "======================================================"
echo " Lusty Library Setup GUI is now running."
echo
echo " Open this in a browser on the same network:"
echo "   http://${IP_ADDR:-<pi-ip>}:9000/setup"
echo
echo " Full install log: $LOG_FILE"
echo " Manage the service with:"
echo "   sudo systemctl status lustylibrary-setup.service"
echo "======================================================"
