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
import base64
import io
import itertools
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, request, render_template_string, redirect, url_for, jsonify, send_file
import requests
import yaml

app = Flask(__name__)

INSTALL_DIR = Path("/opt/lustylibrary-installer")
CONFIG_PATH = INSTALL_DIR / "config.yml"
BOARD_INFO_PATH = INSTALL_DIR / "board_info.json"
LOGO_PATH = Path(__file__).resolve().parent / "lusty_library_logo.png"


def load_logo_data_uri():
    """Loads the Lusty Library logo as an inline base64 data URI so every
    page (setup wizard, request page, welcome page) can embed it without
    a separate static-file route. Returns "" if the logo is missing so
    pages degrade gracefully (no broken image) instead of failing."""
    try:
        data = LOGO_PATH.read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


LOGO_DATA_URI = load_logo_data_uri()

# A little book emoji, baked into an inline SVG, used only as the browser-tab
# favicon. The full Lusty Library logo image is the header's main visual on
# every page; this just gives the tab itself a recognizable icon.
FAVICON_DATA_URI = "data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>%F0%9F%93%9A</text></svg>"


def generate_qr_data_uri(data):
    """Generates a QR code as an inline PNG data URI, entirely offline (no
    external QR-code API). Returns "" if the optional `qrcode` package
    isn't installed or generation fails, so callers can skip the image
    instead of breaking the page. PNG (not SVG) so the same image embeds
    cleanly in both the live HTML page and the generated PDF."""
    try:
        import qrcode
    except ImportError:
        return ""
    try:
        img = qrcode.make(data, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception:
        return ""
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# QR code cache
#
# The `qrcode` package is only ever needed once per install, to bake a
# handful of QR images into the welcome page/PDF. Rather than keep it
# installed forever, the "qr_codes" pipeline step installs it, generates
# and verifies every QR code this install needs, saves them to this cache
# file, then uninstalls the package again. render_welcome_page() (and the
# /welcome, /welcome.pdf routes) read from the cache so the welcome page
# keeps working correctly even after `qrcode` is gone and even across a
# service restart.
# ---------------------------------------------------------------------------

QR_CACHE_PATH = INSTALL_DIR / "qr_cache.json"


def load_qr_cache():
    if QR_CACHE_PATH.exists():
        try:
            return json.loads(QR_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_qr_cache(cache):
    QR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QR_CACHE_PATH.write_text(json.dumps(cache))


def get_cached_qr(cache, key, data):
    """Returns the cached QR data URI for `key` if present, otherwise
    falls back to generating one on the fly (works if `qrcode` happens to
    still be installed; returns "" otherwise so the page just omits it)."""
    return cache.get(key) or generate_qr_data_uri(data)


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
        "patron_username": "book",
        "patron_password": "book",
        # filled in by the "accounts" step at run time — not user-editable
        "calibre_account_status": "not attempted",
        "audiobookshelf_account_status": "not attempted",
    },
    "sync": {
        "enable_sync": False,
        # left blank by default — this isn't pre-filled with anyone's real
        # network info; the field's placeholder shows an example instead
        "server_ip": "",
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
    "requests_page": {
        "enabled": True,
        "port": 5000,
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
    ("accounts", "Creating patron accounts"),
    ("requests_page", "Setting up the book request page"),
    ("sync", "Configuring auto-sync"),
    ("trigger", "Setting up Ethernet plug-in trigger"),
    ("leds_service", "Installing status LED service"),
    ("shutdown_button", "Setting up shutdown button"),
    ("qr_codes", "Generating QR codes"),
    ("welcome_page", "Generating welcome page & PDF"),
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


def validate_wifi_settings(cfg):
    """WPA2-PSK requires an 8-63 character passphrase and a 1-32 byte SSID;
    NetworkManager and wpa_supplicant both reject anything outside those
    ranges with a fairly cryptic "property is invalid" error buried deep in
    the pipeline. Catch it up front with a plain-English message instead."""
    errors = []
    ssid = cfg["wifi"]["ssid"]
    password = cfg["wifi"]["password"]
    ssid_len = len(ssid.encode("utf-8"))
    if not (1 <= ssid_len <= 32):
        errors.append(f"Wi-Fi network name must be 1-32 characters (\"{ssid}\" is {ssid_len}).")
    if not (8 <= len(password) <= 63):
        errors.append(
            f"Wi-Fi password must be 8-63 characters — this is a WPA2 requirement, not "
            f"something Lusty Library can relax (\"{password}\" is {len(password)})."
        )
    return errors


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
    errors = validate_wifi_settings(cfg)
    if errors:
        # setup_apply() already rejects these before the pipeline ever
        # starts, but this is the same check as a backstop in case
        # config.yml was hand-edited directly (or an old one carried over).
        raise StepFailed(" ".join(errors))

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
# patron account auto-provisioning (Calibre-Web / Audiobookshelf)
#
# Best-effort: these talk to each app's own web server over HTTP to create
# the patron account shown on the welcome page, using the real login/setup
# forms each app already serves (not a guessed private API), so a login or
# form field that changes in a future app version fails loudly in the log
# rather than silently. Never raises StepFailed — a login page you can't
# script around isn't a reason to abort the whole install.
# ---------------------------------------------------------------------------

CALIBRE_DEFAULT_ADMIN_USER = "admin"
CALIBRE_DEFAULT_ADMIN_PASSWORD = "admin123"  # linuxserver/calibre-web's documented first-run default
CALIBRE_LIBRARY_PATH_IN_CONTAINER = "/books"  # matches the volume mount in generate_docker_compose()
CALIBRE_EMPTY_LIBRARY_SCHEMA_PATH = Path(__file__).resolve().parent / "calibre_empty_library.sql"


def ensure_calibre_library(cfg, step_id):
    """Calibre-Web's first-run 'Database Configuration' step refuses to
    accept a library path unless a *valid* metadata.db already exists
    there — it never creates one itself (confirmed by reading Calibre-Web's
    own source). So before that step can ever be automated, make sure a
    genuine, empty Calibre library is already sitting at the configured
    books path. Never touches anything if a metadata.db is already there
    (a re-run, or a real library someone dropped in) — this only ever
    creates one from nothing."""
    books_dir = Path(cfg["storage"]["media_root"]) / "books"
    metadata_db = books_dir / "metadata.db"
    if metadata_db.exists():
        log_line(step_id, "Calibre library already exists, leaving it as-is.")
        return

    try:
        books_dir.mkdir(parents=True, exist_ok=True)
        schema_sql = CALIBRE_EMPTY_LIBRARY_SCHEMA_PATH.read_text()
        conn = sqlite3.connect(str(metadata_db))
        try:
            conn.executescript(schema_sql)
            conn.execute("UPDATE library_id SET uuid = ?", (str(uuid.uuid4()),))
            conn.commit()
        finally:
            conn.close()
        log_line(step_id, f"Created a fresh, empty Calibre library at {metadata_db}.")
    except Exception as e:  # noqa: BLE001 - best effort; Calibre-Web will just show its own setup wizard
        log_line(step_id, f"Couldn't create an empty Calibre library ({e}); you may need to complete "
                           "Calibre-Web's Database Configuration step by hand.", level="error")


def _wait_for_http(url, step_id, timeout=90):
    """Polls a URL until it responds (any status code counts — we just
    need the app's web server to be up), or gives up after `timeout`s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=5)
            return True
        except requests.RequestException:
            time.sleep(2)
    log_line(step_id, f"Timed out waiting for {url} to respond.", level="error")
    return False


def _extract_csrf_token(html):
    """Pulls a Flask-WTF style hidden csrf_token field out of an HTML
    form, if present. Returns None if there isn't one (some setups run
    without CSRF protection)."""
    import re

    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def _try_provision_calibre_web_account(base, username, password):
    """One attempt at the login-then-create-user flow. Raises on any
    problem (bad login, session not actually authenticated, HTTP error)
    so the caller can retry — Calibre-Web's own first-run database setup
    (seeding the default admin user) can still be finishing even after its
    web server starts responding, especially on slower boards."""
    session = requests.Session()
    login_page = session.get(f"{base}/login", timeout=10)
    token = _extract_csrf_token(login_page.text)
    login_data = {"username": CALIBRE_DEFAULT_ADMIN_USER, "password": CALIBRE_DEFAULT_ADMIN_PASSWORD}
    if token:
        login_data["csrf_token"] = token
    resp = session.post(f"{base}/login", data=login_data, timeout=10)
    if "login" in resp.url and resp.status_code == 200:
        raise RuntimeError("login page still shown after posting default admin credentials")

    # Calibre-Web won't let ANY other admin route through until its own
    # one-time "Database Configuration" step is done — it redirects every
    # request back to /admin/dbconfig until a library path is set. Drive
    # that same form the setup wizard itself would show you, every time;
    # if it's already configured this is just a harmless re-save of the
    # same value. ensure_calibre_library() (called earlier, in the "apps"
    # step) guarantees a valid empty library already exists at this path,
    # since Calibre-Web will refuse the path otherwise and never creates
    # one on its own.
    dbconfig_page = session.get(f"{base}/admin/dbconfig", timeout=10)
    token = _extract_csrf_token(dbconfig_page.text)
    dbconfig_form = {"config_calibre_dir": CALIBRE_LIBRARY_PATH_IN_CONTAINER}
    if token:
        dbconfig_form["csrf_token"] = token
    resp = session.post(f"{base}/admin/dbconfig", data=dbconfig_form, timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} setting the Calibre library path")

    new_user_page = session.get(f"{base}/admin/user/new", timeout=10)
    if "user/new" not in new_user_page.url:
        raise RuntimeError(
            f"admin page redirected to {new_user_page.url} instead of the new-user form "
            "(Calibre-Web still doesn't consider itself configured)"
        )
    token = _extract_csrf_token(new_user_page.text)
    form = {
        "name": username,
        "email": f"{username}@lustylibrary.local",
        "password": password,
        "kobo_support": "",
        "download_role": "on",
        "viewer_role": "on",
        # Calibre-Web's own new-user handler reads to_save["default_language"]
        # with a plain dict lookup (no default) — omitting it throws an
        # uncaught KeyError server-side, which is the HTTP 500 this was
        # producing. "all" is the built-in "Show All" option, always valid
        # regardless of what's in the library. "locale" (UI language) has a
        # safe fallback in Calibre-Web itself, but we set it explicitly too
        # rather than lean on that.
        "default_language": "all",
        "locale": "en",
    }
    if token:
        form["csrf_token"] = token
    resp = session.post(f"{base}/admin/user/new", data=form, timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} creating user")
    if "user/new" in resp.url and "Oops" in resp.text:
        # Calibre-Web returns 200 and re-renders the same "Add New User"
        # form (rather than redirecting to the admin page) when the fields
        # it got fail its own validation, e.g. a name/email collision from
        # a previous attempt — raise so this is visible and retried/reported
        # instead of being mistaken for success.
        raise RuntimeError("Calibre-Web rejected the new-user form (see its own error/flash message)")


def provision_calibre_web_account(cfg, step_id):
    """Logs into Calibre-Web with its documented first-run admin account
    and creates (or updates) the patron account shown on the welcome page,
    via the same /admin/user/new form the web UI itself uses."""
    base = "http://127.0.0.1:8083"
    username = cfg["apps"]["patron_username"]
    password = cfg["apps"]["patron_password"]

    if not _wait_for_http(base, step_id):
        cfg["apps"]["calibre_account_status"] = "failed: Calibre-Web never came up"
        return

    # Calibre-Web's web server can start answering requests before its own
    # first-run setup (seeding the default admin user in its database) has
    # actually finished — on a slower board like a Pi 3 that can take a
    # while. Retry the whole flow a few times instead of giving up after
    # one attempt right after the container starts.
    attempts = 6
    delay_secs = 15
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            _try_provision_calibre_web_account(base, username, password)
            log_line(step_id, f"Calibre-Web patron account '{username}' created.")
            cfg["apps"]["calibre_account_status"] = "created"
            return
        except Exception as e:  # noqa: BLE001 - best effort, retry a few times then give up gracefully
            last_error = e
            if attempt < attempts:
                log_line(
                    step_id,
                    f"Calibre-Web account setup attempt {attempt}/{attempts} didn't work yet ({e}); "
                    f"retrying in {delay_secs}s — Calibre-Web can take a while to finish its own "
                    "first-run setup.",
                )
                time.sleep(delay_secs)

    log_line(
        step_id,
        f"Couldn't auto-create the Calibre-Web patron account after {attempts} attempts: {last_error}. "
        "You can create it by hand at http://<pi-ip>:8083/admin/user/new (default admin login: "
        f"{CALIBRE_DEFAULT_ADMIN_USER}/{CALIBRE_DEFAULT_ADMIN_PASSWORD}).",
        level="error",
    )
    cfg["apps"]["calibre_account_status"] = f"failed: {last_error}"


def provision_audiobookshelf_account(cfg, step_id):
    """Sets up Audiobookshelf's one-time root account as the patron
    account, via the same /status + /init flow its own first-run setup
    screen uses. If the server was already initialized (e.g. re-running
    setup), it just confirms those credentials still log in."""
    base = "http://127.0.0.1:13378"
    username = cfg["apps"]["patron_username"]
    password = cfg["apps"]["patron_password"]

    if not _wait_for_http(base, step_id):
        cfg["apps"]["audiobookshelf_account_status"] = "failed: Audiobookshelf never came up"
        return

    try:
        status = requests.get(f"{base}/status", timeout=10).json()
        if not status.get("isInit", True):
            resp = requests.post(
                f"{base}/init",
                json={"newRoot": {"username": username, "password": password}},
                timeout=10,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} initializing server")
            log_line(step_id, f"Audiobookshelf root/patron account '{username}' created.")
            cfg["apps"]["audiobookshelf_account_status"] = "created"
        else:
            # Already initialized (e.g. re-run) — just confirm we can log in.
            resp = requests.post(f"{base}/login", json={"username": username, "password": password}, timeout=10)
            if resp.status_code == 200:
                log_line(step_id, f"Audiobookshelf already set up; '{username}' logs in fine.")
                cfg["apps"]["audiobookshelf_account_status"] = "already exists"
            else:
                log_line(
                    step_id,
                    "Audiobookshelf is already initialized with different credentials — "
                    "log in as the existing root user to manage accounts.",
                    level="error",
                )
                cfg["apps"]["audiobookshelf_account_status"] = "failed: already initialized with other credentials"
    except Exception as e:  # noqa: BLE001 - best effort, never fail the install over this
        log_line(step_id, f"Couldn't auto-create the Audiobookshelf account: {e}", level="error")
        cfg["apps"]["audiobookshelf_account_status"] = f"failed: {e}"


def provision_accounts(cfg, step_id):
    if cfg["apps"]["install_calibre_web"]:
        provision_calibre_web_account(cfg, step_id)
    else:
        cfg["apps"]["calibre_account_status"] = "not installed"

    if cfg["apps"]["install_audiobookshelf"]:
        provision_audiobookshelf_account(cfg, step_id)
    else:
        cfg["apps"]["audiobookshelf_account_status"] = "not installed"

    save_config(cfg)


# ---------------------------------------------------------------------------
# book/audiobook request page (port 5000)
# ---------------------------------------------------------------------------

REQUESTS_SCRIPT_PATH = Path("/usr/local/bin/library_requests.py")
REQUESTS_SERVICE_PATH = Path("/etc/systemd/system/lustylibrary-requests.service")

REQUESTS_PAGE_TEMPLATE = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Request a Book</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {% if favicon_data_uri %}<link rel="icon" href="{{ favicon_data_uri }}">{% endif %}
  <style>
    body { font-family: system-ui, sans-serif; background:#111827; color:#f9fafb; margin:0; }
    .wrap { max-width:900px; margin:4vh auto; padding:0 16px 40px; }
    .card { background:#1f2937; padding:24px; border-radius:16px; box-shadow:0 10px 40px rgba(0,0,0,.6); margin-bottom:20px; }
    h1 { margin-top:0; }
    label { display:block; margin:8px 0; }
    input,select,textarea { width:100%; padding:8px; border-radius:6px; border:1px solid #4b5563;
                   background:#030712; color:#e5e7eb; box-sizing:border-box; font-family:inherit; }
    .row { display:flex; gap:12px; }
    .row > div { flex:1; }
    button { background:#10b981; color:#022c22; border:0; padding:10px 18px; border-radius:999px;
             font-weight:600; cursor:pointer; margin-top:10px; font-size:15px; }
    button:hover { background:#059669; }
    button.secondary { background:#374151; color:#e5e7eb; padding:4px 12px; font-size:13px; margin:0; }
    button.secondary:hover { background:#4b5563; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th, td { text-align:left; padding:8px 6px; border-bottom:1px solid #374151; vertical-align:top; }
    th { color:#9ca3af; font-weight:600; }
    .badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
    .badge.pending { background:#78350f; color:#fde68a; }
    .badge.fulfilled { background:#064e3b; color:#a7f3d0; }
    .empty { color:#9ca3af; font-style:italic; }
    small { color:#9ca3af; }
    .brand { display:flex; align-items:center; gap:14px; margin-bottom:4px; }
    .brand img { height:96px; width:auto; }
    .brand h1 { margin:0; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="brand">
        {% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="Lusty Library logo">{% endif %}
        <h1>Request a Book or Audiobook</h1>
      </div>
      <p><small>Can't find something in the library? Ask for it here.</small></p>
      <form method="post" action="/request">
        <label>Title *
          <input name="title" required placeholder="e.g. Project Hail Mary">
        </label>
        <div class="row">
          <div>
            <label>Author
              <input name="author" placeholder="e.g. Andy Weir">
            </label>
          </div>
          <div>
            <label>Type
              <select name="media_type">
                <option value="Audiobook">Audiobook</option>
                <option value="Ebook">Ebook</option>
              </select>
            </label>
          </div>
        </div>
        <label>Your name <small>(optional)</small>
          <input name="requested_by" placeholder="so we know who to tell">
        </label>
        <label>Notes <small>(optional)</small>
          <textarea name="notes" rows="2" placeholder="edition, narrator, series, etc."></textarea>
        </label>
        <button type="submit">Request It</button>
      </form>
    </div>

    <div class="card">
      <h2 style="margin-top:0;">Requests</h2>
      {% if rows %}
      <table>
        <tr>
          <th>Status</th><th>Title</th><th>Author</th><th>Type</th><th>Requested by</th><th>Notes</th><th>When</th><th></th>
        </tr>
        {% for r in rows %}
        <tr>
          <td><span class="badge {{ 'fulfilled' if r.status == 'Fulfilled' else 'pending' }}">{{ r.status }}</span></td>
          <td>{{ r.title }}</td>
          <td>{{ r.author }}</td>
          <td>{{ r.media_type }}</td>
          <td>{{ r.requested_by }}</td>
          <td>{{ r.notes }}</td>
          <td>{{ r.timestamp }}</td>
          <td>
            <form method="post" action="/toggle/{{ r.id }}" style="margin:0;">
              <button type="submit" class="secondary">{{ 'Mark pending' if r.status == 'Fulfilled' else 'Mark fulfilled' }}</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="empty">No requests yet.</p>
      {% endif %}
    </div>
  </div>
</body>
</html>
'''


def generate_requests_page_script(cfg):
    media_root = cfg["storage"]["media_root"]
    csv_path = str(Path(media_root) / "requests.csv")
    port = cfg["requests_page"]["port"]
    logo_data_uri = LOGO_DATA_URI
    favicon_data_uri = FAVICON_DATA_URI

    return f'''#!/usr/bin/env python3
"""
Lusty Library — book/audiobook request page.
Generated by the Lusty Library setup wizard.
"""
import csv
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, redirect, url_for, render_template_string

app = Flask(__name__)

CSV_PATH = Path({csv_path!r})
FIELDNAMES = ["id", "timestamp", "title", "author", "media_type", "requested_by", "notes", "status"]
LOCK = threading.Lock()

PAGE = {REQUESTS_PAGE_TEMPLATE!r}
LOGO_DATA_URI = {logo_data_uri!r}
FAVICON_DATA_URI = {favicon_data_uri!r}


def read_requests():
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open("r", newline="") as f:
        return list(csv.DictReader(f))


def write_requests(rows):
    tmp_path = CSV_PATH.with_suffix(".tmp")
    with tmp_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(CSV_PATH)


def append_request(row):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        rows = read_requests()
        rows.append(row)
        write_requests(rows)


def set_status(request_id, status):
    with LOCK:
        rows = read_requests()
        for r in rows:
            if r.get("id") == request_id:
                r["status"] = status
        write_requests(rows)


@app.route("/", methods=["GET"])
def index():
    rows = read_requests()
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return render_template_string(PAGE, rows=rows, logo_data_uri=LOGO_DATA_URI, favicon_data_uri=FAVICON_DATA_URI)


@app.route("/request", methods=["POST"])
def submit_request():
    title = (request.form.get("title") or "").strip()
    if title:
        row = {{
            "id": uuid.uuid4().hex[:8],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "title": title,
            "author": (request.form.get("author") or "").strip(),
            "media_type": request.form.get("media_type") or "Audiobook",
            "requested_by": (request.form.get("requested_by") or "").strip(),
            "notes": (request.form.get("notes") or "").strip(),
            "status": "Pending",
        }}
        append_request(row)
    return redirect(url_for("index"))


@app.route("/toggle/<request_id>", methods=["POST"])
def toggle_status(request_id):
    rows = read_requests()
    current = next((r for r in rows if r.get("id") == request_id), None)
    if current:
        new_status = "Pending" if current.get("status") == "Fulfilled" else "Fulfilled"
        set_status(request_id, new_status)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port={port})
'''


def install_requests_page(cfg, step_id):
    if not cfg["requests_page"]["enabled"]:
        log_line(step_id, "Book request page disabled, skipping.")
        return

    port = cfg["requests_page"]["port"]
    media_root = cfg["storage"]["media_root"]

    script = generate_requests_page_script(cfg)
    write_file(REQUESTS_SCRIPT_PATH, script, step_id, mode=0o755)

    service_content = f"""[Unit]
Description=Lusty Library book/audiobook request page (port {port})
After=network.target
RequiresMountsFor={media_root}

[Service]
Type=simple
ExecStart=/usr/bin/env python3 {REQUESTS_SCRIPT_PATH}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    write_file(REQUESTS_SERVICE_PATH, service_content, step_id)
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "--now", "lustylibrary-requests.service"], step_id, allow_fail=True)

    # Flask/systemd can take a moment to actually bind the port after
    # "enable --now" returns, so poll for a few seconds instead of checking
    # once immediately — a single early check can report a false failure
    # for a service that comes up fine a moment later.
    up = _wait_for_http(f"http://127.0.0.1:{port}/", step_id, timeout=15)
    if up:
        log_line(step_id, f"Request page responding on port {port}.")
    else:
        log_line(
            step_id,
            f"Couldn't confirm the request page is responding on port {port} — "
            "check 'sudo systemctl status lustylibrary-requests.service'.",
            level="error",
        )
    log_line(step_id, f"Requests are saved to {Path(media_root) / 'requests.csv'}.")


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
    except ImportError:
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
        except ImportError:
            log_line(step_id, "Could not import gpiozero after install attempt.", level="error")
            return False

    # `import gpiozero` succeeding doesn't prove there's a *working* GPIO
    # backend behind it — on newer Raspberry Pi OS releases (Bookworm and
    # trixie), the classic RPi.GPIO module can be present without being
    # able to actually drive a pin on the new kernel GPIO interface, which
    # needs the lgpio-backed replacement instead. Resolving (and logging)
    # the real backend here means a "no error, but nothing lit up" failure
    # later already has its answer sitting in this log, instead of only
    # showing up once someone SSHes in to dig further.
    try:
        import gpiozero

        gpiozero.Device.ensure_pin_factory()
        factory = gpiozero.Device.pin_factory
        factory_name = f"{type(factory).__module__}.{type(factory).__name__}"
        log_line(step_id, f"GPIO backend in use: {factory_name}")
        if "mock" in factory_name.lower():
            log_line(
                step_id,
                "gpiozero is using its MOCK pin factory — it reports success without touching "
                "real hardware. Something has GPIOZERO_PIN_FACTORY=mock set in the environment; "
                "LEDs/the shutdown button won't work until that's removed.",
                level="error",
            )
            return False
    except Exception as e:  # noqa: BLE001 - report clearly rather than let a later, vaguer failure happen
        log_line(
            step_id,
            f"gpiozero imported, but couldn't set up a working GPIO backend: {e}. On newer "
            "Raspberry Pi OS releases this usually means the lgpio backend didn't install "
            "correctly — try 'sudo apt-get install -y python3-rpi-lgpio' or "
            "'sudo pip3 install --break-system-packages rpi-lgpio' by hand, then re-run setup.",
            level="error",
        )
        return False

    return True


