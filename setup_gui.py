#!/usr/bin/env python3
"""
Lusty Library setup wizard.

A small Flask app that:
  - asks for Wi-Fi hotspot / storage / apps / sync settings
  - applies them step by step (Wi-Fi, storage, Docker, apps, sync)
  - streams every command it runs, and its output, to the browser live
    over Server-Sent Events, so nothing fails silently.

Works on Raspberry Pi 3, 4 and 5, on both the classic dhcpcd/hostapd
network stack (older Raspberry Pi OS) and the newer NetworkManager
stack (current Raspberry Pi OS "Bookworm" and later).
"""
import itertools
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, request, render_template_string, redirect, url_for, jsonify
import yaml

app = Flask(__name__)

INSTALL_DIR = Path("/opt/lustylibrary-installer")
CONFIG_PATH = INSTALL_DIR / "config.yml"
BOARD_INFO_PATH = INSTALL_DIR / "board_info.json"

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
        "server_ip": "192.168.0.139",
        "server_username": "",
        "server_password": "",
        "server_path_audio": "/data/media/audiobook",
        "server_path_books": "/data/media/calibre",
    },
    "leds": {
        "enabled": False,
        "pin_wifi": 17,
        "pin_cweb": 27,
        "pin_abs": 22,
    },
    "shutdown_button": {
        "enabled": False,
        "pin": 26,
        "hold_secs": 2.0,
    },
}

# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------


def _deep_merge_defaults(cfg, defaults):
    """Fills in any keys missing from an on-disk config with defaults, so
    a config.yml saved by an older version of this wizard doesn't crash
    on newly-added sections (e.g. 'leds')."""
    for key, value in defaults.items():
        if key not in cfg:
            cfg[key] = value
        elif isinstance(value, dict) and isinstance(cfg.get(key), dict):
            _deep_merge_defaults(cfg[key], value)
    return cfg


def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r") as f:
            cfg = yaml.safe_load(f) or {}
        return _deep_merge_defaults(cfg, DEFAULT_CONFIG)
    cfg = yaml.safe_load(yaml.safe_dump(DEFAULT_CONFIG))
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f)


def load_board_info():
    """Board/OS info written by install.sh, with a live fallback if it's
    missing (e.g. setup_gui.py was run directly)."""
    if BOARD_INFO_PATH.exists():
        try:
            return json.loads(BOARD_INFO_PATH.read_text())
        except Exception:
            pass

    model = "unknown"
    dt_model = Path("/proc/device-tree/model")
    if dt_model.exists():
        model = dt_model.read_text(errors="ignore").strip("\x00").strip()

    codename = "unknown"
    os_release = Path("/etc/os-release")
    if os_release.exists():
        for line in os_release.read_text().splitlines():
            if line.startswith("VERSION_CODENAME="):
                codename = line.split("=", 1)[1].strip().strip('"')

    return {"model": model, "os_codename": codename, "arch": platform.machine()}


# ---------------------------------------------------------------------------
# live event log (Server-Sent Events)
# ---------------------------------------------------------------------------

EVENT_LOCK = threading.Lock()
EVENTS = []
_event_ids = itertools.count(1)

STEP_ORDER = [
    ("preflight", "Checking board and OS"),
    ("leds_test", "Status LED test screen"),
    ("wifi", "Configuring Wi-Fi hotspot"),
    ("storage", "Setting up storage"),
    ("docker", "Checking Docker"),
    ("apps", "Installing apps"),
    ("sync", "Configuring auto-sync"),
    ("trigger", "Setting up Ethernet plug-in trigger"),
    ("leds_service", "Installing status LED service"),
    ("shutdown_button", "Setting up shutdown button"),
]

STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "finished": False,
    "ok": None,
    "steps": {step_id: "pending" for step_id, _ in STEP_ORDER},
}


def emit(kind, **data):
    with EVENT_LOCK:
        ev = {"id": next(_event_ids), "kind": kind, "ts": time.time(), **data}
        EVENTS.append(ev)
        if len(EVENTS) > 4000:
            del EVENTS[: len(EVENTS) - 4000]
    return ev


def set_step(step_id, status, detail=None):
    with STATE_LOCK:
        STATE["steps"][step_id] = status
    emit("step", step=step_id, status=status, detail=detail)


def log_line(step_id, text, level="out"):
    emit("log", step=step_id, level=level, text=text)


class StepFailed(Exception):
    pass


# ---------------------------------------------------------------------------
# interactive confirmations ("did the LED turn on?")
# ---------------------------------------------------------------------------

CONFIRM_LOCK = threading.Lock()
CONFIRMS = {}  # confirm_id -> {"event": threading.Event, "answer": bool|None}
_confirm_ids = itertools.count(1)

CONFIRM_TIMEOUT_SECONDS = 15 * 60  # don't hang forever if nobody's watching


def ask_confirm(step_id, question, yes_label="Yes, it's working", no_label="No / skip"):
    """
    Pauses the running step and asks a yes/no question in the browser.
    Returns True/False once answered, or None if nobody answered within
    the timeout (treated as "skip" by callers).
    """
    confirm_id = next(_confirm_ids)
    event = threading.Event()
    with CONFIRM_LOCK:
        CONFIRMS[confirm_id] = {"event": event, "answer": None}

    emit(
        "confirm_request",
        step=step_id,
        confirm_id=confirm_id,
        question=question,
        yes_label=yes_label,
        no_label=no_label,
    )

    answered = event.wait(timeout=CONFIRM_TIMEOUT_SECONDS)
    with CONFIRM_LOCK:
        answer = CONFIRMS.pop(confirm_id, {}).get("answer")

    if not answered:
        log_line(step_id, f"No response to \"{question}\" after {CONFIRM_TIMEOUT_SECONDS // 60} min, continuing.", level="error")
        return None

    log_line(step_id, f"You answered: {'yes' if answer else 'no'} — {question}")
    return answer


