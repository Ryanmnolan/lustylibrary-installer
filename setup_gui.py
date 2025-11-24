#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path

from flask import Flask, request, render_template_string, redirect, url_for
import yaml

app = Flask(__name__)

CONFIG_PATH = Path("/opt/lustylibrary-installer/config.yml")
DEFAULT_CONFIG = {
    "wifi": {
        "ssid": "LustyLibrary",
        "password": "lustybooks123",
        "ip": "10.10.10.10",
    },
    "storage": {
        "media_root": "/mnt/media",
    },
    "apps": {
        "install_audiobookshelf": True,
        "install_calibre_web": True,
    },
    "sync": {
        "enable_sync": False,
        "unraid_ip": "192.168.0.139",
        "share_path_audio": "/data/media/audiobook",
        "share_path_books": "/data/media/calibre",
    },
}

def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r") as f:
            return yaml.safe_load(f)
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f)

def generate_docker_compose(cfg):
    """Generate a docker-compose.yml based on selected options."""
    media_root = cfg["storage"]["media_root"]
    install_abs = cfg["apps"]["install_audiobookshelf"]
    install_cweb = cfg["apps"]["install_calibre_web"]

    lines = [
        'version: "3.8"',
        "",
        "services:",
    ]

    if install_abs:
        lines += [
            "  audiobookshelf:",
            "    image: ghcr.io/advplyr/audiobookshelf:latest",
            "    container_name: audiobookshelf",
            "    ports:",
            "      - \"13378:80\"",
            "    environment:",
            "      - TZ=America/Chicago",
            "      - AUDIOBOOKSHELF_DISABLE_UPDATES=true",
            "    volumes:",
            f"      - {media_root}/audiobooks:/audiobooks",
            f"      - {media_root}/config/audiobookshelf:/config",
            "    restart: unless-stopped",
            "",
        ]

    if install_cweb:
        lines += [
            "  calibre-web:",
            "    image: lscr.io/linuxserver/calibre-web:latest",
            "    container_name: calibre-web",
            "    ports:",
            "      - \"8083:8083\"",
            "    environment:",
            "      - PUID=1000",
            "      - PGID=1000",
            "      - TZ=America/Chicago",
            "    volumes:",
            f"      - {media_root}/books:/books",
            f"      - {media_root}/config/calibre:/config",
            "    restart: unless-stopped",
            "",
        ]

    # You can add the Request app here later, same pattern

    compose_path = Path("/home/pi/library-server")
    compose_path.mkdir(parents=True, exist_ok=True)
    with (compose_path / "docker-compose.yml").open("w") as f:
        f.write("\n".join(lines))

    # Ensure directories exist
    for sub in ("audiobooks", "books", "config"):
        Path(media_root, sub).mkdir(parents=True, exist_ok=True)

    return compose_path / "docker-compose.yml"

def apply_wifi_config(cfg):
    """
    Minimal example: update hostapd SSID/password and dhcpcd static IP.
    You can extend this to write RaspAP config files if you want.
    """
    ssid = cfg["wifi"]["ssid"]
    password = cfg["wifi"]["password"]
    ip = cfg["wifi"]["ip"]

    # Update hostapd.conf
    hostapd_conf = Path("/etc/hostapd/hostapd.conf")
    if hostapd_conf.exists():
        text = hostapd_conf.read_text()
        lines = []
        for line in text.splitlines():
            if line.startswith("ssid="):
                lines.append(f"ssid={ssid}")
            elif line.startswith("wpa_passphrase="):
                lines.append(f"wpa_passphrase={password}")
            else:
                lines.append(line)
        hostapd_conf.write_text("\n".join(lines))
        try:
            subprocess.run(["systemctl", "restart", "hostapd"], check=False)
        except Exception:
            pass

    # Update dhcpcd.conf static IP for wlan0
    dhcpcd = Path("/etc/dhcpcd.conf")
    block = (
        f"\ninterface wlan0\n"
        f"  static ip_address={ip}/24\n"
        f"  nohook wpa_supplicant\n"
    )
    if dhcpcd.exists():
        text = dhcpcd.read_text()
        # crude: remove old interface wlan0 block, append new
        new_lines = []
        skip = False
        for line in text.splitlines():
            if line.startswith("interface wlan0"):
                skip = True
                continue
            if skip and line.startswith("interface "):
                skip = False
            if not skip:
                new_lines.append(line)
        new_text = "\n".join(new_lines) + block
        dhcpcd.write_text(new_text)
        try:
            subprocess.run(["systemctl", "restart", "dhcpcd"], check=False)
        except Exception:
            pass

