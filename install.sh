#!/bin/bash
set -euo pipefail

REPO_URL="https://github.com/YOURUSER/lustylibrary-installer.git"
INSTALL_DIR="/opt/lustylibrary-installer"

echo "==> Installing Lusty Library Setup Wizard"

# Basic deps
apt update
apt install -y git python3 python3-pip

# Clone or update repo
if [ ! -d "$INSTALL_DIR" ]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  cd "$INSTALL_DIR"
  git pull --ff-only || true
fi

cd "$INSTALL_DIR"

# Install Python deps
pip3 install --break-system-packages -r requirements.txt || pip3 install -r requirements.txt

# Install systemd service
cp lustylibrary-setup.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lustylibrary-setup.service

echo
echo "==> Lusty Library Setup GUI is now running."
echo "Open:  http://<pi-ip>:9000/setup  in your browser."
echo
echo "You can manage the service with:"
echo "  sudo systemctl status lustylibrary-setup.service"