def run_cmd(cmd, step_id, check=True, allow_fail=False):
    """
    Run a command, streaming '$ <command>' and every output line to the
    live console as it happens. Raises StepFailed on a non-zero exit
    unless allow_fail=True.
    """
    emit("cmd", step=step_id, text="$ " + " ".join(str(c) for c in cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
    except FileNotFoundError as e:
        log_line(step_id, f"command not found: {e}", level="error")
        if allow_fail:
            return 127
        raise StepFailed(str(e))

    assert proc.stdout is not None
    for line in proc.stdout:
        log_line(step_id, line.rstrip())
    proc.wait()

    if proc.returncode != 0:
        log_line(step_id, f"exited with code {proc.returncode}", level="error")
        if check and not allow_fail:
            raise StepFailed(f"{' '.join(str(c) for c in cmd)} failed (exit {proc.returncode})")
    return proc.returncode


def write_file(path, content, step_id, mode=None):
    log_line(step_id, f"writing {path}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        path.chmod(mode)


# ---------------------------------------------------------------------------
# network backend detection (dhcpcd/hostapd vs NetworkManager)
# ---------------------------------------------------------------------------


def detect_network_backend():
    if shutil.which("nmcli"):
        rc = subprocess.run(
            ["systemctl", "is-active", "--quiet", "NetworkManager"]
        ).returncode
        if rc == 0:
            return "networkmanager"
    return "dhcpcd"


def apply_wifi_config_networkmanager(cfg, step_id):
    ssid = cfg["wifi"]["ssid"]
    password = cfg["wifi"]["password"]
    ip = cfg["wifi"]["ip"]
    con_name = "LustyLibraryAP"

    log_line(step_id, "Using NetworkManager to configure the hotspot.")
    run_cmd(["nmcli", "radio", "wifi", "on"], step_id, allow_fail=True)
    run_cmd(["nmcli", "connection", "delete", con_name], step_id, allow_fail=True)
    run_cmd(
        [
            "nmcli", "connection", "add",
            "type", "wifi",
            "ifname", "wlan0",
            "con-name", con_name,
            "autoconnect", "yes",
            "ssid", ssid,
        ],
        step_id,
    )
    run_cmd(
        [
            "nmcli", "connection", "modify", con_name,
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "ipv4.method", "shared",
            "ipv4.addresses", f"{ip}/24",
        ],
        step_id,
    )
    run_cmd(
        [
            "nmcli", "connection", "modify", con_name,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", password,
        ],
        step_id,
    )
    run_cmd(["nmcli", "connection", "up", con_name], step_id)


def apply_wifi_config_dhcpcd(cfg, step_id):
    ssid = cfg["wifi"]["ssid"]
    password = cfg["wifi"]["password"]
    ip = cfg["wifi"]["ip"]

    log_line(step_id, "Using hostapd + dhcpcd to configure the hotspot.")

    if not shutil.which("hostapd") or not shutil.which("dnsmasq"):
        log_line(step_id, "hostapd/dnsmasq not found, installing...")
        run_cmd(["apt-get", "update"], step_id)
        run_cmd(["apt-get", "install", "-y", "hostapd", "dnsmasq"], step_id)

    hostapd_conf = Path("/etc/hostapd/hostapd.conf")
    if hostapd_conf.exists():
        text = hostapd_conf.read_text()
    else:
        text = (
            "interface=wlan0\n"
            "driver=nl80211\n"
            f"ssid={ssid}\n"
            "hw_mode=g\n"
            "channel=7\n"
            "wmm_enabled=0\n"
            "auth_algs=1\n"
            "wpa=2\n"
            f"wpa_passphrase={password}\n"
            "wpa_key_mgmt=WPA-PSK\n"
            "rsn_pairwise=CCMP\n"
        )

    lines = []
    seen_ssid = seen_psk = False
    for line in text.splitlines():
        if line.startswith("ssid="):
            lines.append(f"ssid={ssid}")
            seen_ssid = True
        elif line.startswith("wpa_passphrase="):
            lines.append(f"wpa_passphrase={password}")
            seen_psk = True
        else:
            lines.append(line)
    if not seen_ssid:
        lines.append(f"ssid={ssid}")
    if not seen_psk:
        lines.append(f"wpa_passphrase={password}")
    write_file(hostapd_conf, "\n".join(lines) + "\n", step_id)
    run_cmd(["systemctl", "unmask", "hostapd"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "hostapd"], step_id, allow_fail=True)
    run_cmd(["systemctl", "restart", "hostapd"], step_id, allow_fail=True)

    dhcpcd = Path("/etc/dhcpcd.conf")
    block = f"\ninterface wlan0\n  static ip_address={ip}/24\n  nohook wpa_supplicant\n"
    if dhcpcd.exists():
        text = dhcpcd.read_text()
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
        write_file(dhcpcd, "\n".join(new_lines) + block, step_id)
        run_cmd(["systemctl", "restart", "dhcpcd"], step_id, allow_fail=True)
    else:
        log_line(step_id, "/etc/dhcpcd.conf not found, skipping static-IP step.", level="error")


def apply_wifi_config(cfg, step_id):
    backend = detect_network_backend()
    log_line(step_id, f"Detected network backend: {backend}")
    if backend == "networkmanager":
        apply_wifi_config_networkmanager(cfg, step_id)
    else:
        apply_wifi_config_dhcpcd(cfg, step_id)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def get_storage_devices():
    """Removable partitions that look like good media-storage candidates.
    Deliberately skips the internal mmcblk0 (SD card / root)."""
    devices = []
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,TYPE,RM"], text=True
        )
        data = json.loads(out)

        def visit(node):
            t = node.get("type")
            name = node.get("name")
            rm = node.get("rm", 0)
            mp = node.get("mountpoint")
            size = node.get("size")
            fstype = node.get("fstype")
            if "children" in node:
                for ch in node["children"]:
                    visit(ch)
            if t == "part" and name and not name.startswith("mmcblk0") and str(rm) in ("1", "true", "True"):
                devices.append(
                    {
                        "path": f"/dev/{name}",
                        "size": size or "?",
                        "fstype": fstype or "",
                        "mountpoint": mp or "",
                    }
                )

        for dev in data.get("blockdevices", []):
            visit(dev)
    except Exception:
        pass
    return devices


def format_and_mount_device(device, mountpoint, step_id, fstype="ext4"):
    """EXTREMELY DESTRUCTIVE: formats the given device as ext4 and mounts
    it at the given mountpoint, with a systemd mount unit for boot-time
    auto-mount."""
    if not device.startswith("/dev/"):
        log_line(step_id, f"refusing to touch non-device path: {device}", level="error")
        return
    if "mmcblk0" in device:
        log_line(step_id, "refusing to format the internal SD card (mmcblk0).", level="error")
        return

    mountpoint = Path(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)

    run_cmd(["umount", device], step_id, allow_fail=True)
    run_cmd(["mkfs.ext4", "-F", device], step_id)
    run_cmd(["mount", device, str(mountpoint)], step_id)

    unit_name = mountpoint.as_posix().lstrip("/").replace("/", "-") + ".mount"
    unit_path = Path("/etc/systemd/system") / unit_name
    unit_content = f"""[Unit]
Description=Lusty Library media ({device})
After=local-fs.target

[Mount]
What={device}
Where={mountpoint}
Type={fstype}
Options=defaults

[Install]
WantedBy=multi-user.target
"""
    write_file(unit_path, unit_content, step_id)
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "--now", unit_name], step_id, allow_fail=True)


# ---------------------------------------------------------------------------
# docker / apps
# ---------------------------------------------------------------------------


def ensure_docker(step_id):
    if shutil.which("docker"):
        out = subprocess.run(["docker", "--version"], capture_output=True, text=True)
        log_line(step_id, f"Docker already installed: {out.stdout.strip()}")
        return
    log_line(step_id, "Docker not found, installing via get.docker.com...")
    run_cmd(["curl", "-fsSL", "https://get.docker.com", "-o", "/tmp/get-docker.sh"], step_id)
    run_cmd(["sh", "/tmp/get-docker.sh"], step_id)