def blink_led(pin, step_id, label, times=3, on_time=0.2, off_time=0.2):
    """Flashes a pin a few times so it's easy to spot which physical LED
    corresponds to which pin/feature, then leaves it ON and returns the
    still-open LED object. The caller is responsible for turning it off
    (via _release_led) once done with it — turning it off right after the
    flash, before the person has even seen the confirm question, defeats
    the point of a visual test: by the time the browser round-trips the
    question to them, the LED would already look dark either way. Returns
    None (already logged) if the pin can't be driven at all."""
    try:
        from gpiozero import LED
    except ImportError:
        log_line(step_id, "gpiozero not available, cannot test LEDs.", level="error")
        return None

    log_line(step_id, f"Blinking {label} LED on GPIO{pin} ({times}x), then holding it on...")
    try:
        led = LED(pin)
        for _ in range(times):
            led.on()
            time.sleep(on_time)
            led.off()
            time.sleep(off_time)
        led.on()
        return led
    except Exception as e:  # noqa: BLE001 - bad wiring/pin shouldn't kill setup
        log_line(step_id, f"Could not drive GPIO{pin} ({label}): {e}", level="error")
        return None


def _release_led(led):
    """Turns off and releases an LED object returned by blink_led(), if any."""
    if led is None:
        return
    try:
        led.off()
        led.close()
    except Exception:  # noqa: BLE001 - best effort cleanup, never worth failing setup over
        pass