def apply_sync_config(cfg):
    """
    Writes a basic sync_from_unraid.sh based on config.
    You can plug in your full sync script here.
    """
    if not cfg["sync"]["enable_sync"]:
        return

    media_root = cfg["storage"]["media_root"]
    unraid_ip = cfg["sync"]["unraid_ip"]
    share_audio = cfg["sync"]["share_path_audio"]
    share_books = cfg["sync"]["share_path_books"]

    script = f"""#!/bin/bash
set -euo pipefail

UNRAID_IP="{unraid_ip}"
UNRAID_SHARE="//{unraid_ip}/data"
MOUNT_POINT="/mnt/unraid_data"

SRC_AUDIO="${{MOUNT_POINT}}{share_audio}"
SRC_BOOKS="${{MOUNT_POINT}}{share_books}"

DST_AUDIO="{media_root}/audiobooks/"
DST_BOOKS="{media_root}/books/"

CIFS_OPTS="guest,vers=3.0,iocharset=utf8,noperm,uid=1000,gid=1000"

LOG="/var/log/sync_from_unraid.log"
LOCK="/run/sync_from_unraid.lock"
FLAG="/tmp/sync_in_progress"

CARRIER_FILE="/sys/class/net/eth0/carrier"
if [[ ! -f "$CARRIER_FILE" ]] || [[ "$(cat "$CARRIER_FILE" 2>/dev/null)" != "1" ]]; then
  exit 0
fi

exec 9>"$LOCK" || true
flock -n 9 || exit 0

{{
  touch "$FLAG"
  echo
  echo "==== $(date '+%F %T') — sync start ===="

  for i in {{1..5}}; do
    if ping -c1 -W1 "$UNRAID_IP" >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done

  if ! mountpoint -q "$MOUNT_POINT"; then
    echo "Mounting $UNRAID_SHARE -> $MOUNT_POINT"
    mkdir -p "$MOUNT_POINT"
    mount -t cifs "$UNRAID_SHARE" "$MOUNT_POINT" -o "$CIFS_OPTS" || {{
      echo "ERROR: mount failed"
      rm -f "$FLAG"
      exit 0
    }}
  fi

  mkdir -p "$DST_AUDIO" "$DST_BOOKS"

  echo "Syncing Audiobooks..."
  rsync -av --ignore-existing "$SRC_AUDIO" "$DST_AUDIO" || true

  echo "Syncing Calibre books..."
  rsync -av --ignore-existing "$SRC_BOOKS" "$DST_BOOKS" || true

  if mountpoint -q "$MOUNT_POINT"; then
    echo "Unmounting $MOUNT_POINT"
    umount "$MOUNT_POINT" || true
  fi

  rm -f "$FLAG"
  echo "==== $(date '+%F %T') — sync done ===="
}} >>"$LOG" 2>&1
"""
    script_path = Path("/usr/local/bin/sync_from_unraid.sh")
    script_path.write_text(script)
    script_path.chmod(0o755)
    # You can also create/enable the eth0-watcher.service here if desired