def generate_docker_compose(cfg, step_id):
    media_root = cfg["storage"]["media_root"]
    install_abs = cfg["apps"]["install_audiobookshelf"]
    install_cweb = cfg["apps"]["install_calibre_web"]

    lines = ['version: "3.8"', "", "services:"]

    if install_abs:
        lines += [
            "  audiobookshelf:",
            "    image: ghcr.io/advplyr/audiobookshelf:latest",
            "    container_name: audiobookshelf",
            "    ports:",
            '      - "13378:80"',
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
            '      - "8083:8083"',
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

    compose_path = Path("/home/pi/library-server")
    write_file(compose_path / "docker-compose.yml", "\n".join(lines), step_id)

    for sub in ("audiobooks", "books", "config"):
        Path(media_root, sub).mkdir(parents=True, exist_ok=True)

    return compose_path / "docker-compose.yml"


def bring_up_apps(compose_path, step_id):
    rc = run_cmd(["docker", "compose", "-f", str(compose_path), "up", "-d"], step_id, allow_fail=True)
    if rc != 0:
        log_line(step_id, "'docker compose' plugin unavailable, trying legacy 'docker-compose'...")
        run_cmd(["docker-compose", "-f", str(compose_path), "up", "-d"], step_id)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

SYNC_CREDENTIALS_PATH = Path("/etc/lustylibrary-sync-credentials")
SYNC_SERVICE_PATH = Path("/etc/systemd/system/lustylibrary-sync.service")


def detect_ethernet_iface():
    """Best-effort detection of the wired Ethernet interface name. Raspberry
    Pi OS has used eth0 historically and end0 on some Pi 5 images with
    predictable network names; falls back to eth0 if nothing is found."""
    net_dir = Path("/sys/class/net")
    candidates = []
    if net_dir.exists():
        for iface_path in net_dir.iterdir():
            name = iface_path.name
            if name == "lo" or (iface_path / "wireless").exists():
                continue
            try:
                if (iface_path / "type").read_text().strip() != "1":  # ARPHRD_ETHER
                    continue
            except Exception:
                continue
            candidates.append(name)
    for preferred in ("eth0", "end0"):
        if preferred in candidates:
            return preferred
    return candidates[0] if candidates else "eth0"


def apply_sync_config(cfg, step_id):
    if not cfg["sync"]["enable_sync"]:
        log_line(step_id, "Auto-sync disabled, skipping.")
        return

    media_root = cfg["storage"]["media_root"]
    server_ip = cfg["sync"]["server_ip"]
    username = (cfg["sync"].get("server_username") or "").strip()
    password = cfg["sync"].get("server_password") or ""
    path_audio = cfg["sync"]["server_path_audio"]
    path_books = cfg["sync"]["server_path_books"]
    eth_iface = detect_ethernet_iface()
    log_line(step_id, f"Detected wired interface: {eth_iface}")

    if not shutil.which("mount.cifs") and not shutil.which("cifs.mount"):
        log_line(step_id, "cifs-utils not found, installing (needed to mount the server share)...")
        run_cmd(["apt-get", "update"], step_id)
        run_cmd(["apt-get", "install", "-y", "cifs-utils"], step_id)

    if username:
        log_line(step_id, f"Writing SMB credentials file for user '{username}'.")
        cred_content = f"username={username}\npassword={password}\n"
        write_file(SYNC_CREDENTIALS_PATH, cred_content, step_id, mode=0o600)
        cifs_opts = f"credentials={SYNC_CREDENTIALS_PATH},vers=3.0,iocharset=utf8,noperm,uid=1000,gid=1000"
    else:
        log_line(step_id, "No username given, mounting the server share as guest.")
        if SYNC_CREDENTIALS_PATH.exists():
            SYNC_CREDENTIALS_PATH.unlink()
        cifs_opts = "guest,vers=3.0,iocharset=utf8,noperm,uid=1000,gid=1000"

    script = f"""#!/bin/bash
set -euo pipefail

SERVER_IP="{server_ip}"
SERVER_SHARE="//{server_ip}/data"
MOUNT_POINT="/mnt/remote_data"

SRC_AUDIO="${{MOUNT_POINT}}{path_audio}"
SRC_BOOKS="${{MOUNT_POINT}}{path_books}"

DST_AUDIO="{media_root}/audiobooks/"
DST_BOOKS="{media_root}/books/"

CIFS_OPTS="{cifs_opts}"

LOG="/var/log/sync_from_server.log"
LOCK="/run/sync_from_server.lock"
FLAG="/tmp/sync_in_progress"

CARRIER_FILE="/sys/class/net/{eth_iface}/carrier"
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
    if ping -c1 -W1 "$SERVER_IP" >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done

  if ! mountpoint -q "$MOUNT_POINT"; then
    echo "Mounting $SERVER_SHARE -> $MOUNT_POINT"
    mkdir -p "$MOUNT_POINT"
    mount -t cifs "$SERVER_SHARE" "$MOUNT_POINT" -o "$CIFS_OPTS" || {{
      echo "ERROR: mount failed"
      rm -f "$FLAG"
      exit 0
    }}
  fi

  mkdir -p "$DST_AUDIO" "$DST_BOOKS"

  echo "Syncing audiobooks..."
  rsync -av --ignore-existing "$SRC_AUDIO" "$DST_AUDIO" || true

  echo "Syncing books..."
  rsync -av --ignore-existing "$SRC_BOOKS" "$DST_BOOKS" || true

  if mountpoint -q "$MOUNT_POINT"; then
    echo "Unmounting $MOUNT_POINT"
    umount "$MOUNT_POINT" || true
  fi

  rm -f "$FLAG"
  echo "==== $(date '+%F %T') — sync done ===="
}} >>"$LOG" 2>&1
"""
    write_file("/usr/local/bin/sync_from_server.sh", script, step_id, mode=0o755)
    log_line(step_id, "Wrote /usr/local/bin/sync_from_server.sh.")


def apply_sync_trigger(cfg, step_id):
    """
    Wires the sync script to actually fire when the Ethernet cable is
    plugged in, instead of relying on something else to call it:
      - a oneshot systemd service that runs the sync script (so it's
        logged with `journalctl -u lustylibrary-sync` too)
      - on NetworkManager systems, a dispatcher script that fires on the
        wired interface's "up" event
      - otherwise, a udev rule that fires when that interface's carrier
        (link) state changes to up

    This triggers on link coming up (cable inserted / negotiated), which is
    the practical, event-driven equivalent of "sees activity" for a link
    that's otherwise idle — true packet-level traffic monitoring would need
    a separate always-running daemon and isn't needed to get pull-on-plug
    behavior.
    """
    if not cfg["sync"]["enable_sync"]:
        log_line(step_id, "Auto-sync disabled, skipping trigger setup.")
        return

    eth_iface = detect_ethernet_iface()

    service_content = f"""[Unit]
Description=Lusty Library sync from server (triggered by {eth_iface} link up)
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/sync_from_server.sh
"""
    write_file(SYNC_SERVICE_PATH, service_content, step_id)
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)

    backend = detect_network_backend()
    log_line(step_id, f"Detected network backend: {backend}")

    if backend == "networkmanager":
        dispatcher_path = Path("/etc/NetworkManager/dispatcher.d/99-lustylibrary-sync")
        dispatcher_content = f"""#!/bin/bash
# Runs the Lusty Library sync whenever {eth_iface} comes up.
IFACE="$1"
ACTION="$2"

if [[ "$IFACE" == "{eth_iface}" && ( "$ACTION" == "up" || "$ACTION" == "connectivity-change" ) ]]; then
    systemctl start --no-block lustylibrary-sync.service
fi
"""
        write_file(dispatcher_path, dispatcher_content, step_id, mode=0o755)
        log_line(step_id, f"Installed NetworkManager dispatcher hook for {eth_iface}.")
    else:
        udev_rule_path = Path("/etc/udev/rules.d/99-lustylibrary-sync.rules")
        udev_content = (
            f'SUBSYSTEM=="net", KERNEL=="{eth_iface}", ACTION=="change", '
            f'ATTR{{operstate}}=="up", RUN+="/usr/bin/systemctl start --no-block lustylibrary-sync.service"\n'
        )
        write_file(udev_rule_path, udev_content, step_id)
        run_cmd(["udevadm", "control", "--reload-rules"], step_id, allow_fail=True)
        log_line(step_id, f"Installed udev rule for {eth_iface} link-up.")

    log_line(
        step_id,
        f"Plugging a cable into {eth_iface} now triggers lustylibrary-sync.service "
        "immediately. Manually run 'sudo systemctl start lustylibrary-sync.service' "
        "to test it without unplugging anything.",
    )