def run_led_test_screen(cfg, step_id):
    """The general 'test screen': blinks every configured LED once, in
    sequence, then holds all three on solid while asking a single yes/no
    question, so what's on the board still matches what's being asked
    about instead of going dark before the person can answer."""
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
    held = [
        blink_led(leds["pin_wifi"], step_id, "Wi-Fi", times=2),
        blink_led(leds["pin_cweb"], step_id, "Calibre-Web", times=2),
        blink_led(leds["pin_abs"], step_id, "Audiobookshelf", times=2),
    ]
    try:
        answer = ask_confirm(
            step_id,
            f"All three LEDs (GPIO{leds['pin_wifi']}, GPIO{leds['pin_cweb']}, GPIO{leds['pin_abs']}) "
            "should be lit solid right now — are they?",
        )
        if answer is False:
            log_line(
                step_id,
                "Check wiring/pin numbers in the form above and re-run setup. "
                "Continuing installation regardless — LEDs are cosmetic.",
                level="error",
            )
    finally:
        for led in held:
            _release_led(led)


def test_feature_led(cfg, step_id, pin, feature_label):
    """Called right after a specific feature (Wi-Fi, Calibre-Web,
    Audiobookshelf) comes up, so the test is tied to the thing it
    indicates rather than only run once up front. Holds the LED on solid
    through the confirm question, then releases it either way."""
    if not cfg["leds"]["enabled"] or not gpio_available():
        return
    led = blink_led(pin, step_id, feature_label, times=3)
    try:
        answer = ask_confirm(step_id, f"The {feature_label} LED (GPIO{pin}) should be lit solid right now — is it?")
        if answer is False:
            log_line(step_id, f"{feature_label} LED not confirmed working — check GPIO{pin} wiring.", level="error")
    finally:
        _release_led(led)


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

    # Poll the raw pin state directly in this thread instead of relying on
    # gpiozero's when_pressed/when_released event callbacks. Those callbacks
    # depend on a background watcher thread of gpiozero's own getting set
    # up correctly, which has proven unreliable specifically when this test
    # runs inside the setup wizard's own background pipeline thread (this
    # exact button works fine right after a reboot, once its own standalone
    # systemd service runs it directly as the process's main thread) —
    # polling is_pressed sidesteps that distinction entirely.
    try:
        btn = Button(pin, pull_up=True, bounce_time=0.05)
    except Exception as e:  # noqa: BLE001 - bad wiring/pin shouldn't kill setup
        log_line(step_id, f"Could not read GPIO{pin}: {e}", level="error")
        return False

    held_for = None
    pressed_at = None
    was_pressed = False
    try:
        deadline = time.time() + SHUTDOWN_TEST_WINDOW_SECONDS
        while time.time() < deadline and held_for is None:
            is_pressed = btn.is_pressed
            if is_pressed and not was_pressed:
                pressed_at = time.monotonic()
            elif not is_pressed and was_pressed and pressed_at is not None:
                held_for = time.monotonic() - pressed_at
            was_pressed = is_pressed
            time.sleep(0.05)
    except Exception as e:  # noqa: BLE001 - bad wiring/pin shouldn't kill setup
        log_line(step_id, f"Could not read GPIO{pin}: {e}", level="error")
        return False
    finally:
        btn.close()

    if held_for is None:
        log_line(step_id, f"No press detected on GPIO{pin} within {SHUTDOWN_TEST_WINDOW_SECONDS}s.", level="error")
        return False

    log_line(step_id, f"Detected a press on GPIO{pin}, held for {held_for:.2f}s.")
    if held_for >= hold_secs:
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
# welcome / instructions page (generated after install)
# ---------------------------------------------------------------------------