FORM_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Lusty Library Setup</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; background:#111827; color:#f9fafb; margin:0; }
    .wrap { max-width:800px; margin:4vh auto; background:#1f2937; padding:24px;
            border-radius:16px; box-shadow:0 10px 40px rgba(0,0,0,.6); }
    h1 { margin-top:0; }
    fieldset { border:1px solid #374151; margin-bottom:18px; border-radius:8px; }
    legend { padding:0 8px; color:#9ca3af; }
    label { display:block; margin:8px 0; }
    input,select { width:100%; padding:8px; border-radius:6px; border:1px solid #4b5563;
                   background:#030712; color:#e5e7eb; }
    .row { display:flex; gap:12px; }
    .row > div { flex:1; }
    button { background:#10b981; color:#022c22; border:0; padding:10px 18px; border-radius:999px;
             font-weight:600; cursor:pointer; margin-top:10px; }
    button:hover { background:#059669; }
    .checkbox-row { display:flex; align-items:center; gap:8px; }
    .checkbox-row input { width:auto; }
    small { color:#9ca3af; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>📚 Lusty Library Setup</h1>
    <form method="post">
      <fieldset>
        <legend>Wi-Fi / Hotspot</legend>
        <div class="row">
          <div>
            <label>SSID
              <input name="wifi_ssid" value="{{ cfg.wifi.ssid }}">
            </label>
          </div>
          <div>
            <label>Password
              <input name="wifi_password" value="{{ cfg.wifi.password }}">
            </label>
          </div>
        </div>
        <label>Hotspot IP (wlan0)
          <input name="wifi_ip" value="{{ cfg.wifi.ip }}">
        </label>
        <small>This will update hostapd and dhcpcd for wlan0.</small>
      </fieldset>

      <fieldset>
        <legend>Storage</legend>
        <label>Media root path
          <input name="media_root" value="{{ cfg.storage.media_root }}">
        </label>
        <small>Subfolders like <code>audiobooks</code>, <code>books</code>, <code>config</code> will be created here.</small>
      </fieldset>

      <fieldset>
        <legend>Apps to install</legend>
        <div class="checkbox-row">
          <input type="checkbox" id="abs" name="install_audiobookshelf" {% if cfg.apps.install_audiobookshelf %}checked{% endif %}>
          <label for="abs">Install Audiobookshelf</label>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" id="cweb" name="install_calibre_web" {% if cfg.apps.install_calibre_web %}checked{% endif %}>
          <label for="cweb">Install Calibre-Web</label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Auto-sync from Unraid (optional)</legend>
        <div class="checkbox-row">
          <input type="checkbox" id="enable_sync" name="enable_sync" {% if cfg.sync.enable_sync %}checked{% endif %}>
          <label for="enable_sync">Enable auto-sync from Unraid when Ethernet plugged in</label>
        </div>
        <div class="row">
          <div>
            <label>Unraid IP
              <input name="unraid_ip" value="{{ cfg.sync.unraid_ip }}">
            </label>
          </div>
        </div>
        <label>Audiobooks share path on Unraid
          <input name="share_path_audio" value="{{ cfg.sync.share_path_audio }}">
        </label>
        <label>Books/Calibre share path on Unraid
          <input name="share_path_books" value="{{ cfg.sync.share_path_books }}">
        </label>
        <small>These are the paths as mounted under the Unraid share root (e.g. <code>/data/media/audiobook</code>).</small>
      </fieldset>

      <button type="submit">Apply & Generate Config</button>
    </form>
  </div>
</body>
</html>
"""

FORM_TEMPLATE = """..."""

@app.route("/")
def index():
    return redirect(url_for("setup")

@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg = load_config()

    if request.method == "POST":
        # Wi-Fi
        cfg["wifi"]["ssid"] = request.form.get("wifi_ssid", "").strip() or cfg["wifi"]["ssid"]
        cfg["wifi"]["password"] = request.form.get("wifi_password", "").strip() or cfg["wifi"]["password"]
        cfg["wifi"]["ip"] = request.form.get("wifi_ip", "").strip() or cfg["wifi"]["ip"]

        # Storage
        cfg["storage"]["media_root"] = request.form.get("media_root", "").strip() or cfg["storage"]["media_root"]

        # Apps
        cfg["apps"]["install_audiobookshelf"] = "install_audiobookshelf" in request.form
        cfg["apps"]["install_calibre_web"] = "install_calibre_web" in request.form

        # Sync
        cfg["sync"]["enable_sync"] = "enable_sync" in request.form
        cfg["sync"]["unraid_ip"] = request.form.get("unraid_ip", "").strip() or cfg["sync"]["unraid_ip"]
        cfg["sync"]["share_path_audio"] = request.form.get("share_path_audio", "").strip() or cfg["sync"]["share_path_audio"]
        cfg["sync"]["share_path_books"] = request.form.get("share_path_books", "").strip() or cfg["sync"]["share_path_books"]

        save_config(cfg)

        # Apply configs
        apply_wifi_config(cfg)
        compose_path = generate_docker_compose(cfg)
        apply_sync_config(cfg)

        # Bring up Docker stack
        try:
            subprocess.run(["docker", "compose", "-f", str(compose_path), "up", "-d", "--build"], check=False)
        except Exception:
            pass

        return redirect(url_for("setup"))

    # GET
    return render_template_string(FORM_TEMPLATE, cfg=cfg)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