# ---------------------------------------------------------------------------
# status LEDs (GPIO)
# ---------------------------------------------------------------------------

LED_SERVICE_PATH = Path("/etc/systemd/system/lustylibrary-leds.service")
LED_SCRIPT_PATH = Path("/usr/local/bin/status_leds.py")


def gpio_available():
    return Path("/dev/gpiochip0").exists() or Path("/dev/gpiochip4").exists()


def ensure_gpio_deps(step_id):
    try:
        import gpiozero  # noqa: F401
        log_line(step_id, "gpiozero already installed.")
        return True
    except ImportError:
        pass

    log_line(step_id, "Installing gpiozero (and lgpio backend) for LED control...")
    rc = run_cmd(["apt-get", "install", "-y", "python3-gpiozero", "python3-rpi-lgpio"], step_id, allow_fail=True)
    if rc != 0:
        rc = run_cmd(
            ["pip3", "install", "--break-system-packages", "gpiozero", "rpi-lgpio"],
            step_id,
            allow_fail=True,
        )
    try:
        import gpiozero  # noqa: F401
        return True
    except ImportError:
        log_line(step_id, "Could not import gpiozero after install attempt.", level="error")
        return False


def blink_led(pin, step_id, label, times=3, on_time=0.2, off_time=0.2, leave_on=False):
    """Blinks a single LED a few times so the person can visually confirm
    which physical LED corresponds to which pin/feature."""
    try:
        from gpiozero import LED
    except ImportError:
        log_line(step_id, "gpiozero not available, cannot test LEDs.", level="error")
        return False

    log_line(step_id, f"Blinking {label} LED on GPIO{pin} ({times}x)...")
    try:
        with LED(pin) as led:
            for _ in range(times):
                led.on()
                time.sleep(on_time)
                led.off()
                time.sleep(off_time)
            if leave_on:
                led.on()
                time.sleep(0.5)
                led.off()
        return True
    except Exception as e:  # noqa: BLE001 - bad wiring/pin shouldn't kill setup
        log_line(step_id, f"Could not drive GPIO{pin} ({label}): {e}", level="error")
        return False


def run_led_test_screen(cfg, step_id):
    """The general 'test screen': blinks every configured LED once, in
    sequence, and asks a single yes/no question before moving on."""
    if not cfg["leds"]["enabled"]:
        log_line(step_id, "Status LEDs disabled in setup, skipping LED tests.")
        return

    if not gpio_available():
        log_line(step_id, "No GPIO chip found on this board — skipping LED tests.", level="error")
        return

    if not ensure_gpio_deps(step_id):
        return

    leds = cfg["leds"]
    log_line(step_id, "Running through each configured LED so you can watch the board...")
    blink_led(leds["pin_wifi"], step_id, "Wi-Fi", times=2)
    blink_led(leds["pin_cweb"], step_id, "Calibre-Web", times=2)
    blink_led(leds["pin_abs"], step_id, "Audiobookshelf", times=2)

    answer = ask_confirm(
        step_id,
        f"Did all three LEDs (GPIO{leds['pin_wifi']}, GPIO{leds['pin_cweb']}, GPIO{leds['pin_abs']}) blink twice?",
    )
    if answer is False:
        log_line(
            step_id,
            "Check wiring/pin numbers in the form above and re-run setup. "
            "Continuing installation regardless — LEDs are cosmetic.",
            level="error",
        )


def test_feature_led(cfg, step_id, pin, feature_label):
    """Called right after a specific feature (Wi-Fi, Calibre-Web,
    Audiobookshelf) comes up, so the test is tied to the thing it
    indicates rather than only run once up front."""
    if not cfg["leds"]["enabled"] or not gpio_available():
        return
    blink_led(pin, step_id, feature_label, times=3, leave_on=True)
    answer = ask_confirm(step_id, f"Did the {feature_label} LED (GPIO{pin}) light up?")
    if answer is False:
        log_line(step_id, f"{feature_label} LED not confirmed working — check GPIO{pin} wiring.", level="error")


def generate_status_leds_script(cfg):
    leds = cfg["leds"]
    wifi_ip = cfg["wifi"]["ip"]
    install_cweb = cfg["apps"]["install_calibre_web"]
    install_abs = cfg["apps"]["install_audiobookshelf"]

    led_defs = [f'LED_WIFI = LED({leds["pin_wifi"]})   # Wi-Fi AP + sync indicator']
    if install_cweb:
        led_defs.append(f'LED_CWEB = LED({leds["pin_cweb"]})   # Calibre-Web')
    if install_abs:
        led_defs.append(f'LED_ABS  = LED({leds["pin_abs"]})   # Audiobookshelf')

    cweb_block = ""
    if install_cweb:
        cweb_block = """
        calibre_up = container_running("calibre-web")
        if not calibre_up:
            blink(LED_CWEB, 1)
        else:
            LED_CWEB.on()"""

    abs_block = ""
    if install_abs:
        abs_block = """
        audio_up = container_running("audiobookshelf")
        if not audio_up:
            blink(LED_ABS, 1)
        else:
            LED_ABS.on()"""

    startup_leds = ["LED_WIFI"] + (["LED_CWEB"] if install_cweb else []) + (["LED_ABS"] if install_abs else [])

    return f'''#!/usr/bin/env python3
import subprocess
import time
import os
from gpiozero import LED

# BCM pin numbers (set by the Lusty Library setup wizard)
{chr(10).join(led_defs)}

CHECK_INTERVAL = 0.2   # loop rate
BLINK_RATE     = 0.2   # used for slow blink while starting

SYNC_FLAG = "/tmp/sync_in_progress"
WIFI_IP_PREFIX = "{wifi_ip}"


def interface_has_ip(iface, ip_prefix):
    try:
        out = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True, timeout=3
        )
        return ip_prefix in out.stdout
    except Exception:
        return False


def container_running(name):
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{{{.State.Running}}}}", name],
            capture_output=True, text=True, timeout=3
        )
        return out.stdout.strip().lower() == "true"
    except Exception:
        return False


def blink(led, times=1):
    for _ in range(times):
        led.on()
        time.sleep(BLINK_RATE)
        led.off()
        time.sleep(BLINK_RATE)


def main():
    for led in ({", ".join(startup_leds)},):
        blink(led, 2)

    while True:
        wifi_ready = interface_has_ip("wlan0", WIFI_IP_PREFIX)
        syncing = os.path.exists(SYNC_FLAG)

        # Wi-Fi LED:
        # - If syncing from the server: fast toggle
        # - Else: blink while starting, solid when the hotspot IP is up
        if syncing:
            LED_WIFI.toggle()
        elif not wifi_ready:
            blink(LED_WIFI, 1)
        else:
            LED_WIFI.on()
{cweb_block}
{abs_block}

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        for led in ({", ".join(startup_leds)},):
            led.off()
'''