WELCOME_HTML_NAME = "welcome.html"
WELCOME_PDF_NAME = "welcome.pdf"

# Generic, always-correct app-store search links (rather than a specific app
# ID we can't fully verify), so the QR code always lands somewhere useful.
ABS_IOS_SEARCH_URL = "https://apps.apple.com/us/search?term=audiobookshelf"
ABS_ANDROID_SEARCH_URL = "https://play.google.com/store/search?q=audiobookshelf&c=apps"

# Matches the LED legend used on the printed instruction card.
LED_COLOR_WIFI = "Green"
LED_COLOR_ABS = "Blue"
LED_COLOR_CWEB = "Yellow"

def qr_targets(cfg):
    """Every (cache key, data) pair this install needs a QR code for,
    based on what's actually enabled — kept in one place so generation
    and rendering always agree on what "the QR codes" means."""
    wifi_ip = cfg["wifi"]["ip"]
    targets = {}
    if cfg["apps"]["install_calibre_web"]:
        targets["ebook"] = f"http://{wifi_ip}:8083"
    if cfg["apps"]["install_audiobookshelf"]:
        targets["abs_ios"] = ABS_IOS_SEARCH_URL
        targets["abs_android"] = ABS_ANDROID_SEARCH_URL
    if cfg["requests_page"]["enabled"]:
        targets["requests"] = f"http://{wifi_ip}:{cfg['requests_page']['port']}"
    return targets