def install_led_service(cfg, step_id):
    if not cfg["leds"]["enabled"]:
        log_line(step_id, "Status LEDs disabled, skipping LED service install.")
        return

    if not gpio_available():
        log_line(step_id, "No GPIO chip found on this board — skipping LED service install.", level="error")
        return

    if not ensure_gpio_deps(step_id):
        return

    script = generate_status_leds_script(cfg)
    write_file(LED_SCRIPT_PATH, script, step_id, mode=0o755)

    service_content = """[Unit]
Description=Lusty Library status LEDs
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/env python3 /usr/local/bin/status_leds.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    write_file(LED_SERVICE_PATH, service_content, step_id)
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "--now", "lustylibrary-leds.service"], step_id, allow_fail=True)
    log_line(step_id, "Status LED service installed and running.")


# ---------------------------------------------------------------------------
# shutdown button (GPIO)
# ---------------------------------------------------------------------------

SHUTDOWN_SCRIPT_PATH = Path("/usr/local/bin/shutdown_button.py")
SHUTDOWN_SERVICE_PATH = Path("/etc/systemd/system/lustylibrary-shutdown-button.service")
SHUTDOWN_TEST_WINDOW_SECONDS = 20


def test_shutdown_button(cfg, step_id):
    """
    Actually exercises the GPIO pin rather than asking the person to
    self-report: waits for a real press+release and reports how long it
    was held, without ever calling shutdown_sequence(). Returns True if a
    press was detected at all (even a short one — that at least proves
    the wiring/pin is right), False if nothing was seen in the window.
    """
    pin = cfg["shutdown_button"]["pin"]
    hold_secs = cfg["shutdown_button"]["hold_secs"]

    try:
        from gpiozero import Button
    except ImportError:
        log_line(step_id, "gpiozero not available, cannot test the shutdown button.", level="error")
        return False

    log_line(
        step_id,
        f"Press and HOLD the shutdown button (GPIO{pin}) for at least {hold_secs:.1f}s now — "
        f"you have {SHUTDOWN_TEST_WINDOW_SECONDS}s. This will NOT power off the Pi.",
    )

    result = {"held": None}
    state = {"pressed_at": None}

    def on_press():
        state["pressed_at"] = time.monotonic()

    def on_release():
        if state["pressed_at"] is not None:
            result["held"] = time.monotonic() - state["pressed_at"]

    try:
        btn = Button(pin, pull_up=True, bounce_time=0.05)
        btn.when_pressed = on_press
        btn.when_released = on_release
        deadline = time.time() + SHUTDOWN_TEST_WINDOW_SECONDS
        while time.time() < deadline and result["held"] is None:
            time.sleep(0.1)
        btn.close()
    except Exception as e:  # noqa: BLE001 - bad wiring/pin shouldn't kill setup
        log_line(step_id, f"Could not read GPIO{pin}: {e}", level="error")
        return False

    if result["held"] is None:
        log_line(step_id, f"No press detected on GPIO{pin} within {SHUTDOWN_TEST_WINDOW_SECONDS}s.", level="error")
        return False

    log_line(step_id, f"Detected a press on GPIO{pin}, held for {result['held']:.2f}s.")
    if result["held"] >= hold_secs:
        log_line(step_id, "That's long enough to trigger a shutdown once the service is running.")
    else:
        log_line(
            step_id,
            f"Wiring works, but that hold was shorter than the {hold_secs:.1f}s threshold — "
            "just hold it longer once the service is installed.",
        )
    return True


def generate_shutdown_button_script(cfg):
    pin = cfg["shutdown_button"]["pin"]
    hold_secs = cfg["shutdown_button"]["hold_secs"]
    leds_enabled = cfg["leds"]["enabled"]
    led_pins = [cfg["leds"]["pin_wifi"], cfg["leds"]["pin_cweb"], cfg["leds"]["pin_abs"]]

    if leds_enabled:
        led_flash_block = f"""
    # Stop the status-LED service so it releases the LED pins
    os.system("systemctl stop lustylibrary-leds.service")
    time.sleep(0.2)

    from gpiozero import LED
    leds = [LED(p) for p in {led_pins!r}]

    def all_on():
        for led in leds:
            led.on()

    def all_off():
        for led in leds:
            led.off()

    all_off()
    for _ in range(3):
        all_on()
        time.sleep(0.15)
        all_off()
        time.sleep(0.15)

    end = time.time() + 2.0
    while time.time() < end:
        all_on()
        time.sleep(0.1)
        all_off()
        time.sleep(0.1)

    all_off()
"""
    else:
        led_flash_block = "\n    # Status LEDs not enabled in setup, skipping the flash sequence.\n"

    return f'''#!/usr/bin/env python3
from gpiozero import Button
import time, os, sys

BTN_PIN = {pin}          # BCM {pin} (set by the Lusty Library setup wizard)
HOLD_SECS = {hold_secs}

btn = Button(BTN_PIN, pull_up=True, bounce_time=0.05)
pressed_at = None


def log(msg: str):
    sys.stdout.write(msg + "\\n")
    sys.stdout.flush()


def shutdown_sequence():
    log("shutdown-button: starting shutdown sequence")
{led_flash_block}
    log("shutdown-button: calling poweroff")
    os.system("/sbin/poweroff")


def on_press():
    global pressed_at
    pressed_at = time.monotonic()
    log("shutdown-button: pressed")


def on_release():
    global pressed_at
    if pressed_at is None:
        return
    held = time.monotonic() - pressed_at
    pressed_at = None
    log(f"shutdown-button: released after {{held:.2f}}s")

    if held >= HOLD_SECS:
        log("shutdown-button: held long enough, initiating shutdown")
        shutdown_sequence()


btn.when_pressed = on_press
btn.when_released = on_release

log("shutdown-button: watcher started on GPIO{pin}")
from signal import pause
try:
    pause()
finally:
    try:
        from gpiozero import LED
        for p in {led_pins!r}:
            LED(p).off()
    except Exception:
        pass
'''


def setup_shutdown_button(cfg, step_id):
    if not cfg["shutdown_button"]["enabled"]:
        log_line(step_id, "Shutdown button disabled, skipping.")
        return

    if not gpio_available():
        log_line(step_id, "No GPIO chip found on this board — skipping shutdown button setup.", level="error")
        return

    if not ensure_gpio_deps(step_id):
        return

    ok = test_shutdown_button(cfg, step_id)
    if not ok:
        log_line(
            step_id,
            "Installing the service anyway — re-check wiring/pin number, then "
            "'sudo systemctl status lustylibrary-shutdown-button.service' to confirm it's running.",
            level="error",
        )

    script = generate_shutdown_button_script(cfg)
    write_file(SHUTDOWN_SCRIPT_PATH, script, step_id, mode=0o755)

    service_content = f"""[Unit]
Description=Lusty Library shutdown button (GPIO{cfg['shutdown_button']['pin']})
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 {SHUTDOWN_SCRIPT_PATH}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    write_file(SHUTDOWN_SERVICE_PATH, service_content, step_id)
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "--now", "lustylibrary-shutdown-button.service"], step_id, allow_fail=True)
    log_line(step_id, "Shutdown button service installed and running.")


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def run_pipeline(cfg, storage_device, format_device):
    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["finished"] = False
        STATE["ok"] = None
        for step_id, _ in STEP_ORDER:
            STATE["steps"][step_id] = "pending"

    emit("start")
    ok = True
    try:
        # preflight
        set_step("preflight", "running")
        board = load_board_info()
        log_line("preflight", f"Board: {board.get('model', 'unknown')}")
        log_line("preflight", f"OS codename: {board.get('os_codename', 'unknown')}")
        log_line("preflight", f"Architecture: {board.get('arch', 'unknown')}")
        set_step("preflight", "done")

        # leds: general test screen, up front
        set_step("leds_test", "running")
        run_led_test_screen(cfg, "leds_test")
        set_step("leds_test", "done")

        # wifi
        set_step("wifi", "running")
        apply_wifi_config(cfg, "wifi")
        test_feature_led(cfg, "wifi", cfg["leds"]["pin_wifi"], "Wi-Fi")
        set_step("wifi", "done")

        # storage
        set_step("storage", "running")
        if storage_device and format_device:
            format_and_mount_device(storage_device, cfg["storage"]["media_root"], "storage")
        else:
            log_line("storage", "No device selected for formatting; using existing storage as-is.")
            Path(cfg["storage"]["media_root"]).mkdir(parents=True, exist_ok=True)
        set_step("storage", "done")

        # docker
        set_step("docker", "running")
        ensure_docker("docker")
        set_step("docker", "done")

        # apps
        set_step("apps", "running")
        compose_path = generate_docker_compose(cfg, "apps")
        bring_up_apps(compose_path, "apps")
        if cfg["apps"]["install_calibre_web"]:
            test_feature_led(cfg, "apps", cfg["leds"]["pin_cweb"], "Calibre-Web")
        if cfg["apps"]["install_audiobookshelf"]:
            test_feature_led(cfg, "apps", cfg["leds"]["pin_abs"], "Audiobookshelf")
        set_step("apps", "done")

        # sync
        set_step("sync", "running")
        apply_sync_config(cfg, "sync")
        set_step("sync", "done")

        # trigger
        set_step("trigger", "running")
        apply_sync_trigger(cfg, "trigger")
        set_step("trigger", "done")

        # leds: install the persistent status-LED daemon last, now that
        # docker/app state it reports on actually exists
        set_step("leds_service", "running")
        install_led_service(cfg, "leds_service")
        set_step("leds_service", "done")

        # shutdown button: tested and installed last of all, since its
        # script needs to know the final LED service name/pins
        set_step("shutdown_button", "running")
        setup_shutdown_button(cfg, "shutdown_button")
        set_step("shutdown_button", "done")

    except StepFailed as e:
        ok = False
        # mark the in-flight step (and anything after it) as failed/skipped
        with STATE_LOCK:
            steps = STATE["steps"]
            failed_marked = False
            for step_id, _ in STEP_ORDER:
                if steps[step_id] == "running":
                    steps[step_id] = "error"
                    failed_marked = True
                elif failed_marked:
                    steps[step_id] = "skipped"
        emit("log", step="pipeline", level="error", text=f"Setup stopped: {e}")
    except Exception as e:  # noqa: BLE001 - keep the wizard alive no matter what
        ok = False
        with STATE_LOCK:
            steps = STATE["steps"]
            failed_marked = False
            for step_id, _ in STEP_ORDER:
                if steps[step_id] == "running":
                    steps[step_id] = "error"
                    failed_marked = True
                elif failed_marked:
                    steps[step_id] = "skipped"
        emit("log", step="pipeline", level="error", text=f"Unexpected error: {e}")
    finally:
        with STATE_LOCK:
            STATE["running"] = False
            STATE["finished"] = True
            STATE["ok"] = ok
        emit("done", ok=ok)


# ---------------------------------------------------------------------------
# web UI
# ---------------------------------------------------------------------------