def generate_and_cache_qr_codes(cfg, step_id):
    """Installs `qrcode` just long enough to generate and verify every QR
    code this install needs, caches the resulting images to disk, then
    uninstalls the package again — so QR support doesn't need to be a
    permanent dependency of the installer."""
    targets = qr_targets(cfg)
    if not targets:
        log_line(step_id, "No QR codes needed for this configuration, skipping.")
        return

    rc = run_cmd(["pip3", "install", "--break-system-packages", "-q", "qrcode[pil]"], step_id, allow_fail=True)
    if rc != 0:
        # Some environments don't understand --break-system-packages; fall back.
        rc = run_cmd(["pip3", "install", "-q", "qrcode[pil]"], step_id, allow_fail=True)
    if rc != 0:
        log_line(step_id, "Couldn't install the 'qrcode' package — welcome page will skip QR images.", level="error")
        return

    cache = {}
    all_ok = True
    for key, data in targets.items():
        try:
            import qrcode

            img = qrcode.make(data, box_size=8, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            # Verify: re-open the generated image and confirm it's a real,
            # sane QR image (non-trivial size) before trusting it. If a
            # decoder happens to be available, do a real decode-and-compare
            # for extra confidence; otherwise this structural check stands
            # in for it without requiring extra system packages.
            from PIL import Image

            check_img = Image.open(io.BytesIO(png_bytes))
            if check_img.size[0] < 20 or check_img.size[1] < 20:
                raise ValueError("generated QR image looks too small to be valid")
            try:
                from pyzbar.pyzbar import decode as zbar_decode

                decoded = zbar_decode(check_img)
                if not decoded or decoded[0].data.decode("utf-8", "ignore") != data:
                    raise ValueError("decoded QR content didn't match")
                log_line(step_id, f"QR code '{key}' generated and decode-verified.")
            except ImportError:
                log_line(step_id, f"QR code '{key}' generated and looks valid ({check_img.size[0]}x{check_img.size[1]}).")

            cache[key] = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        except Exception as e:  # noqa: BLE001 - one bad QR shouldn't lose the rest
            log_line(step_id, f"QR code '{key}' failed verification, skipping it: {e}", level="error")
            all_ok = False

    save_qr_cache(cache)
    log_line(step_id, f"Cached {len(cache)}/{len(targets)} QR code(s) to {QR_CACHE_PATH}.")

    # The QR images are already safely cached above, so a failure to
    # uninstall here doesn't affect functionality — but we still check the
    # actual return code rather than assuming success, and retry with
    # --break-system-packages (the same PEP 668 "externally-managed-
    # environment" restriction that affects installs also affects
    # uninstalls on newer Debian/Raspberry Pi OS releases).
    rc = run_cmd(["pip3", "uninstall", "-y", "-q", "--break-system-packages", "qrcode"], step_id, allow_fail=True)
    if rc != 0:
        rc = run_cmd(["pip3", "uninstall", "-y", "-q", "qrcode"], step_id, allow_fail=True)
    if rc == 0:
        log_line(step_id, "Removed the 'qrcode' package again — not needed after this point.")
    else:
        log_line(
            step_id,
            "Couldn't remove the 'qrcode' package (not fatal — the QR images are already cached, "
            "so the welcome page works fine either way). You can remove it by hand later with "
            "'pip3 uninstall --break-system-packages qrcode' if you'd like.",
            level="error",
        )

    if not all_ok:
        log_line(step_id, "Some QR codes couldn't be verified; the welcome page will just show plain links for those.", level="error")


WELCOME_PAGE_TEMPLATE = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Welcome to the Lusty Library</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {% if favicon_data_uri %}<link rel="icon" href="{{ favicon_data_uri }}">{% endif %}
  <style>
    body { font-family: system-ui, sans-serif; background:#faf6f0; color:#2b2118; margin:0; }
    .wrap { max-width:760px; margin:0 auto; padding:32px 20px 48px; }
    .brand { text-align:center; margin-bottom:8px; }
    .brand img { height:120px; width:auto; }
    h1 { text-align:center; margin:4px 0 2px; font-size:28px; color:#4a2e0a; }
    .subtitle { text-align:center; color:#8a6d4a; margin:0 0 28px; }
    .card { background:#ffffff; border:1px solid #e7dcc9; padding:20px 22px; border-radius:14px;
            margin-bottom:18px; box-shadow:0 2px 10px rgba(74,46,10,.06); }
    .card h2 { margin:0 0 10px; font-size:18px; color:#4a2e0a; }
    .card p { margin:4px 0; line-height:1.5; }
    .row { display:flex; gap:20px; flex-wrap:wrap; align-items:center; }
    .qr { text-align:center; }
    .qr img { width:140px; height:140px; background:#fff; padding:6px; border-radius:8px; border:1px solid #e7dcc9; }
    .qr small { display:block; margin-top:6px; color:#8a6d4a; }
    .kv { background:#faf6f0; border-radius:8px; padding:10px 14px; font-family:ui-monospace,Menlo,Consolas,monospace;
          font-size:14px; display:inline-block; }
    .note { color:#8a6d4a; font-size:13px; }
    .led-list { list-style:none; padding:0; margin:0; }
    .led-list li { display:flex; align-items:center; gap:10px; padding:4px 0; }
    .dot { width:14px; height:14px; border-radius:50%; flex-shrink:0; display:inline-block; }
    .dot.green { background:#22c55e; }
    .dot.blue { background:#3b82f6; }
    .dot.yellow { background:#eab308; }
    @media print {
      body { background:#fff; }
      .card { box-shadow:none; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      {% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="Lusty Library logo">{% endif %}
    </div>
    <h1>Welcome to the Lusty Library</h1>
    <p class="subtitle">Everything you need to connect, read, and listen.</p>

    <div class="card">
      <h2>📶 Connect to Wi-Fi</h2>
      <p>Network name (SSID): <span class="kv">{{ wifi_ssid }}</span></p>
      <p>Password: <span class="kv">{{ wifi_password }}</span></p>
    </div>

    {% if show_ebooks %}
    <div class="card">
      <h2>📖 eBooks</h2>
      <div class="row">
        {% if ebook_qr %}
        <div class="qr"><img src="{{ ebook_qr }}" alt="eBooks QR code"><small>Scan to open</small></div>
        {% endif %}
        <div>
          <p>Or open <span class="kv">{{ ebook_url }}</span> in a browser.</p>
          {% if ebook_login_note %}<p class="note">{{ ebook_login_note }}</p>{% endif %}
        </div>
      </div>
    </div>
    {% endif %}

    {% if show_audiobooks %}
    <div class="card">
      <h2>🎧 Audiobooks</h2>
      <p>Install the free <strong>Audiobookshelf</strong> app, then add this server address:</p>
      <p><span class="kv">{{ audiobook_url }}</span></p>
      {% if audiobook_login_note %}<p class="note">{{ audiobook_login_note }}</p>{% endif %}
      <div class="row" style="margin-top:10px;">
        {% if abs_ios_qr %}
        <div class="qr"><img src="{{ abs_ios_qr }}" alt="iOS app QR code"><small>iOS</small></div>
        {% endif %}
        {% if abs_android_qr %}
        <div class="qr"><img src="{{ abs_android_qr }}" alt="Android app QR code"><small>Android</small></div>
        {% endif %}
      </div>
    </div>
    {% endif %}

    {% if show_requests %}
    <div class="card">
      <h2>🙋 Request New Books or Audiobooks</h2>
      <div class="row">
        {% if requests_qr %}
        <div class="qr"><img src="{{ requests_qr }}" alt="Request page QR code"><small>Scan to request</small></div>
        {% endif %}
        <p>Or visit <span class="kv">{{ requests_url }}</span></p>
      </div>
    </div>
    {% endif %}

    {% if show_shutdown %}
    <div class="card">
      <h2>⏻ Shutting Down</h2>
      <p>Press and hold the shutdown button for about {{ shutdown_hold_secs }} seconds. The status
      LEDs will flash a few times, then it's safe to unplug the power.</p>
    </div>
    {% endif %}

    {% if show_leds %}
    <div class="card">
      <h2>💡 LED Indicators</h2>
      <ul class="led-list">
        <li><span class="dot green"></span> Green — Wi-Fi ready</li>
        {% if show_ebooks %}<li><span class="dot yellow"></span> Yellow — eBooks ready</li>{% endif %}
        {% if show_audiobooks %}<li><span class="dot blue"></span> Blue — Audiobooks ready</li>{% endif %}
      </ul>
    </div>
    {% endif %}
  </div>
</body>
</html>
'''

# The PDF is built directly with reportlab (see render_welcome_pdf_bytes)
# rather than rendered from HTML. xhtml2pdf (an earlier approach here) pulls
# in pyHanko for PDF-signing support we don't need, which drags in
# cryptography/aiohttp/lxml — heavy, and on Debian's "externally managed"
# Python, cryptography's distro package has no pip RECORD file, which makes
# pip refuse to upgrade it even with --break-system-packages. reportlab
# alone only needs Pillow (already required) and installs cleanly.


def _account_note(status, username, password):
    """Turns a provisioning status (set by provision_accounts()) into the
    login line shown on the welcome page — sourced entirely from what the
    wizard actually configured/did, never a hand-typed note."""
    if not status or status in ("not installed", "not attempted"):
        return ""
    if status.startswith("failed"):
        reason = status.split(":", 1)[1].strip() if ":" in status else status
        return f"Username: {username} / Password: {password} (please double-check — automatic setup didn't finish: {reason})"
    return f"Username: {username} / Password: {password}"


def _welcome_context(cfg):
    """Everything both the HTML welcome page and the PDF need, computed
    once from the wizard's own config/state so the two never disagree."""
    wifi_ip = cfg["wifi"]["ip"]
    show_ebooks = bool(cfg["apps"]["install_calibre_web"])
    show_audiobooks = bool(cfg["apps"]["install_audiobookshelf"])
    show_requests = bool(cfg["requests_page"]["enabled"])
    show_shutdown = bool(cfg["shutdown_button"]["enabled"])
    show_leds = bool(cfg["leds"]["enabled"])

    username = cfg["apps"]["patron_username"]
    password = cfg["apps"]["patron_password"]

    ebook_url = f"http://{wifi_ip}:8083"
    audiobook_url = f"http://{wifi_ip}:13378"
    requests_url = f"http://{wifi_ip}:{cfg['requests_page']['port']}"

    cache = load_qr_cache()

    return {
        "logo_data_uri": LOGO_DATA_URI,
        "favicon_data_uri": FAVICON_DATA_URI,
        "wifi_ssid": cfg["wifi"]["ssid"],
        "wifi_password": cfg["wifi"]["password"],
        "show_ebooks": show_ebooks,
        "show_audiobooks": show_audiobooks,
        "show_requests": show_requests,
        "show_shutdown": show_shutdown,
        "show_leds": show_leds,
        "ebook_url": ebook_url,
        "audiobook_url": audiobook_url,
        "requests_url": requests_url,
        "ebook_login_note": _account_note(cfg["apps"]["calibre_account_status"], username, password) if show_ebooks else "",
        "audiobook_login_note": _account_note(cfg["apps"]["audiobookshelf_account_status"], username, password) if show_audiobooks else "",
        "shutdown_hold_secs": cfg["shutdown_button"]["hold_secs"],
        "ebook_qr": get_cached_qr(cache, "ebook", ebook_url) if show_ebooks else "",
        "abs_ios_qr": get_cached_qr(cache, "abs_ios", ABS_IOS_SEARCH_URL) if show_audiobooks else "",
        "abs_android_qr": get_cached_qr(cache, "abs_android", ABS_ANDROID_SEARCH_URL) if show_audiobooks else "",
        "requests_qr": get_cached_qr(cache, "requests", requests_url) if show_requests else "",
    }


def render_welcome_page(cfg):
    # render_template_string needs an app context; the /welcome route
    # already has one, but this is also called from the pipeline's
    # background thread (no request in flight), so make sure one exists
    # either way rather than crashing setup at the last step.
    with app.app_context():
        return render_template_string(WELCOME_PAGE_TEMPLATE, **_welcome_context(cfg))


def _data_uri_to_buf(data_uri):
    """Decodes a base64 data URI (as produced by load_logo_data_uri()/
    get_cached_qr()) back to a BytesIO reportlab can use as an image
    source. Returns None for an empty/invalid URI."""
    if not data_uri or "," not in data_uri:
        return None
    try:
        _, b64data = data_uri.split(",", 1)
        return io.BytesIO(base64.b64decode(b64data))
    except Exception:
        return None


def render_welcome_pdf_bytes(cfg):
    """Builds the welcome page as a PDF directly with reportlab (no HTML
    rendering step involved), from the same context as the on-screen
    welcome page, so the two always agree on content."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ctx = _welcome_context(cfg)

    styles = getSampleStyleSheet()
    heading_color = colors.HexColor("#4a2e0a")
    muted_color = colors.HexColor("#8a6d4a")
    rule_color = colors.HexColor("#e7dcc9")
    kv_bg = colors.HexColor("#faf6f0")

    title_style = ParagraphStyle("WelcomeTitle", parent=styles["Title"], textColor=heading_color, fontSize=22, alignment=TA_CENTER)
    subtitle_style = ParagraphStyle("WelcomeSubtitle", parent=styles["Normal"], textColor=muted_color, alignment=TA_CENTER, spaceAfter=14)
    heading_style = ParagraphStyle("CardHeading", parent=styles["Heading2"], textColor=heading_color, spaceAfter=4)
    body_style = ParagraphStyle("CardBody", parent=styles["Normal"], spaceAfter=3, leading=14)
    kv_style = ParagraphStyle(
        "KV", parent=styles["Normal"], fontName="Courier", backColor=kv_bg, borderPadding=4, spaceAfter=3
    )
    note_style = ParagraphStyle("Note", parent=styles["Normal"], textColor=muted_color, fontSize=9)

    story = []

    logo_buf = _data_uri_to_buf(ctx["logo_data_uri"])
    if logo_buf:
        logo_img = Image(logo_buf, width=90, height=90)
        logo_img.hAlign = "CENTER"
        story.append(logo_img)
        story.append(Spacer(1, 6))

    story.append(Paragraph("Welcome to the Lusty Library", title_style))
    story.append(Paragraph("Everything you need to connect, read, and listen.", subtitle_style))

    def add_card(heading, flowables):
        story.append(Paragraph(escape(heading), heading_style))
        story.extend(flowables)
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width="100%", thickness=0.75, color=rule_color))
        story.append(Spacer(1, 10))

    def with_qr(qr_data_uri, text_flowables, qr_size=80):
        """Lays `text_flowables` next to a QR image (if one is cached),
        or just returns them alone if there isn't one."""
        qr_buf = _data_uri_to_buf(qr_data_uri)
        if not qr_buf:
            return text_flowables
        qr_img = Image(qr_buf, width=qr_size, height=qr_size)
        table = Table([[qr_img, text_flowables]], colWidths=[qr_size + 10, None])
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
        return [table]

    add_card("Connect to Wi-Fi", [
        Paragraph(f"Network name (SSID): {escape(ctx['wifi_ssid'])}", kv_style),
        Paragraph(f"Password: {escape(ctx['wifi_password'])}", kv_style),
    ])

    if ctx["show_ebooks"]:
        text = [Paragraph(f"Open {escape(ctx['ebook_url'])} in a browser.", body_style)]
        if ctx["ebook_login_note"]:
            text.append(Paragraph(escape(ctx["ebook_login_note"]), note_style))
        add_card("eBooks", with_qr(ctx["ebook_qr"], text))

    if ctx["show_audiobooks"]:
        flowables = [
            Paragraph("Install the free Audiobookshelf app, then add this server address:", body_style),
            Paragraph(escape(ctx["audiobook_url"]), kv_style),
        ]
        if ctx["audiobook_login_note"]:
            flowables.append(Paragraph(escape(ctx["audiobook_login_note"]), note_style))
        ios_buf = _data_uri_to_buf(ctx["abs_ios_qr"])
        android_buf = _data_uri_to_buf(ctx["abs_android_qr"])
        if ios_buf or android_buf:
            row = []
            if ios_buf:
                row.append(Image(ios_buf, width=70, height=70))
            if android_buf:
                row.append(Image(android_buf, width=70, height=70))
            qr_table = Table([row])
            flowables.append(Spacer(1, 4))
            flowables.append(qr_table)
        add_card("Audiobooks", flowables)

    if ctx["show_requests"]:
        text = [Paragraph(f"Or visit {escape(ctx['requests_url'])}", body_style)]
        add_card("Request New Books or Audiobooks", with_qr(ctx["requests_qr"], text))

    if ctx["show_shutdown"]:
        add_card("Shutting Down", [
            Paragraph(
                f"Press and hold the shutdown button for about {ctx['shutdown_hold_secs']} seconds. "
                "The status LEDs will flash a few times, then it's safe to unplug the power.",
                body_style,
            )
        ])

    if ctx["show_leds"]:
        led_lines = ["● Green — Wi-Fi ready"]
        if ctx["show_ebooks"]:
            led_lines.append("● Yellow — eBooks ready")
        if ctx["show_audiobooks"]:
            led_lines.append("● Blue — Audiobooks ready")
        add_card("LED Indicators", [Paragraph(line, body_style) for line in led_lines])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, topMargin=40, bottomMargin=40, leftMargin=54, rightMargin=54)
    doc.build(story)
    return buf.getvalue()


def write_welcome_page(cfg, step_id):
    """Renders the welcome/instructions page (HTML + PDF) and saves static
    copies in the media folder (so they can be opened/printed even
    without the setup wizard running), in addition to the live
    /welcome and /welcome.pdf routes."""
    html = render_welcome_page(cfg)
    media_root = cfg["storage"]["media_root"]
    out_html = Path(media_root) / WELCOME_HTML_NAME
    write_file(out_html, html, step_id)

    try:
        pdf_bytes = render_welcome_pdf_bytes(cfg)
        out_pdf = Path(media_root) / WELCOME_PDF_NAME
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf_bytes)
        log_line(step_id, f"writing {out_pdf}")
    except Exception as e:  # noqa: BLE001 - the HTML page still works without a PDF
        log_line(step_id, f"Couldn't generate the printable PDF: {e}", level="error")

    log_line(
        step_id,
        f"Welcome page ready — http://{cfg['wifi']['ip']}:9000/welcome "
        f"(printable PDF at /welcome.pdf, also saved under {media_root}).",
    )


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
        if cfg["apps"]["install_calibre_web"]:
            ensure_calibre_library(cfg, "apps")
        bring_up_apps(compose_path, "apps")
        if cfg["apps"]["install_calibre_web"]:
            test_feature_led(cfg, "apps", cfg["leds"]["pin_cweb"], "Calibre-Web")
        if cfg["apps"]["install_audiobookshelf"]:
            test_feature_led(cfg, "apps", cfg["leds"]["pin_abs"], "Audiobookshelf")
        set_step("apps", "done")

        # accounts: create the patron login shown on the welcome page
        set_step("accounts", "running")
        provision_accounts(cfg, "accounts")
        set_step("accounts", "done")

        # requests page
        set_step("requests_page", "running")
        install_requests_page(cfg, "requests_page")
        set_step("requests_page", "done")

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

        # qr codes: install qrcode just long enough to bake in the images
        # the welcome page needs, then remove it again
        set_step("qr_codes", "running")
        generate_and_cache_qr_codes(cfg, "qr_codes")
        set_step("qr_codes", "done")

        # welcome page: generated last, once every feature's real on/off
        # state (apps, requests page, LEDs, shutdown button, accounts, QR
        # codes) is known
        set_step("welcome_page", "running")
        write_welcome_page(cfg, "welcome_page")
        set_step("welcome_page", "done")

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
  {% if favicon_data_uri %}<link rel="icon" href="{{ favicon_data_uri }}">{% endif %}
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
    #banner a { color:inherit; text-decoration:underline; }
    .brand { display:flex; align-items:center; gap:14px; }
    .brand img { height:96px; width:auto; }
    .brand h1 { margin:0; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="brand">
        {% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="Lusty Library logo">{% endif %}
        <h1>Lusty Library Setup</h1>
      </div>
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
                <input name="wifi_password" value="{{ cfg.wifi.password }}" placeholder="e.g. lustybooks123"
                       minlength="8" maxlength="63" title="8-63 characters (WPA2 requirement)">
                <small>Must be 8-63 characters — WPA2 won't accept anything shorter.</small>
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
          <div class="row" style="margin-top:10px;">
            <div>
              <label>Patron account username
                <input name="patron_username" value="{{ cfg.apps.patron_username }}">
              </label>
            </div>
            <div>
              <label>Patron account password
                <input name="patron_password" value="{{ cfg.apps.patron_password }}">
              </label>
            </div>
          </div>
          <small>Setup creates this account automatically in Calibre-Web and/or Audiobookshelf
          (whichever you install above) and shows it on the welcome page — you don't need to log
          into either app yourself first.</small>
        </fieldset>

        <fieldset>
          <legend>Book/Audiobook Requests</legend>
          <div class="checkbox-row">
            <input type="checkbox" id="requests_enabled" name="requests_enabled" {% if cfg.requests_page.enabled %}checked{% endif %}>
            <label for="requests_enabled">Enable a request page for patrons</label>
          </div>
          <label>Port
            <input name="requests_port" value="{{ cfg.requests_page.port }}">
          </label>
          <small>A simple page anyone on the hotspot can use to ask for a title. Requests are saved to
          <code>requests.csv</code> in the media folder and shown in a table on the same page, with a
          one-click "mark fulfilled" toggle.</small>
        </fieldset>

        <fieldset>
          <legend>Welcome / Instructions Page</legend>
          <small>After setup finishes, a printable welcome page is generated at
          <code>/welcome</code> (and as a PDF at <code>/welcome.pdf</code>) with the Lusty Library
          logo, Wi-Fi info, the patron account above, and QR codes for eBooks, Audiobooks, and
          requests — only for the features you actually installed above.</small>
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
  if (ok) {
    b.innerHTML = 'Setup finished successfully. ' +
      '<a href="/welcome" target="_blank">View the welcome page</a> &middot; ' +
      '<a href="/welcome.pdf" target="_blank">Download the printable PDF</a>';
  } else {
    b.textContent = "Setup stopped due to an error — see the console below.";
  }
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
  payload.requests_enabled = fd.has("requests_enabled");
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
        logo_data_uri=LOGO_DATA_URI,
        favicon_data_uri=FAVICON_DATA_URI,
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

    wifi_errors = validate_wifi_settings(cfg)
    if wifi_errors:
        return jsonify({"error": " ".join(wifi_errors)}), 400

    cfg["storage"]["media_root"] = (data.get("media_root") or "").strip() or cfg["storage"]["media_root"]
    storage_device = (data.get("storage_device") or "").strip()
    format_device = bool(data.get("format_device"))

    cfg["apps"]["install_audiobookshelf"] = bool(data.get("install_audiobookshelf"))
    cfg["apps"]["install_calibre_web"] = bool(data.get("install_calibre_web"))
    cfg["apps"]["patron_username"] = (data.get("patron_username") or "").strip() or cfg["apps"]["patron_username"]
    cfg["apps"]["patron_password"] = (data.get("patron_password") or "").strip() or cfg["apps"]["patron_password"]

    cfg["requests_page"]["enabled"] = bool(data.get("requests_enabled"))
    try:
        cfg["requests_page"]["port"] = int(data.get("requests_port", cfg["requests_page"]["port"]))
    except (TypeError, ValueError):
        pass

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


@app.route("/welcome")
def welcome():
    cfg = load_config()
    return render_welcome_page(cfg)


@app.route("/welcome.pdf")
def welcome_pdf():
    cfg = load_config()
    media_root = Path(cfg["storage"]["media_root"])
    saved_pdf = media_root / WELCOME_PDF_NAME
    if saved_pdf.exists():
        # Serve the copy generated during setup rather than re-rendering,
        # so this always reflects exactly what setup produced.
        return send_file(saved_pdf, mimetype="application/pdf", download_name="lusty-library-welcome.pdf")
    try:
        pdf_bytes = render_welcome_pdf_bytes(cfg)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Couldn't generate the PDF: {e}"}), 500
    return Response(pdf_bytes, mimetype="application/pdf")


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