FORM_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Lusty Library Setup</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; background:#111827; color:#f9fafb; margin:0; }
    .wrap { max-width:900px; margin:4vh auto; padding:0 16px 40px; }
    .card { background:#1f2937; padding:24px; border-radius:16px; box-shadow:0 10px 40px rgba(0,0,0,.6); margin-bottom:20px; }
    h1 { margin-top:0; }
    fieldset { border:1px solid #374151; margin-bottom:18px; border-radius:8px; }
    legend { padding:0 8px; color:#9ca3af; }
    label { display:block; margin:8px 0; }
    input,select { width:100%; padding:8px; border-radius:6px; border:1px solid #4b5563;
                   background:#030712; color:#e5e7eb; box-sizing:border-box; }
    .row { display:flex; gap:12px; }
    .row > div { flex:1; }
    button { background:#10b981; color:#022c22; border:0; padding:10px 18px; border-radius:999px;
             font-weight:600; cursor:pointer; margin-top:10px; font-size:15px; }
    button:hover:not(:disabled) { background:#059669; }
    button:disabled { background:#4b5563; color:#9ca3af; cursor:not-allowed; }
    .checkbox-row { display:flex; align-items:center; gap:8px; }
    .checkbox-row input { width:auto; }
    small { color:#9ca3af; }
    .board-info { color:#9ca3af; font-size:14px; margin-bottom:20px; }
    #steps { list-style:none; padding:0; margin:0 0 16px; }
    #steps li { display:flex; align-items:center; gap:10px; padding:6px 0; font-size:15px; }
    .badge { display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px;
             border-radius:50%; font-size:12px; flex-shrink:0; }
    .badge.pending { background:#374151; color:#9ca3af; }
    .badge.running { background:#f59e0b; color:#451a03; animation:pulse 1s infinite; }
    .badge.done { background:#10b981; color:#022c22; }
    .badge.error { background:#ef4444; color:#450a0a; }
    .badge.skipped { background:#374151; color:#6b7280; }
    @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.4;} }
    #console { background:#030712; color:#d1d5db; font-family:ui-monospace,Menlo,Consolas,monospace;
                font-size:13px; padding:14px; border-radius:8px; height:320px; overflow-y:auto;
                white-space:pre-wrap; word-break:break-all; }
    #console .line-error { color:#f87171; }
    #console .line-cmd { color:#93c5fd; }
    #banner { display:none; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-weight:600; }
    #banner.ok { display:block; background:#064e3b; color:#a7f3d0; }
    #banner.fail { display:block; background:#450a0a; color:#fecaca; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>📚 Lusty Library Setup</h1>
      <div class="board-info">
        Board: {{ board.model }} &middot; OS: {{ board.os_codename }} &middot; Arch: {{ board.arch }}
      </div>

      <div id="banner"></div>

      <form id="setup-form">
        <fieldset>
          <legend>Wi-Fi / Hotspot</legend>
          <div class="row">
            <div>
              <label>SSID
                <input name="wifi_ssid" value="{{ cfg.wifi.ssid }}" placeholder="e.g. LustyLibrary">
              </label>
            </div>
            <div>
              <label>Password
                <input name="wifi_password" value="{{ cfg.wifi.password }}" placeholder="e.g. lustybooks123">
              </label>
            </div>
          </div>
          <label>Hotspot IP (wlan0)
            <input name="wifi_ip" value="{{ cfg.wifi.ip }}" placeholder="e.g. 10.10.10.10">
          </label>
          <small>Automatically uses NetworkManager on newer Raspberry Pi OS, or hostapd/dhcpcd on older images.</small>
        </fieldset>

        <fieldset>
          <legend>Storage</legend>
          <label>Media root path
            <input name="media_root" value="{{ cfg.storage.media_root }}" placeholder="e.g. /mnt/media">
          </label>
          <small>Subfolders like <code>audiobooks</code>, <code>books</code>, <code>config</code> will be created here.</small>

          <label>Detected storage device to use (optional)
            <select name="storage_device">
              <option value="">-- Leave existing storage as-is --</option>
              {% for d in devices %}
                <option value="{{ d.path }}">{{ d.path }} ({{ d.size }}, {{ d.fstype or 'unformatted' }}, {% if d.mountpoint %}mounted at {{ d.mountpoint }}{% else %}not mounted{% endif %})</option>
              {% endfor %}
            </select>
          </label>
          <div class="checkbox-row">
            <input type="checkbox" id="format_device" name="format_device">
            <label for="format_device">Format selected device as ext4 and mount at {{ cfg.storage.media_root }} <strong>(⚠ erases all data on that device)</strong></label>
          </div>
          <small>If you're already using an attached drive for media, leave the device blank and do not check the format box.</small>
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
          <small>Docker will be installed automatically first if it isn't already present.</small>
        </fieldset>

        <fieldset>
          <legend>Auto-sync from Server (optional)</legend>
          <div class="checkbox-row">
            <input type="checkbox" id="enable_sync" name="enable_sync" {% if cfg.sync.enable_sync %}checked{% endif %}>
            <label for="enable_sync">Enable auto-sync from server when Ethernet is plugged in</label>
          </div>
          <div class="row">
            <div>
              <label>Server IP
                <input name="server_ip" value="{{ cfg.sync.server_ip }}" placeholder="e.g. 192.168.0.139">
              </label>
            </div>
          </div>
          <div class="row">
            <div>
              <label>Username <small>(leave blank for guest access)</small>
                <input name="server_username" value="{{ cfg.sync.server_username }}" placeholder="e.g. ryan">
              </label>
            </div>
            <div>
              <label>Password
                <input type="password" name="server_password" value="{{ cfg.sync.server_password }}" placeholder="leave blank if guest">
              </label>
            </div>
          </div>
          <label>Audiobooks path on server
            <input name="server_path_audio" value="{{ cfg.sync.server_path_audio }}" placeholder="e.g. /data/media/audiobook">
          </label>
          <label>Books/Calibre path on server
            <input name="server_path_books" value="{{ cfg.sync.server_path_books }}" placeholder="e.g. /data/media/calibre">
          </label>
          <small>These are the paths as they exist on the remote server share (for example <code>/data/media/audiobook</code>). Sync runs automatically the moment an Ethernet cable is plugged in — no polling.</small>
        </fieldset>

        <fieldset>
          <legend>Status LEDs (optional)</legend>
          <div class="checkbox-row">
            <input type="checkbox" id="leds_enabled" name="leds_enabled" {% if cfg.leds.enabled %}checked{% endif %}>
            <label for="leds_enabled">I have status LEDs wired up (Wi-Fi / Calibre-Web / Audiobookshelf)</label>
          </div>
          <div class="row">
            <div>
              <label>Wi-Fi LED (BCM pin)
                <input name="pin_wifi" value="{{ cfg.leds.pin_wifi }}">
              </label>
            </div>
            <div>
              <label>Calibre-Web LED (BCM pin)
                <input name="pin_cweb" value="{{ cfg.leds.pin_cweb }}">
              </label>
            </div>
            <div>
              <label>Audiobookshelf LED (BCM pin)
                <input name="pin_abs" value="{{ cfg.leds.pin_abs }}">
              </label>
            </div>
          </div>
          <small>Each LED is tested (blink + confirm) right after the feature it indicates is installed, plus a
          full test screen up front. The status service is installed last so it doesn't fight the test.</small>
        </fieldset>

        <fieldset>
          <legend>Shutdown button (optional)</legend>
          <div class="checkbox-row">
            <input type="checkbox" id="shutdown_enabled" name="shutdown_enabled" {% if cfg.shutdown_button.enabled %}checked{% endif %}>
            <label for="shutdown_enabled">I have a shutdown button wired up</label>
          </div>
          <div class="row">
            <div>
              <label>Button (BCM pin)
                <input name="shutdown_pin" value="{{ cfg.shutdown_button.pin }}">
              </label>
            </div>
            <div>
              <label>Hold time to trigger (seconds)
                <input name="shutdown_hold_secs" value="{{ cfg.shutdown_button.hold_secs }}">
              </label>
            </div>
          </div>
          <small>Setup will ask you to press and hold the button for real (GPIO is checked directly, not just
          asked about) before installing the always-on watcher service.</small>
        </fieldset>

        <button type="submit" id="apply-btn">Apply &amp; Set Up</button>
      </form>
    </div>

    <div class="card">
      <h2 style="margin-top:0;">Progress</h2>
      <ul id="steps">
        {% for id, label in step_order %}
          <li data-step="{{ id }}"><span class="badge pending">•</span><span class="label">{{ label }}</span></li>
        {% endfor %}
      </ul>
      <div id="console"></div>
    </div>
  </div>

  <div id="confirm-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.6);
       align-items:center; justify-content:center; z-index:50;">
    <div style="background:#1f2937; padding:24px; border-radius:16px; max-width:420px; text-align:center;">
      <p id="confirm-question" style="font-size:16px; margin-bottom:18px;"></p>
      <div style="display:flex; gap:12px; justify-content:center;">
        <button id="confirm-yes" type="button">Yes</button>
        <button id="confirm-no" type="button" style="background:#4b5563; color:#f9fafb;">No / skip</button>
      </div>
    </div>
  </div>

<script>
const STEP_LABELS = {{ step_order_json|safe }};
const badgeChar = {pending:"•", running:"…", done:"✓", error:"✕", skipped:"–"};
let es = null;

function setBadge(stepId, status) {
  const li = document.querySelector(`li[data-step="${stepId}"]`);
  if (!li) return;
  const badge = li.querySelector(".badge");
  badge.className = "badge " + status;
  badge.textContent = badgeChar[status] || "•";
}

function appendLine(text, cls) {
  const c = document.getElementById("console");
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text;
  c.appendChild(div);
  c.scrollTop = c.scrollHeight;
}

function showBanner(ok) {
  const b = document.getElementById("banner");
  b.className = ok ? "ok" : "fail";
  b.textContent = ok ? "Setup finished successfully." : "Setup stopped due to an error — see the console below.";
}

function showConfirm(data) {
  document.getElementById("confirm-question").textContent = data.question;
  document.getElementById("confirm-yes").textContent = data.yes_label || "Yes";
  document.getElementById("confirm-no").textContent = data.no_label || "No / skip";
  document.getElementById("confirm-overlay").style.display = "flex";

  const answer = (val) => {
    document.getElementById("confirm-overlay").style.display = "none";
    fetch("/setup/confirm", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirm_id: data.confirm_id, answer: val}),
    });
  };
  document.getElementById("confirm-yes").onclick = () => answer(true);
  document.getElementById("confirm-no").onclick = () => answer(false);
}

function connectStream() {
  if (es) es.close();
  es = new EventSource("/setup/stream");
  es.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    if (data.kind === "step") {
      setBadge(data.step, data.status);
    } else if (data.kind === "cmd") {
      appendLine(data.text, "line-cmd");
    } else if (data.kind === "log") {
      appendLine(data.text, data.level === "error" ? "line-error" : "");
    } else if (data.kind === "confirm_request") {
      showConfirm(data);
    } else if (data.kind === "done") {
      document.getElementById("apply-btn").disabled = false;
      document.getElementById("apply-btn").textContent = "Apply & Set Up";
      document.getElementById("confirm-overlay").style.display = "none";
      showBanner(data.ok);
    } else if (data.kind === "start") {
      document.getElementById("console").innerHTML = "";
      document.getElementById("banner").style.display = "none";
      document.getElementById("confirm-overlay").style.display = "none";
    }
  };
}

document.getElementById("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("apply-btn");
  btn.disabled = true;
  btn.textContent = "Running...";
  document.getElementById("banner").style.display = "none";
  Object.keys(STEP_LABELS).forEach(id => setBadge(id, "pending"));

  const fd = new FormData(e.target);
  const payload = {};
  for (const [k, v] of fd.entries()) payload[k] = v;
  payload.format_device = fd.has("format_device");
  payload.install_audiobookshelf = fd.has("install_audiobookshelf");
  payload.install_calibre_web = fd.has("install_calibre_web");
  payload.enable_sync = fd.has("enable_sync");
  payload.server_username = fd.get("server_username") || "";
  payload.server_password = fd.get("server_password") || "";
  payload.leds_enabled = fd.has("leds_enabled");
  payload.shutdown_enabled = fd.has("shutdown_enabled");

  connectStream();

  const resp = await fetch("/setup/apply", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({error: resp.statusText}));
    appendLine("Failed to start: " + (err.error || resp.statusText), "line-error");
    btn.disabled = false;
    btn.textContent = "Apply & Set Up";
  }
});

// Reconnect to an in-progress run on page load/refresh.
fetch("/setup/state").then(r => r.json()).then(state => {
  Object.entries(state.steps).forEach(([id, status]) => setBadge(id, status));
  if (state.running) {
    document.getElementById("apply-btn").disabled = true;
    document.getElementById("apply-btn").textContent = "Running...";
    connectStream();
    if (state.pending_confirm) showConfirm(state.pending_confirm);
  } else if (state.finished) {
    showBanner(state.ok);
  }
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return redirect(url_for("setup"))


@app.route("/setup", methods=["GET"])
def setup():
    cfg = load_config()
    devices = get_storage_devices()
    board = load_board_info()
    return render_template_string(
        FORM_TEMPLATE,
        cfg=cfg,
        devices=devices,
        board=board,
        step_order=STEP_ORDER,
        step_order_json=json.dumps({sid: label for sid, label in STEP_ORDER}),
    )


@app.route("/setup/apply", methods=["POST"])
def setup_apply():
    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"error": "Setup is already running."}), 409

    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()

    cfg["wifi"]["ssid"] = (data.get("wifi_ssid") or "").strip() or cfg["wifi"]["ssid"]
    cfg["wifi"]["password"] = (data.get("wifi_password") or "").strip() or cfg["wifi"]["password"]
    cfg["wifi"]["ip"] = (data.get("wifi_ip") or "").strip() or cfg["wifi"]["ip"]

    cfg["storage"]["media_root"] = (data.get("media_root") or "").strip() or cfg["storage"]["media_root"]
    storage_device = (data.get("storage_device") or "").strip()
    format_device = bool(data.get("format_device"))

    cfg["apps"]["install_audiobookshelf"] = bool(data.get("install_audiobookshelf"))
    cfg["apps"]["install_calibre_web"] = bool(data.get("install_calibre_web"))

    cfg["sync"]["enable_sync"] = bool(data.get("enable_sync"))
    cfg["sync"]["server_ip"] = (data.get("server_ip") or "").strip() or cfg["sync"]["server_ip"]
    cfg["sync"]["server_username"] = (data.get("server_username") or "").strip()
    cfg["sync"]["server_password"] = data.get("server_password") or ""
    cfg["sync"]["server_path_audio"] = (data.get("server_path_audio") or "").strip() or cfg["sync"]["server_path_audio"]
    cfg["sync"]["server_path_books"] = (data.get("server_path_books") or "").strip() or cfg["sync"]["server_path_books"]

    cfg["leds"]["enabled"] = bool(data.get("leds_enabled"))
    for key, field in (("pin_wifi", "pin_wifi"), ("pin_cweb", "pin_cweb"), ("pin_abs", "pin_abs")):
        try:
            cfg["leds"][key] = int(data.get(field, cfg["leds"][key]))
        except (TypeError, ValueError):
            pass  # keep existing pin if the field wasn't a valid int

    cfg["shutdown_button"]["enabled"] = bool(data.get("shutdown_enabled"))
    try:
        cfg["shutdown_button"]["pin"] = int(data.get("shutdown_pin", cfg["shutdown_button"]["pin"]))
    except (TypeError, ValueError):
        pass
    try:
        cfg["shutdown_button"]["hold_secs"] = float(data.get("shutdown_hold_secs", cfg["shutdown_button"]["hold_secs"]))
    except (TypeError, ValueError):
        pass

    save_config(cfg)

    thread = threading.Thread(target=run_pipeline, args=(cfg, storage_device, format_device), daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/setup/state", methods=["GET"])
def setup_state():
    pending_confirm = None
    with EVENT_LOCK:
        recent_confirm_requests = [e for e in reversed(EVENTS) if e["kind"] == "confirm_request"]
    if recent_confirm_requests:
        latest = recent_confirm_requests[0]
        with CONFIRM_LOCK:
            if latest["confirm_id"] in CONFIRMS:
                pending_confirm = latest
    with STATE_LOCK:
        return jsonify(
            {
                "running": STATE["running"],
                "finished": STATE["finished"],
                "ok": STATE["ok"],
                "steps": dict(STATE["steps"]),
                "pending_confirm": pending_confirm,
            }
        )


@app.route("/setup/confirm", methods=["POST"])
def setup_confirm():
    data = request.get_json(force=True, silent=True) or {}
    confirm_id = data.get("confirm_id")
    answer = bool(data.get("answer"))
    with CONFIRM_LOCK:
        entry = CONFIRMS.get(confirm_id)
        if not entry:
            return jsonify({"error": "unknown or already-answered confirmation"}), 404
        entry["answer"] = answer
        entry["event"].set()
    return jsonify({"ok": True})


@app.route("/setup/stream")
def setup_stream():
    last_id = request.args.get("after", type=int, default=0)

    def gen():
        nonlocal last_id
        idle = 0
        while True:
            with EVENT_LOCK:
                new_events = [e for e in EVENTS if e["id"] > last_id]
            if new_events:
                for ev in new_events:
                    last_id = ev["id"]
                    yield f"data: {json.dumps(ev)}\n\n"
                idle = 0
            else:
                idle += 1
                # heartbeat so proxies/browsers don't close the connection
                if idle % 20 == 0:
                    yield ": heartbeat\n\n"
                time.sleep(0.25)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, threaded=True)
