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
import re
import shlex
import errno
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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


def load_logo_on_dark_data_uri():
    """The logo file is black artwork on a white background, which turns into
    a muddy dark blob (with a speckled edge) on the setup/request pages' dark
    theme. This converts it, once at startup, into light-coloured artwork on
    a transparent background — cropped tight and scaled down, so it's crisp
    on dark pages and far smaller to embed than the full-size original. The
    printable welcome page (white background) keeps using the original.
    Falls back to the original if Pillow isn't available."""
    try:
        from PIL import Image, ImageOps

        ink = ImageOps.invert(Image.open(LOGO_PATH).convert("L"))
        # Levels: crush the paper's faint speckle to nothing, keep solid ink.
        ink = ink.point(lambda v: 0 if v < 70 else (255 if v > 170 else int((v - 70) * 255 / 100)))
        bbox = ink.getbbox()
        if bbox:
            pad = 12
            l, t, r, b = bbox
            ink = ink.crop((max(0, l - pad), max(0, t - pad), min(ink.width, r + pad), min(ink.height, b + pad)))
        width = 480
        ink = ink.resize((width, max(1, round(ink.height * width / ink.width))), Image.LANCZOS)
        out = Image.new("RGBA", ink.size, (243, 244, 246, 0))
        out.putalpha(ink)
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 - purely cosmetic, never block startup
        return LOGO_DATA_URI


LOGO_ON_DARK_DATA_URI = load_logo_on_dark_data_uri()

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
        # Chosen with the folder browser on the Auto-sync step: the share on the
        # server, and the folder inside that share, for each of the two folders.
        "server_share_audio": "",
        "server_path_audio": "",
        "server_share_books": "",
        "server_path_books": "",
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

APPLIED_SNAPSHOT_NAME = ".applied_config.json"
# Config keys the installer fills in itself; they never count as a user change.
_DERIVED_KEYS = {"calibre_account_status", "audiobookshelf_account_status"}


def applied_snapshot_path():
    return CONFIG_PATH.parent / APPLIED_SNAPSHOT_NAME


def load_applied_snapshot():
    """The settings the last SUCCESSFUL run applied, or None if there isn't
    one (first install, or an install from before this existed)."""
    try:
        return json.loads(applied_snapshot_path().read_text())
    except (OSError, ValueError):
        return None


def save_applied_snapshot(cfg):
    try:
        applied_snapshot_path().write_text(json.dumps(cfg, sort_keys=True))
    except OSError:
        pass


def _section(cfg, name):
    sec = dict(cfg.get(name) or {})
    for k in _DERIVED_KEYS:
        sec.pop(k, None)
    return sec


def plan_changes(old, new, storage_changed=False, force_all=False):
    """Works out which install steps need to run by comparing the settings
    the last successful run applied (old) with the new ones. Returns
    (plan, needs_reboot): plan maps step id -> True (run) / False (skip).
    With no previous run, or force_all, everything runs."""
    ids = [sid for sid, _ in STEP_ORDER]
    if force_all or old is None:
        return {sid: True for sid in ids}, True

    def differs(name):
        return _section(old, name) != _section(new, name)

    wifi_c = differs("wifi")
    storage_c = storage_changed or old["storage"].get("media_root") != new["storage"].get("media_root")
    apps_c = differs("apps") or storage_c
    sync_c = differs("sync") or storage_c
    leds_c = differs("leds")
    btn_c = differs("shutdown_button") or leds_c
    req_c = differs("requests_page") or wifi_c
    # The LED daemon reports on the apps and the hotspot, so it follows them.
    led_svc_c = leds_c or apps_c or wifi_c
    anything = any([wifi_c, storage_c, apps_c, sync_c, leds_c, btn_c, req_c])
    plan = {
        "preflight": True,
        "leds_test": leds_c,
        "wifi": wifi_c,
        "storage": storage_c,
        "docker": apps_c,
        "apps": apps_c,
        "accounts": apps_c,
        "requests_page": req_c,
        "sync": sync_c,
        "trigger": sync_c,
        "leds_service": led_svc_c,
        "shutdown_button": btn_c,
        "qr_codes": anything,
        "welcome_page": anything,
    }
    # Only a changed hotspot needs a reboot: it is saved now and starts on boot.
    return plan, wifi_c


STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "finished": False,
    "ok": None,
    "needs_reboot": True,
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
            "connection.autoconnect-priority", "100",
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
    # Deliberately NOT bringing the hotspot up now: that would switch the Pi's
    # only Wi-Fi radio away from your home network, cutting off this page and
    # the internet the install still needs (Docker images, packages). The
    # hotspot starts by itself the next time the Pi boots.
    log_line(step_id, "Hotspot saved. It will start after the Pi reboots, so this page and the internet keep working during the install.")


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
        log_line(step_id, "Hotspot saved. It will start after the Pi reboots, so this page and the internet keep working during the install.")
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


def _truthy(v):
    return str(v).lower() in ("1", "true", "yes")


def _tree_mountpoints(node):
    """Every mountpoint used by a device and everything under it."""
    mps = [m for m in (node.get("mountpoints") or []) if m]
    if node.get("mountpoint"):
        mps.append(node["mountpoint"])
    for ch in node.get("children") or []:
        mps.extend(_tree_mountpoints(ch))
    return mps


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") or n >= 100 else f"{n:.1f} {unit}"
        n /= 1000


def parse_storage_devices(data):
    """One entry per physical drive (not per partition), so the list stays
    short. USB-to-SATA/NVMe adapters usually report the drive as NOT
    removable, so a drive counts if it is removable, hot-pluggable or on the
    USB/NVMe bus. Any disk holding / or /boot (the SD card or system disk) is
    never offered.

    Each entry says whether the drive is already "ready" (has an ext4
    partition we can use as it is) or needs to be erased and set up."""
    devices = []
    for dev in data.get("blockdevices", []):
        name = dev.get("name") or ""
        if dev.get("type") != "disk" or name.startswith(("mmcblk", "loop", "zram", "ram")):
            continue
        usbish = (
            _truthy(dev.get("rm"))
            or _truthy(dev.get("hotplug"))
            or str(dev.get("tran") or "").lower() in ("usb", "nvme")
        )
        if not usbish:
            continue
        if any(m == "/" or m.startswith("/boot") for m in _tree_mountpoints(dev)):
            continue
        parts = [c for c in (dev.get("children") or []) if c.get("type") == "part"]
        ext4 = [c for c in parts if (c.get("fstype") or "") == "ext4"]
        ready = max(ext4, key=lambda c: int(c.get("size") or 0)) if ext4 else None
        ready_mp = ""
        if ready:
            ready_mp = ready.get("mountpoint") or next((m for m in (ready.get("mountpoints") or []) if m), "")
        devices.append(
            {
                "path": f"/dev/{name}",
                "model": (dev.get("model") or "").strip() or "USB drive",
                "size": _human_size(dev.get("size")),
                "ready_part": f"/dev/{ready['name']}" if ready else "",
                "ready_mountpoint": ready_mp or "",
            }
        )
    return devices


def get_storage_devices():
    cols = "NAME,SIZE,FSTYPE,MOUNTPOINT,TYPE,RM,HOTPLUG,TRAN,MODEL"
    for extra in (",MOUNTPOINTS", ""):
        try:
            out = subprocess.check_output(["lsblk", "-b", "-J", "-o", cols + extra], text=True)
            return parse_storage_devices(json.loads(out))
        except Exception:
            continue
    return []


def write_mount_unit(device, mountpoint, step_id, fstype="ext4"):
    """systemd mount unit so the drive mounts itself on every boot."""
    mountpoint = Path(mountpoint)
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


def _first_partition(device):
    """Path of the first partition on a disk, or "" if there isn't one."""
    try:
        out = subprocess.check_output(["lsblk", "-ln", "-o", "NAME,TYPE", device], text=True)
    except Exception:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "part":
            return "/dev/" + parts[0]
    return ""


def format_and_mount_device(device, mountpoint, step_id, fstype="ext4"):
    """EXTREMELY DESTRUCTIVE: wipes the given drive, formats it as ext4 and
    mounts it at the given mountpoint (and on every boot)."""
    if not device.startswith("/dev/"):
        log_line(step_id, f"refusing to touch non-device path: {device}", level="error")
        return
    if "mmcblk0" in device:
        log_line(step_id, "refusing to format the internal SD card (mmcblk0).", level="error")
        return

    mountpoint = Path(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)

    # Stop anything using the drive (a previous install's containers keep the
    # media folder busy), unmount it and its partitions, then clear old
    # signatures (partition tables, NTFS markers) so the new filesystem is
    # the only thing left.
    run_cmd(["bash", "-c", "docker ps -q 2>/dev/null | xargs -r docker stop >/dev/null 2>&1; true"], step_id, allow_fail=True)
    unit_name = mountpoint.as_posix().lstrip("/").replace("/", "-") + ".mount"
    run_cmd(["systemctl", "stop", unit_name], step_id, allow_fail=True)
    run_cmd(["bash", "-c", f"for p in {shlex.quote(device)}*; do umount \"$p\" 2>/dev/null; done; true"], step_id, allow_fail=True)
    rc = run_cmd(["wipefs", "-a", device], step_id, allow_fail=True)
    if rc not in (0, None):
        raise StepFailed(
            f"{device} is still in use, so it can't be erased. Close anything using it "
            f"(for example a file manager), or reboot the Pi, then run setup again."
        )
    # Give the drive a fresh GPT with one partition spanning it, then format
    # that partition (the standard layout, and what "ready to use" detects).
    run_cmd(["bash", "-c", f"printf 'label: gpt\\n,,L\\n' | sfdisk --wipe always {shlex.quote(device)}"], step_id)
    run_cmd(["partprobe", device], step_id, allow_fail=True)
    run_cmd(["udevadm", "settle"], step_id, allow_fail=True)
    time.sleep(1.0)
    part = _first_partition(device)
    if not part:
        raise StepFailed(f"Couldn't find the new partition on {device}. Unplug and re-plug the drive, then run setup again.")
    log_line(step_id, f"Formatting {part} as ext4 (this can take a minute on a big drive)...")
    run_cmd(["mkfs.ext4", "-F", part], step_id)
    run_cmd(["mount", part, str(mountpoint)], step_id)
    write_mount_unit(part, mountpoint, step_id, fstype)


def ensure_media_root(media_root, step_id):
    """Creates the media folder. If it is a dead mount (a drive that was
    erased or unplugged, still registered as a mount), remove the stale mount
    and its boot-time unit first so the folder can be created again."""
    path = Path(media_root)
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / ".lusty_write_test").touch()
        (path / ".lusty_write_test").unlink()
        return
    except OSError as e:
        if e.errno not in (errno.ENODEV, errno.EIO, errno.ENOTCONN, errno.ESTALE, errno.EROFS, errno.ENXIO):
            raise
        log_line(step_id, f"{media_root} isn't usable ({e.strerror}); clearing the old drive mount...")
    unit_name = path.as_posix().lstrip("/").replace("/", "-") + ".mount"
    run_cmd(["systemctl", "disable", "--now", unit_name], step_id, allow_fail=True)
    run_cmd(["umount", "-l", media_root], step_id, allow_fail=True)
    unit_path = Path("/etc/systemd/system") / unit_name
    if unit_path.exists():
        unit_path.unlink()
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StepFailed(
            f"{media_root} still can't be used ({e.strerror}). Reboot the Pi, or pick a drive on the Storage step."
        )


def use_existing_device(partition, mountpoint, step_id):
    """Uses a drive that already has an ext4 partition, keeping its files."""
    mountpoint = Path(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)
    current = subprocess.run(["findmnt", "-n", "-o", "TARGET", partition], capture_output=True, text=True).stdout.strip()
    if current == str(mountpoint):
        log_line(step_id, f"{partition} is already mounted at {mountpoint}; keeping its files.")
    else:
        if current:
            run_cmd(["umount", partition], step_id, allow_fail=True)
        run_cmd(["mount", partition, str(mountpoint)], step_id)
    write_mount_unit(partition, mountpoint, step_id)


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


# Calibre-Web Automated, pinned to a released version (not "latest") so an
# install today and one next year behave the same.
CWA_IMAGE = "crocodilestick/calibre-web-automated:v4.0.8"
CWA_CONTAINER = "calibre-web-automated"
MEDIA_UID = 1000
MEDIA_GID = 1000


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
        # Calibre-Web Automated (CWA): Calibre-Web plus an "ingest" folder.
        # Any book file dropped into the ingest folder is imported into the
        # library on its own (and removed from the folder afterwards), so
        # nobody has to scan or re-index anything.
        lines += [
            "  calibre-web-automated:",
            f"    image: {CWA_IMAGE}",
            "    container_name: calibre-web-automated",
            "    ports:",
            '      - "8083:8083"',
            "    environment:",
            f"      - PUID={MEDIA_UID}",
            f"      - PGID={MEDIA_GID}",
            "      - TZ=America/Chicago",
            "    volumes:",
            f"      - {media_root}/books:/calibre-library",
            f"      - {media_root}/ingest:/cwa-book-ingest",
            f"      - {media_root}/config/calibre:/config",
            "    restart: unless-stopped",
            "",
        ]

    compose_path = Path("/home/pi/library-server")
    write_file(compose_path / "docker-compose.yml", "\n".join(lines), step_id)

    for sub in ("audiobooks", "books", "config", "ingest"):
        Path(media_root, sub).mkdir(parents=True, exist_ok=True)
    if install_cweb:
        # CWA runs as an unprivileged user; it can only import books if the
        # folders it uses are owned by that user, not root.
        for sub in ("books", "ingest", "config/calibre"):
            folder = Path(media_root, sub)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                os.chown(folder, MEDIA_UID, MEDIA_GID)
            except OSError as e:
                log_line(step_id, f"Couldn't set the owner of {folder}: {e}", level="error")

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
CALIBRE_DEFAULT_ADMIN_PASSWORD = "admin123"  # Calibre-Web (Automated) documented first-run default
CALIBRE_LIBRARY_PATH_IN_CONTAINER = "/calibre-library"  # matches the volume mount in generate_docker_compose()


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

    _calibre_create_user(session, base, username, password)

    # Don't trust "no error shown" — prove the patron account really works
    # by logging in as it from a brand-new session.
    check = requests.Session()
    page = check.get(f"{base}/login", timeout=10)
    data = {"username": username, "password": password}
    token = _extract_csrf_token(page.text)
    if token:
        data["csrf_token"] = token
    resp = check.post(f"{base}/login", data=data, timeout=10)
    if "login" in resp.url and resp.status_code == 200:
        raise RuntimeError(f"created '{username}' but logging in as it still fails")


def _calibre_flash_text(html):
    """Best-effort: the red/green message Calibre-Web flashed on the page."""
    import re

    import html as html_lib

    # Returns (category, text) of the last flashed message, e.g.
    # ("success", "User 'book' created") or ("danger", "Password doesn't ...").
    found = re.findall(r'<div[^>]*id="flash_(\w+)"[^>]*>(.*?)</div>', html, re.S)
    if not found:
        return "", ""
    category, body = found[-1]
    return category, html_lib.unescape(re.sub(r"<[^>]+>", " ", body)).strip()


def _calibre_disable_password_rules(session, base):
    """Calibre-Web ships with a password policy on (8+ characters and a mix
    of upper/lower case, digits and symbols) that rejects a simple patron
    login like book/book. This is a public offline library with a shared
    login, so turn the policy off — through the same settings form an admin
    would use. Checkboxes we leave out are reset by Calibre-Web, so the ones
    that are on by default and shouldn't change are sent as on."""
    page = session.get(f"{base}/admin/config", timeout=10)
    token = _extract_csrf_token(page.text)
    form = {
        "config_password_min_length": "1",
        "config_check_extensions": "on",
        "config_embed_metadata": "on",
        "config_ratelimiter": "on",
    }
    headers = {"X-CSRFToken": token} if token else {}
    if token:
        form["csrf_token"] = token
    resp = session.post(f"{base}/admin/ajaxconfig", data=form, headers=headers, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} turning off Calibre-Web's password rules")


def _calibre_set_library(session, base):
    """If Calibre-Web hasn't picked its library yet it redirects every admin
    page to /admin/dbconfig. Calibre-Web Automated normally finds
    /calibre-library by itself, but if it hasn't yet, set it through the same
    form the setup screen would show."""
    page = session.get(f"{base}/admin/dbconfig", timeout=10)
    token = _extract_csrf_token(page.text)
    form = {"config_calibre_dir": CALIBRE_LIBRARY_PATH_IN_CONTAINER}
    if token:
        form["csrf_token"] = token
    resp = session.post(f"{base}/admin/dbconfig", data=form, timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} setting the Calibre library path")


def _calibre_create_user(session, base, username, password):
    new_user_page = session.get(f"{base}/admin/user/new", timeout=10)
    if "dbconfig" in new_user_page.url:
        _calibre_set_library(session, base)
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

    for attempt in (1, 2):
        resp = session.post(f"{base}/admin/user/new", data=form, timeout=10)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} creating user")
        if "user/new" not in resp.url:
            return  # redirected to the user list: created
        # Calibre-Web answers 200 and re-shows the "Add New User" form when
        # it refuses the user. Read its own message instead of guessing.
        category, message = _calibre_flash_text(resp.text)
        if category == "success":
            return  # created (this Calibre-Web version re-shows the form with a success message)
        if "existing account" in message.lower() or "already exists" in message.lower():
            return  # a re-run: the login check right after this proves the password still works
        if attempt == 1 and "password" in message.lower():
            _calibre_disable_password_rules(session, base)
            new_user_page = session.get(f"{base}/admin/user/new", timeout=10)
            token = _extract_csrf_token(new_user_page.text)
            if token:
                form["csrf_token"] = token
            continue
        raise RuntimeError(f"Calibre-Web rejected the new user: {message or 'no reason shown'}")


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


def _abs_ensure_library(base, username, password, step_id):
    """Audiobookshelf starts with no library, so the audiobooks folder is
    invisible until someone adds one. Create an 'Audiobooks' library on the
    mounted /audiobooks folder if there are no libraries yet."""
    resp = requests.post(f"{base}/login", json={"username": username, "password": password}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} logging in to add the library")
    user = (resp.json() or {}).get("user") or {}
    token = user.get("accessToken") or user.get("token")
    if not token:
        raise RuntimeError("no login token returned")
    headers = {"Authorization": f"Bearer {token}"}

    libs = requests.get(f"{base}/api/libraries", headers=headers, timeout=15)
    libs.raise_for_status()
    body = libs.json()
    existing = body.get("libraries", body) if isinstance(body, dict) else body
    if existing:
        log_line(step_id, "Audiobookshelf already has a library; leaving it as it is.")
        return
    created = requests.post(
        f"{base}/api/libraries",
        headers=headers,
        json={
            "name": "Audiobooks",
            "folders": [{"fullPath": "/audiobooks"}],
            "icon": "audiobookshelf",
            "mediaType": "book",
            "provider": "audible",
        },
        timeout=30,
    )
    created.raise_for_status()
    log_line(step_id, "Created the 'Audiobooks' library in Audiobookshelf; it is scanning /audiobooks now.")


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

    if not str(cfg["apps"].get("audiobookshelf_account_status", "")).startswith("failed"):
        try:
            _abs_ensure_library(base, username, password, step_id)
        except Exception as e:  # noqa: BLE001 - the library can always be added by hand
            log_line(
                step_id,
                f"Couldn't create the Audiobookshelf library automatically: {e}. "
                "In Audiobookshelf choose Add Library and pick the /audiobooks folder.",
                level="error",
            )


def install_library_refresh(cfg, step_id):
    """Calibre-Web Automated only tells the website to re-read the library once
    the importer has been quiet for a moment, so during a big import the site
    keeps showing an empty library. This timer runs the importer's own
    follow-up every few minutes while books are waiting, so new books show up
    as they are imported."""
    if not cfg["apps"]["install_calibre_web"]:
        return
    ingest = cfg["storage"]["media_root"].rstrip("/") + "/ingest"
    script = f"""#!/bin/bash
# Refresh the ebook library view while books are still being imported.
INGEST={shlex.quote(ingest)}
docker inspect -f '{{{{.State.Running}}}}' {CWA_CONTAINER} 2>/dev/null | grep -q true || exit 0
[[ -n "$(find "$INGEST" -type f -print -quit 2>/dev/null)" ]] || exit 0
docker exec {CWA_CONTAINER} python3 /app/calibre-web-automated/scripts/ingest_processor.py --post-batch-follow-up >/dev/null 2>&1 || true
"""
    write_file("/usr/local/bin/lustylibrary-refresh-library.sh", script, step_id, mode=0o755)
    write_file(
        "/etc/systemd/system/lustylibrary-refresh.service",
        "[Unit]\nDescription=Refresh the Lusty Library ebook list while importing\n\n"
        "[Service]\nType=oneshot\nExecStart=/usr/local/bin/lustylibrary-refresh-library.sh\n",
        step_id,
    )
    write_file(
        "/etc/systemd/system/lustylibrary-refresh.timer",
        "[Unit]\nDescription=Refresh the Lusty Library ebook list every few minutes\n\n"
        "[Timer]\nOnBootSec=3min\nOnUnitActiveSec=5min\n\n[Install]\nWantedBy=timers.target\n",
        step_id,
    )
    run_cmd(["systemctl", "daemon-reload"], step_id, allow_fail=True)
    run_cmd(["systemctl", "enable", "--now", "lustylibrary-refresh.timer"], step_id, allow_fail=True)


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
    logo_data_uri = LOGO_ON_DARK_DATA_URI
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


# ---------------------------------------------------------------------------
# SMB folder browsing (the picker on the Auto-sync step)
#
# Lists the shares on a server, and the folders inside a share, by running
# smbclient. Nothing is mounted. Every value that ends up in a smbclient
# command or in the generated sync script is validated first: smbclient's -c
# option runs ';'-separated commands, so a quote or ';' in a path must never
# get through.
# ---------------------------------------------------------------------------

SMB_TIMEOUT_SECONDS = 25
SMB_INSTALL_TIMEOUT_SECONDS = 300
SMB_SKIP_FOLDERS = {"$recycle.bin", "system volume information", "@eadir", "lost+found", "__macosx", "#recycle"}
_SMB_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
# Characters that must never reach a smbclient command or the sync script:
# quotes and ';' (command separators), backslash, wildcards, and control chars.
_SMB_BAD_CHARS = frozenset('";*?<>|' + chr(92))


class _SmbNameBad:
    @staticmethod
    def search(text):
        return any(c in _SMB_BAD_CHARS or ord(c) < 32 for c in text)


_SMB_NAME_BAD = _SmbNameBad()
_SMB_LS_RE = re.compile(
    r"^\s{2}(?P<name>.+?)\s+(?P<attr>[A-Za-z]+)\s+(?P<size>[0-9]+)\s+"
    r"[A-Za-z]{3}\s+[A-Za-z]{3}\s+[0-9]+\s+[0-9]{2}:[0-9]{2}:[0-9]{2}\s+[0-9]{4}\s*$"
)
_SMBCLIENT_LOCK = threading.Lock()


def smb_clean_host(host):
    host = str(host or "").strip()
    if not _SMB_HOST_RE.match(host):
        raise ValueError("Enter the server as an IP address (like 192.168.0.50) or a name (like tower).")
    return host


def smb_clean_share(share):
    share = str(share or "").strip()
    if not share:
        raise ValueError("Choose a share on the server first.")
    if len(share) > 80 or "/" in share or _SMB_NAME_BAD.search(share) or share.upper() == "IPC$":
        raise ValueError(f"'{share}' isn't a share name this tool can use.")
    return share


def smb_clean_path(path):
    """'/media//audiobook/' -> ['media', 'audiobook']. Raises ValueError for
    anything that could change what a smbclient command does."""
    parts = []
    for part in str(path or "").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("A folder path can't contain '..'.")
        if _SMB_NAME_BAD.search(part):
            raise ValueError(f"The folder name '{part}' has a character this tool can't handle (a quote, backslash, semicolon or wildcard).")
        parts.append(part)
    return parts


def _smb_clean_login(value, label):
    value = str(value or "")
    if any(c in value for c in "\r\n\x00"):
        raise ValueError(f"The {label} can't contain line breaks.")
    return value


def ensure_smbclient():
    """None if smbclient is available (installing it first if it's missing),
    otherwise a plain-language problem description."""
    if shutil.which("smbclient"):
        return None
    with _SMBCLIENT_LOCK:
        if shutil.which("smbclient"):
            return None
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return "The folder browser needs a small tool (smbclient) that isn't installed, and setup isn't running as root. Run: sudo apt-get install -y smbclient"
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        last = ""
        for attempt in range(2):
            if attempt == 1:  # package lists can be stale on a fresh image
                subprocess.run(["apt-get", "update"], capture_output=True, text=True, env=env, timeout=SMB_INSTALL_TIMEOUT_SECONDS)
            try:
                proc = subprocess.run(
                    ["apt-get", "install", "-y", "smbclient"],
                    capture_output=True, text=True, env=env, timeout=SMB_INSTALL_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return "Installing the folder-browser tool took too long. Check the Pi's internet connection and try again."
            if proc.returncode == 0 and shutil.which("smbclient"):
                return None
            last = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            last = last[0]
        return f"Couldn't install the folder-browser tool (the Pi needs internet for this once). {last}".strip()


def start_smbclient_install_async():
    """Called as soon as auto-sync is ticked, so the tool is usually installed
    by the time the user taps Browse."""
    if shutil.which("smbclient"):
        return
    threading.Thread(target=ensure_smbclient, daemon=True).start()


def run_smbclient(server, username, password, share=None, command=None):
    """Runs smbclient and returns (returncode, stdout, stderr). Credentials
    go in a private temp file, never on the command line. Raises
    subprocess.TimeoutExpired if the server doesn't answer in time."""
    args = ["smbclient"]
    args += ["-L", server, "-g"] if share is None else [f"//{server}/{share}"]
    auth_path = None
    try:
        if username:
            fd, auth_path = tempfile.mkstemp(prefix="lusty-smb-")
            os.chmod(auth_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"username = {username}\npassword = {password}\n")
            args += ["-A", auth_path]
        else:
            args += ["-N"]
        if command:
            args += ["-c", command]
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=SMB_TIMEOUT_SECONDS,
            env=dict(os.environ, LC_ALL="C"),
        )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        if auth_path:
            try:
                os.unlink(auth_path)
            except OSError:
                pass


_SMB_ERRORS = [
    ("NT_STATUS_LOGON_FAILURE", "The server rejected that username or password."),
    ("NT_STATUS_WRONG_PASSWORD", "The server rejected that username or password."),
    ("NT_STATUS_ACCOUNT_DISABLED", "That account is disabled on the server."),
    ("NT_STATUS_PASSWORD_EXPIRED", "That account's password has expired on the server."),
    ("NT_STATUS_ACCESS_DENIED", "Access denied. That login isn't allowed to open this (guest access may be turned off — try entering a username and password)."),
    ("NT_STATUS_BAD_NETWORK_NAME", "The server doesn't have a share with that name."),
    ("NT_STATUS_OBJECT_NAME_NOT_FOUND", "That folder doesn't exist any more (it may have been renamed)."),
    ("NT_STATUS_OBJECT_PATH_NOT_FOUND", "That folder doesn't exist any more (it may have been renamed)."),
    ("NT_STATUS_NO_SUCH_FILE", "That folder doesn't exist any more (it may have been renamed)."),
    ("NT_STATUS_NOT_A_DIRECTORY", "That isn't a folder."),
    ("NT_STATUS_HOST_UNREACHABLE", "Couldn't reach the server. Check the IP address and that it's on the same network as this Pi."),
    ("NT_STATUS_NETWORK_UNREACHABLE", "Couldn't reach the server. Check the IP address and that it's on the same network as this Pi."),
    ("NT_STATUS_CONNECTION_REFUSED", "The server refused the connection. Is file sharing (SMB) turned on there?"),
    ("NT_STATUS_IO_TIMEOUT", "The server didn't answer in time. Check the IP address and that it's switched on."),
    ("NT_STATUS_CONNECTION_DISCONNECTED", "The server dropped the connection. Try again."),
    ("NT_STATUS_UNSUCCESSFUL", "Couldn't connect to the server. Check the IP address and that file sharing (SMB) is on."),
    ("NT_STATUS_RESOURCE_NAME_NOT_FOUND", "Couldn't find that server. Check the IP address or name."),
    ("NT_STATUS_PROTOCOL_NOT_SUPPORTED", "The server and this Pi couldn't agree on an SMB version."),
]


def smb_friendly_error(stdout, stderr):
    text = f"{stderr}\n{stdout}"
    for needle, message in _SMB_ERRORS:
        if needle in text:
            return message
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "The server said: " + (lines[-1] if lines else "no details given")


def smb_browse(server, username, password, share, parts):
    """With no share: returns ({'shares': [...]}, None). With a share: returns
    ({'folders': [...], 'files': n, 'path': '/a/b'}, None). On failure
    returns (None, plain-language message)."""
    try:
        if not share:
            rc, out, err = run_smbclient(server, username, password)
            shares = []
            for line in out.splitlines():
                kind, _, rest = line.partition("|")
                name = rest.partition("|")[0]
                if kind == "Disk" and name and not name.endswith("$"):
                    shares.append(name)
            if rc != 0 and not shares:
                return None, smb_friendly_error(out, err)
            return {"shares": sorted(set(shares), key=str.lower)}, None

        command = "ls" if not parts else 'ls "' + "/".join(parts) + '/*"'
        rc, out, err = run_smbclient(server, username, password, share=share, command=command)
    except subprocess.TimeoutExpired:
        return None, "The server took too long to answer. Check the IP address and that it's switched on."
    except OSError as e:
        return None, f"Couldn't run the folder-browser tool: {e}"

    folders, files = [], 0
    for line in out.splitlines():
        m = _SMB_LS_RE.match(line)
        if not m:
            continue
        name, attr = m.group("name"), m.group("attr")
        if name in (".", ".."):
            continue
        if "D" in attr:
            if name.startswith(".") or name.lower() in SMB_SKIP_FOLDERS:
                continue
            folders.append({"name": name, "ok": not _SMB_NAME_BAD.search(name)})
        else:
            files += 1
    if rc != 0 and not folders and not files:
        return None, smb_friendly_error(out, err)
    folders.sort(key=lambda f: f["name"].lower())
    return {"folders": folders, "files": files, "path": "/" + "/".join(parts)}, None


BOOK_EXT_PATTERN = "epub|mobi|azw|azw3|azw4|kepub|pdf|fb2|cbz|cbr|cb7|djvu|txt|rtf|docx|odt|htm|html|lit|lrf|prc|pdb|snb"


def apply_sync_config(cfg, step_id):
    if not cfg["sync"]["enable_sync"]:
        log_line(step_id, "Auto-sync disabled, skipping.")
        return

    media_root = cfg["storage"]["media_root"]
    username = (cfg["sync"].get("server_username") or "").strip()
    password = cfg["sync"].get("server_password") or ""
    try:
        server_ip = smb_clean_host(cfg["sync"]["server_ip"])
        share_audio = smb_clean_share(cfg["sync"]["server_share_audio"])
        share_books = smb_clean_share(cfg["sync"]["server_share_books"])
        path_audio = "/" + "/".join(smb_clean_path(cfg["sync"]["server_path_audio"]))
        path_books = "/" + "/".join(smb_clean_path(cfg["sync"]["server_path_books"]))
    except ValueError as e:
        raise StepFailed(f"Auto-sync settings are incomplete: {e}")
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

    q = shlex.quote
    # Audiobooks and books can live in different shares on the server, so each
    # gets its own mount. Values are validated above and shell-quoted here.
    script = f"""#!/bin/bash
set -euo pipefail

SERVER_IP={q(server_ip)}
SHARE_AUDIO={q(f"//{server_ip}/{share_audio}")}
SHARE_BOOKS={q(f"//{server_ip}/{share_books}")}
MOUNT_AUDIO="/mnt/remote_audio"
MOUNT_BOOKS="/mnt/remote_books"

SRC_AUDIO="${{MOUNT_AUDIO}}"{q(path_audio)}
SRC_BOOKS="${{MOUNT_BOOKS}}"{q(path_books)}

DST_AUDIO={q(media_root.rstrip("/") + "/audiobooks/")}
DST_INGEST={q(media_root.rstrip("/") + "/ingest/")}
STATE_DIR={q(media_root.rstrip("/") + "/.sync_state")}
MANIFEST="$STATE_DIR/delivered_books.txt"

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

  mount_share() {{
    local share="$1" point="$2"
    if ! mountpoint -q "$point"; then
      echo "Mounting $share -> $point"
      mkdir -p "$point"
      mount -t cifs "$share" "$point" -o "$CIFS_OPTS" || return 1
    fi
  }}

  unmount_all() {{
    for point in "$MOUNT_AUDIO" "$MOUNT_BOOKS"; do
      if mountpoint -q "$point"; then
        echo "Unmounting $point"
        umount "$point" || true
      fi
    done
  }}

  if ! mount_share "$SHARE_AUDIO" "$MOUNT_AUDIO" || ! mount_share "$SHARE_BOOKS" "$MOUNT_BOOKS"; then
    echo "ERROR: mount failed"
    unmount_all
    rm -f "$FLAG"
    exit 0
  fi

  mkdir -p "$DST_AUDIO" "$DST_INGEST" "$STATE_DIR"
  touch "$MANIFEST"

  echo "Syncing audiobooks..."
  rsync -av --ignore-existing "${{SRC_AUDIO%/}}/" "$DST_AUDIO" || true

  # Books go to the ingest folder, where Calibre-Web Automated imports them
  # into its library by itself (and deletes the copies). Because the ingest
  # folder empties out, a manifest of already-delivered files stops the same
  # book from being copied again on the next sync.
  echo "Syncing books..."
  WORK="$(mktemp -d)"
  EMPTY="$WORK/empty"; mkdir -p "$EMPTY"
  if rsync -rn --out-format='%n' "${{SRC_BOOKS%/}}/" "$EMPTY/" > "$WORK/all.txt" 2>>"$LOG"; then
    grep -v '/$' "$WORK/all.txt" \
      | grep -iE '\.({BOOK_EXT_PATTERN})$' | LC_ALL=C sort -u > "$WORK/candidates.txt" || true
    LC_ALL=C sort -u "$MANIFEST" -o "$MANIFEST"
    LC_ALL=C comm -23 "$WORK/candidates.txt" "$MANIFEST" > "$WORK/new.txt"
    COUNT="$(wc -l < "$WORK/new.txt")"
    echo "Books: $COUNT new file(s) to hand to the library."
    if [[ "$COUNT" -gt 0 ]]; then
      # Copy to a staging folder on the same drive first, then move each book
      # into the ingest folder as a plain file (no author sub-folders), owned
      # by the library user. Flat files are what the importer handles best,
      # and a move never shows it a half-copied book.
      STAGE="$STATE_DIR/staging"
      mkdir -p "$STAGE"
      if rsync -rt --chown={MEDIA_UID}:{MEDIA_GID} --files-from="$WORK/new.txt" "${{SRC_BOOKS%/}}/" "$STAGE/"; then
        while IFS= read -r -d '' f; do
          dest="$DST_INGEST/$(basename "$f")"
          if [[ -e "$dest" ]]; then
            dest="$DST_INGEST/$(printf '%s' "$f" | md5sum | cut -c1-8)-$(basename "$f")"
          fi
          mv -f "$f" "$dest"
        done < <(find "$STAGE" -type f -print0)
        find "$STAGE" -mindepth 1 -type d -empty -delete
        cat "$WORK/new.txt" >> "$MANIFEST"
        LC_ALL=C sort -u "$MANIFEST" -o "$MANIFEST"
      else
        echo "WARNING: some books could not be copied, will retry next sync."
      fi
    fi
  else
    echo "WARNING: could not read the books folder on the server."
  fi
  rm -rf "$WORK"

  unmount_all

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


def free_gpio_pins(step_id):
    """Makes sure nothing from a previous install is holding the LED/button
    pins ("GPIO busy"): stops the status-LED and shutdown-button services and
    any leftover status_leds.py, then logs whoever still has the GPIO chip."""
    for unit in ("lustylibrary-leds.service", "lustylibrary-shutdown-button.service"):
        run_cmd(["systemctl", "stop", unit], step_id, allow_fail=True)
    run_cmd(["pkill", "-f", "/usr/local/bin/status_leds.py"], step_id, allow_fail=True)
    time.sleep(0.7)
    if shutil.which("fuser"):
        holders = subprocess.run(
            "fuser -v /dev/gpiochip* 2>&1 | grep -v '^$'", shell=True, capture_output=True, text=True
        ).stdout.strip()
        if holders:
            log_line(step_id, "Still holding the GPIO chip:\n" + holders)


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
        try:
            led = LED(pin)
        except Exception as first:  # noqa: BLE001
            if "busy" not in str(first).lower():
                raise
            log_line(step_id, f"GPIO{pin} is busy, freeing it from other services and retrying...")
            free_gpio_pins(step_id)
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
        cweb_block = f"""
        calibre_up = container_running({CWA_CONTAINER!r})
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
  <title>Lusty Library Connection Instructions</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {% if favicon_data_uri %}<link rel="icon" href="{{ favicon_data_uri }}">{% endif %}
  <style>
    @page { size: 11in 8.5in; margin: 0; }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; }
    body { background:#d9d4cb; font-family: Arial, Helvetica, sans-serif; color:#111; }
    .toolbar { text-align:center; padding:10px 12px; font-size:15px; }
    .toolbar a, .toolbar button { display:inline-block; margin:0 4px; padding:9px 16px; border-radius:999px; border:0;
      background:#4a2e0a; color:#fff; text-decoration:none; font-size:15px; font-weight:700; cursor:pointer; }
    #stage { width:11in; height:8.5in; transform-origin:0 0; margin:0 auto; }
    .sheet { width:11in; height:8.5in; background:#fff; padding:0.35in 0.55in; position:relative; overflow:hidden;
             box-shadow:0 4px 24px rgba(0,0,0,.25); }
    .head { display:flex; align-items:center; justify-content:center; gap:22px; }
    .head img { height:0.95in; width:auto; }
    h1 { margin:0; font-size:44px; font-weight:700; line-height:1.05; text-align:center; }
    .sub { text-align:center; font-size:24px; margin:6px 0 14px; }
    .cols { display:grid; grid-template-columns: 1fr 1fr; gap:0.4in; align-items:start; }
    .sec { margin-bottom:0.16in; }
    h2 { margin:0 0 6px; font-size:25px; font-weight:700; }
    p { margin:0 0 5px; font-size:18px; line-height:1.28; }
    .row { display:flex; gap:18px; align-items:flex-start; }
    .qr { text-align:center; }
    .qr img { width:1.35in; height:1.35in; display:block; }
    .qr span { display:block; font-size:21px; font-weight:700; margin-top:2px; }
    .qrs { display:flex; gap:0.45in; margin:4px 0 8px; }
    .big { font-size:23px; font-weight:700; }
    .kv { font-family: "Courier New", monospace; font-weight:700; }
    .wifi { border:3px solid #111; border-radius:10px; padding:8px 14px; margin-bottom:0.16in; }
    .wifi p { margin:2px 0; font-size:21px; }
    .leds p { display:flex; align-items:center; gap:10px; margin:3px 0; font-size:20px; }
    .dot { width:20px; height:20px; border-radius:50%; flex-shrink:0; border:1px solid rgba(0,0,0,.4); }
    .dot.green { background:#22c55e; } .dot.blue { background:#3b82f6; } .dot.yellow { background:#facc15; }
    @media print {
      body { background:#fff; }
      .toolbar { display:none; }
      #stage { transform:none !important; margin:0; }
      .sheet { box-shadow:none; }
    }
  </style>
</head>
<body>
  <div class="toolbar">
    <button type="button" onclick="window.print()">Print</button>
    <a href="/welcome.pdf" target="_blank">Download PDF</a>
  </div>
  <div id="stage"><div class="sheet">
    <div class="head">
      {% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="Lusty Library logo">{% endif %}
      <h1>Welcome to the Lusty Library</h1>
    </div>
    <div class="sub">Lusty Library Connection Instructions</div>

    <div class="cols">
      <div>
        <div class="wifi">
          <p>Once the <strong>green LED</strong> is on, connect to the Wi-Fi. Please connect without internet access.</p>
          <p>Network: <span class="kv">{{ wifi_ssid }}</span></p>
          <p>Password: <span class="kv">{{ wifi_password }}</span></p>
        </div>

        {% if show_ebooks %}
        <div class="sec">
          <h2>For eBooks:</h2>
          <div class="row">
            {% if ebook_qr %}<div class="qr"><img src="{{ ebook_qr }}" alt="eBooks QR code"></div>{% endif %}
            <div>
              <p>Scan the QR code for eBooks.</p>
              {% if ebook_login_note %}<p class="big">{{ ebook_login_note }}</p>{% endif %}
              {% if not ebook_qr %}<p>Open <span class="kv">{{ ebook_url }}</span></p>{% endif %}
            </div>
          </div>
        </div>
        {% endif %}

        {% if show_requests %}
        <div class="sec">
          <h2>To Request New Books or Audiobooks:</h2>
          <div class="row">
            {% if requests_qr %}<div class="qr"><img src="{{ requests_qr }}" alt="Request page QR code"></div>{% endif %}
            <div>
              <p>Scan the QR code and fill out the request form. Requested titles will be added before your next visit.</p>
              {% if not requests_qr %}<p>Visit <span class="kv">{{ requests_url }}</span></p>{% endif %}
            </div>
          </div>
        </div>
        {% endif %}
      </div>

      <div>
        {% if show_audiobooks %}
        <div class="sec">
          <h2>For Audiobooks:</h2>
          <p>Please download the Audiobookshelf app for your phone <strong>before</strong> connecting to the Wi-Fi. Scan the QR code for Android or iOS.</p>
          <div class="qrs">
            {% if abs_ios_qr %}<div class="qr"><img src="{{ abs_ios_qr }}" alt="iOS app QR code"><span>iOS</span></div>{% endif %}
            {% if abs_android_qr %}<div class="qr"><img src="{{ abs_android_qr }}" alt="Android app QR code"><span>Android</span></div>{% endif %}
          </div>
          <p>After installing, connect to the Wi-Fi and, inside the app, use <span class="kv">{{ audiobook_url }}</span> as the server address.</p>
          {% if audiobook_login_note %}<p class="big">{{ audiobook_login_note }}</p>{% endif %}
        </div>
        {% endif %}

        {% if show_leds %}
        <div class="sec leds">
          <h2>LED Indicators:</h2>
          <p><span class="dot green"></span> Green Light = Wi-Fi is ready</p>
          {% if show_audiobooks %}<p><span class="dot blue"></span> Blue Light = Audiobooks are ready</p>{% endif %}
          {% if show_ebooks %}<p><span class="dot yellow"></span> Yellow Light = eBooks are ready</p>{% endif %}
        </div>
        {% endif %}

        {% if show_shutdown %}
        <div class="sec">
          <h2>To Shut Down the Server:</h2>
          <p>Press and hold the button for {{ shutdown_hold_secs }} seconds. Once all LEDs are off, unplug from the wall.</p>
        </div>
        {% endif %}
      </div>
    </div>
  </div></div>
  <script>
  (function () {
    var stage = document.getElementById("stage");
    function fit() {
      var w = 11 * 96, h = 8.5 * 96;
      var s = Math.min(1, window.innerWidth / w);
      stage.style.transform = "scale(" + s + ")";
      stage.style.marginLeft = s < 1 ? "0" : "auto";
      stage.style.height = (h * s) + "px";
      stage.style.width = (w * s) + "px";
      stage.firstElementChild.style.transformOrigin = "0 0";
    }
    fit(); window.addEventListener("resize", fit);
  })();
  </script>
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
        "shutdown_hold_secs": f"{float(cfg['shutdown_button']['hold_secs']):g}",
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
    """Builds the welcome sign as a landscape US-Letter PDF directly with
    reportlab (no HTML rendering step involved), from the same context as
    the on-screen welcome page, so the two always agree on content."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate, Flowable, Frame, FrameBreak, Image, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    ctx = _welcome_context(cfg)
    page_w, page_h = landscape(LETTER)
    margin = 36
    gap = 28
    col_w = (page_w - 2 * margin - gap) / 2
    head_h = 110

    title = ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=32, leading=36, alignment=TA_CENTER)
    sub = ParagraphStyle("S", fontName="Helvetica", fontSize=17, leading=20, alignment=TA_CENTER, spaceAfter=4)
    h2 = ParagraphStyle("H", fontName="Helvetica-Bold", fontSize=17, leading=20, spaceAfter=4)
    body = ParagraphStyle("B", fontName="Helvetica", fontSize=13, leading=16, spaceAfter=4)
    big = ParagraphStyle("G", fontName="Helvetica-Bold", fontSize=15, leading=18, spaceAfter=4)
    cap = ParagraphStyle("C", fontName="Helvetica-Bold", fontSize=14, leading=16, alignment=TA_CENTER)

    class Dot(Flowable):
        """A coloured LED dot followed by its label."""
        def __init__(self, color, label):
            super().__init__()
            self.color, self.label = color, label
            self.width, self.height = col_w, 22

        def draw(self):
            c = self.canv
            c.setFillColor(colors.HexColor(self.color))
            c.setStrokeColor(colors.HexColor("#444444"))
            c.circle(8, 9, 7, stroke=1, fill=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 14)
            c.drawString(24, 4, self.label)

    class Box(Flowable):
        """Outlined box holding flowables (the Wi-Fi notice)."""
        def __init__(self, flowables, width):
            super().__init__()
            self.flowables, self.width_ = flowables, width
            pad = 8
            self.pad = pad
            self.inner = []
            h = 0
            for f in flowables:
                w, fh = f.wrap(width - 2 * pad, 1000)
                h += fh
            self.height = h + 2 * pad
            self.width = width

        def wrap(self, aw, ah):
            return self.width, self.height

        def draw(self):
            c = self.canv
            c.setLineWidth(2)
            c.roundRect(0, 0, self.width, self.height, 7, stroke=1, fill=0)
            y = self.height - self.pad
            for f in self.flowables:
                w, fh = f.wrap(self.width - 2 * self.pad, 1000)
                y -= fh
                f.drawOn(c, self.pad, y)

    def qr_img(data_uri, size=96):
        buf = _data_uri_to_buf(data_uri)
        return Image(buf, width=size, height=size) if buf else None

    def qr_row(data_uri, flowables, size=96):
        img = qr_img(data_uri, size)
        if not img:
            return flowables
        t = Table([[img, flowables]], colWidths=[size + 12, col_w - size - 12])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0)]))
        return [t]

    P = lambda text, st=body: Paragraph(text, st)
    mono = lambda s: f'<font name="Courier-Bold">{escape(s)}</font>'

    story = []
    # header
    logo = _data_uri_to_buf(ctx["logo_data_uri"])
    head_cells = []
    if logo:
        head_cells.append(Image(logo, width=72, height=72))
    head_cells.append([P("Welcome to the Lusty Library", title), P("Lusty Library Connection Instructions", sub)])
    ht = Table([head_cells], colWidths=([84] if logo else []) + [None])
    ht.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(ht)
    story.append(FrameBreak())

    # left column
    story.append(Box([
        P("Once the <b>green LED</b> is on, connect to the Wi-Fi. Please connect without internet access.", body),
        P(f"Network: {mono(ctx['wifi_ssid'])} &nbsp; Password: {mono(ctx['wifi_password'])}", body),
    ], col_w))
    story.append(Spacer(1, 12))
    if ctx["show_ebooks"]:
        text = [P("Scan the QR code for eBooks.")]
        if ctx["ebook_login_note"]:
            text.append(P(escape(ctx["ebook_login_note"]), big))
        if not ctx["ebook_qr"]:
            text.append(P(f"Open {mono(ctx['ebook_url'])}"))
        story.append(P("For eBooks:", h2))
        story.extend(qr_row(ctx["ebook_qr"], text))
        story.append(Spacer(1, 12))
    if ctx["show_requests"]:
        text = [P("Scan the QR code and fill out the request form. Requested titles will be added before your next visit.")]
        if not ctx["requests_qr"]:
            text.append(P(f"Visit {mono(ctx['requests_url'])}"))
        story.append(P("To Request New Books or Audiobooks:", h2))
        story.extend(qr_row(ctx["requests_qr"], text))
    story.append(FrameBreak())

    # right column
    if ctx["show_audiobooks"]:
        story.append(P("For Audiobooks:", h2))
        story.append(P("Please download the Audiobookshelf app for your phone <b>before</b> connecting to the Wi-Fi. "
                       "Scan the QR code for Android or iOS."))
        cells, labels = [], []
        for key, label in (("abs_ios_qr", "iOS"), ("abs_android_qr", "Android")):
            img = qr_img(ctx[key], 90)
            if img:
                cells.append(img)
                labels.append(P(label, cap))
        if cells:
            qt = Table([cells, labels], colWidths=[110] * len(cells))
            qt.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            qt.hAlign = "LEFT"
            story.append(qt)
            story.append(Spacer(1, 6))
        story.append(P(f"After installing, connect to the Wi-Fi and, inside the app, use {mono(ctx['audiobook_url'])} as the server address."))
        if ctx["audiobook_login_note"]:
            story.append(P(escape(ctx["audiobook_login_note"]), big))
        story.append(Spacer(1, 10))
    if ctx["show_leds"]:
        story.append(P("LED Indicators:", h2))
        story.append(Dot("#22c55e", "Green Light = Wi-Fi is ready"))
        if ctx["show_audiobooks"]:
            story.append(Dot("#3b82f6", "Blue Light = Audiobooks are ready"))
        if ctx["show_ebooks"]:
            story.append(Dot("#facc15", "Yellow Light = eBooks are ready"))
        story.append(Spacer(1, 10))
    if ctx["show_shutdown"]:
        story.append(P("To Shut Down the Server:", h2))
        story.append(P(f"Press and hold the button for {ctx['shutdown_hold_secs']} seconds. "
                       "Once all LEDs are off, unplug from the wall."))

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(page_w, page_h), leftMargin=margin, rightMargin=margin,
                          topMargin=margin, bottomMargin=margin, title="Lusty Library Connection Instructions")
    body_h = page_h - 2 * margin - head_h
    frames = [
        Frame(margin, page_h - margin - head_h, page_w - 2 * margin, head_h, id="head", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
        Frame(margin, margin, col_w, body_h, id="left", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
        Frame(margin + col_w + gap, margin, col_w, body_h, id="right", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0),
    ]
    doc.addPageTemplates([PageTemplate(id="sign", frames=frames)])
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


def run_pipeline(cfg, storage_device, format_device, plan=None, needs_reboot=True):
    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["finished"] = False
        STATE["ok"] = None
        STATE["needs_reboot"] = needs_reboot
        for step_id, _ in STEP_ORDER:
            STATE["steps"][step_id] = "pending"

    plan = plan or {sid: True for sid, _ in STEP_ORDER}
    emit("start")
    ok = True
    try:
        # preflight
        if not plan.get("preflight", True):
            log_line("preflight", "Settings for this step didn't change, so it was skipped.")
            set_step("preflight", "skipped")
        else:
            set_step("preflight", "running")
            board = load_board_info()
            log_line("preflight", f"Board: {board.get('model', 'unknown')}")
            log_line("preflight", f"OS codename: {board.get('os_codename', 'unknown')}")
            log_line("preflight", f"Architecture: {board.get('arch', 'unknown')}")
            set_step("preflight", "done")

        # leds: general test screen, up front. A previous install leaves the
        # LED service running and holding the pins ("GPIO busy"), so stop it
        # first; the LED step starts it again at the end.
        if not plan.get("leds_test", True):
            log_line("leds_test", "Settings for this step didn't change, so it was skipped.")
            set_step("leds_test", "skipped")
        else:
            free_gpio_pins("leds_test")
            set_step("leds_test", "running")
            run_led_test_screen(cfg, "leds_test")
            set_step("leds_test", "done")

        # wifi
        if not plan.get("wifi", True):
            log_line("wifi", "Settings for this step didn't change, so it was skipped.")
            set_step("wifi", "skipped")
        else:
            set_step("wifi", "running")
            apply_wifi_config(cfg, "wifi")
            if plan.get("leds_test", True):
                test_feature_led(cfg, "wifi", cfg["leds"]["pin_wifi"], "Wi-Fi")
            set_step("wifi", "done")

        # storage
        if not plan.get("storage", True):
            log_line("storage", "Settings for this step didn't change, so it was skipped.")
            set_step("storage", "skipped")
        else:
            set_step("storage", "running")
            chosen = next((d for d in get_storage_devices() if d["path"] == storage_device), None) if storage_device else None
            if storage_device and not chosen:
                raise StepFailed(f"The drive {storage_device} isn't available any more. Plug it in and run setup again.")
            if chosen:
                log_line("storage", f"Drive {chosen['path']}: " + (
                    f"has a usable ext4 partition ({chosen['ready_part']})" if chosen["ready_part"] else "no usable ext4 partition")
                    + f"; erase requested: {'yes' if format_device else 'no'}.")
            if chosen and (format_device or not chosen["ready_part"]):
                format_and_mount_device(chosen["path"], cfg["storage"]["media_root"], "storage")
            elif chosen:
                use_existing_device(chosen["ready_part"], cfg["storage"]["media_root"], "storage")
            else:
                log_line("storage", "No device selected for formatting; using existing storage as-is.")
                ensure_media_root(cfg["storage"]["media_root"], "storage")
            set_step("storage", "done")

        # docker
        if not plan.get("docker", True):
            log_line("docker", "Settings for this step didn't change, so it was skipped.")
            set_step("docker", "skipped")
        else:
            set_step("docker", "running")
            ensure_docker("docker")
            set_step("docker", "done")

        # apps
        if not plan.get("apps", True):
            log_line("apps", "Settings for this step didn't change, so it was skipped.")
            set_step("apps", "skipped")
        else:
            set_step("apps", "running")
            compose_path = generate_docker_compose(cfg, "apps")
            bring_up_apps(compose_path, "apps")
            if cfg["apps"]["install_calibre_web"]:
                if plan.get("leds_test", True):
                    test_feature_led(cfg, "apps", cfg["leds"]["pin_cweb"], "Calibre-Web")
            if cfg["apps"]["install_audiobookshelf"]:
                if plan.get("leds_test", True):
                    test_feature_led(cfg, "apps", cfg["leds"]["pin_abs"], "Audiobookshelf")
            set_step("apps", "done")

        # accounts: create the patron login shown on the welcome page
        if not plan.get("accounts", True):
            log_line("accounts", "Settings for this step didn't change, so it was skipped.")
            set_step("accounts", "skipped")
        else:
            set_step("accounts", "running")
            provision_accounts(cfg, "accounts")
            install_library_refresh(cfg, "accounts")
            set_step("accounts", "done")

        # requests page
        if not plan.get("requests_page", True):
            log_line("requests_page", "Settings for this step didn't change, so it was skipped.")
            set_step("requests_page", "skipped")
        else:
            set_step("requests_page", "running")
            install_requests_page(cfg, "requests_page")
            set_step("requests_page", "done")

        # sync
        if not plan.get("sync", True):
            log_line("sync", "Settings for this step didn't change, so it was skipped.")
            set_step("sync", "skipped")
        else:
            set_step("sync", "running")
            apply_sync_config(cfg, "sync")
            set_step("sync", "done")

        # trigger
        if not plan.get("trigger", True):
            log_line("trigger", "Settings for this step didn't change, so it was skipped.")
            set_step("trigger", "skipped")
        else:
            set_step("trigger", "running")
            apply_sync_trigger(cfg, "trigger")
            set_step("trigger", "done")

        # leds: install the persistent status-LED daemon last, now that
        # docker/app state it reports on actually exists
        if not plan.get("leds_service", True):
            log_line("leds_service", "Settings for this step didn't change, so it was skipped.")
            set_step("leds_service", "skipped")
        else:
            set_step("leds_service", "running")
            install_led_service(cfg, "leds_service")
            set_step("leds_service", "done")

        # shutdown button: tested and installed last of all, since its
        # script needs to know the final LED service name/pins
        if not plan.get("shutdown_button", True):
            log_line("shutdown_button", "Settings for this step didn't change, so it was skipped.")
            set_step("shutdown_button", "skipped")
        else:
            set_step("shutdown_button", "running")
            setup_shutdown_button(cfg, "shutdown_button")
            set_step("shutdown_button", "done")

        # qr codes: install qrcode just long enough to bake in the images
        # the welcome page needs, then remove it again
        if not plan.get("qr_codes", True):
            log_line("qr_codes", "Settings for this step didn't change, so it was skipped.")
            set_step("qr_codes", "skipped")
        else:
            set_step("qr_codes", "running")
            generate_and_cache_qr_codes(cfg, "qr_codes")
            set_step("qr_codes", "done")

        # welcome page: generated last, once every feature's real on/off
        # state (apps, requests page, LEDs, shutdown button, accounts, QR
        # codes) is known
        if not plan.get("welcome_page", True):
            log_line("welcome_page", "Settings for this step didn't change, so it was skipped.")
            set_step("welcome_page", "skipped")
        else:
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
        if ok:
            save_applied_snapshot(cfg)
        with STATE_LOCK:
            STATE["running"] = False
            STATE["finished"] = True
            STATE["ok"] = ok
            reboot = STATE["needs_reboot"]
        emit("done", ok=ok, needs_reboot=reboot)


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
    :root { --bg:#111827; --card:#1f2937; --line:#374151; --muted:#9ca3af; --accent:#10b981; --accent-dark:#059669; }
    * { box-sizing:border-box; }
    [hidden] { display:none !important; }
    body { font-family: system-ui, sans-serif; background:var(--bg); color:#f9fafb; margin:0; }
    .wrap { max-width:640px; margin:0 auto; padding:20px 16px 48px; }
    .card { background:var(--card); padding:22px; border-radius:16px; box-shadow:0 10px 40px rgba(0,0,0,.6); margin-bottom:20px; }
    h1 { margin:0; font-size:26px; }
    h2 { margin:0; font-size:20px; }
    h3 { margin:0; font-size:15px; }
    label { display:block; margin:12px 0 0; font-size:15px; }
    input, select { width:100%; padding:12px; margin-top:6px; border-radius:8px; border:1px solid #4b5563;
                    background:#030712; color:#e5e7eb; font-size:16px; font-family:inherit; }
    input:focus, select:focus { outline:2px solid var(--accent); outline-offset:1px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    .row > div { flex:1 1 150px; min-width:0; }
    button { background:var(--accent); color:#022c22; border:0; padding:12px 20px; border-radius:999px;
             font-weight:600; cursor:pointer; font-size:16px; font-family:inherit; }
    button:hover:not(:disabled) { background:var(--accent-dark); }
    button:disabled { background:#4b5563; color:#9ca3af; cursor:not-allowed; }
    button.secondary { background:#374151; color:#f9fafb; }
    button.secondary:hover:not(:disabled) { background:#4b5563; }
    small, .hint { color:var(--muted); font-size:13px; line-height:1.45; display:block; margin-top:6px; }
    code { background:#030712; padding:1px 5px; border-radius:4px; }

    .brand { text-align:center; }
    .brand img { width:min(180px, 50vw); height:auto; display:block; margin:0 auto 6px; }
    .board-info { color:var(--muted); font-size:13px; text-align:center; margin:4px 0 0; }

    .progress { display:flex; gap:6px; margin:20px 0 14px; }
    .seg { flex:1; height:8px; border-radius:99px; background:var(--line); }
    .seg.done { background:var(--accent); }
    .seg.current { background:#6ee7b7; }
    .seg.clickable { cursor:pointer; }
    .step-count { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.06em; }
    .step-title { margin:2px 0 6px; font-size:24px; }
    .step-intro { color:#d1d5db; font-size:15px; line-height:1.5; margin:0 0 4px; }
    .step { display:none; }
    .step.active { display:block; }

    .check { display:flex; align-items:center; gap:12px; padding:14px; margin:12px 0 0; background:#030712;
             border:1px solid var(--line); border-radius:10px; cursor:pointer; font-size:16px; }
    .check input { width:22px; height:22px; margin:0; flex-shrink:0; accent-color:var(--accent); }
    .opt-details { margin-top:4px; padding-left:2px; }
    .warn-box { background:#450a0a; color:#fecaca; border-radius:8px; padding:12px 14px; margin-top:12px; font-size:14px; line-height:1.45; }

    .step-error { display:none; background:#450a0a; color:#fecaca; border-radius:8px; padding:12px 14px; margin-top:16px; font-size:14px; line-height:1.45; }
    .step-error.show { display:block; }
    .nav { display:flex; gap:12px; margin-top:20px; }
    .nav button { flex:1; padding:14px 18px; }

    .picker-label { margin-top:14px; font-size:15px; }
    .picker { display:flex; justify-content:space-between; align-items:center; gap:10px; width:100%; margin-top:6px;
              padding:14px; background:#030712; color:#e5e7eb; border:1px solid #4b5563; border-radius:8px;
              font-size:16px; font-weight:400; text-align:left; }
    .picker:hover:not(:disabled) { background:#0b1220; }
    .picker .picker-text { word-break:break-word; }
    .picker .picker-text.empty { color:var(--muted); }
    .picker .picker-go { color:#6ee7b7; font-weight:600; flex-shrink:0; }

    #browse-overlay { position:fixed; inset:0; background:rgba(0,0,0,.7); display:flex; align-items:center;
                      justify-content:center; padding:12px; z-index:60; }
    .browse-box { background:var(--card); border-radius:16px; width:100%; max-width:520px; max-height:88vh;
                  display:flex; flex-direction:column; padding:18px; box-shadow:0 10px 40px rgba(0,0,0,.6); }
    .br-head { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:10px; }
    .br-head h3 { font-size:17px; }
    .br-head button { padding:6px 12px; font-size:15px; }
    .br-crumbs { display:flex; flex-wrap:wrap; align-items:center; gap:2px 6px; font-size:14px; margin-bottom:8px; color:var(--muted); }
    .br-crumbs a { color:#6ee7b7; text-decoration:none; word-break:break-all; }
    .br-crumbs a:hover { text-decoration:underline; }
    .br-crumbs .cur { color:#f9fafb; font-weight:600; word-break:break-all; }
    .br-status { font-size:14px; color:var(--muted); min-height:20px; margin-bottom:6px; line-height:1.45; }
    .br-status.err { color:#fca5a5; }
    .br-status button { padding:4px 12px; font-size:13px; margin-left:8px; }
    .br-list { flex:1 1 auto; min-height:80px; overflow-y:auto; border:1px solid var(--line); border-radius:10px; background:#030712; }
    .br-list:empty { display:none; }
    .br-row { display:flex; justify-content:space-between; align-items:center; gap:10px; width:100%; padding:14px; margin:0;
              background:transparent; color:#e5e7eb; border:0; border-bottom:1px solid var(--line); border-radius:0;
              font-size:16px; font-weight:400; text-align:left; }
    .br-row:last-child { border-bottom:0; }
    .br-row:hover:not(:disabled) { background:#111827; }
    .br-row:disabled { color:#6b7280; cursor:not-allowed; }
    .br-row .name { word-break:break-word; }
    .br-row .go { color:#6ee7b7; flex-shrink:0; }
    .br-manual { margin-top:10px; }
    .br-manual .row { align-items:flex-end; }
    .br-manual button { padding:12px 18px; }
    .br-foot { display:flex; gap:12px; margin-top:14px; }
    .br-foot button { flex:1; padding:13px 16px; }

    .rv-group { background:#030712; border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-top:12px; }
    .rv-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
    .rv-head a { color:#6ee7b7; font-size:14px; }
    .rv-row { display:flex; justify-content:space-between; gap:12px; padding:4px 0; font-size:14px; }
    .rv-row span { color:var(--muted); flex-shrink:0; }
    .rv-row strong { font-weight:600; text-align:right; word-break:break-word; }
    .rv-row.warn strong { color:#fca5a5; }

    .progress-head { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; }
    .progress-head button { padding:8px 14px; font-size:14px; }
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
    #banner { display:none; padding:12px 16px; border-radius:8px; margin-bottom:16px; font-weight:600; line-height:1.5; }
    #banner.ok { display:block; background:#064e3b; color:#a7f3d0; }
    #banner.fail { display:block; background:#450a0a; color:#fecaca; }
    #banner a { color:inherit; text-decoration:underline; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="brand">
        {% if logo_data_uri %}<img src="{{ logo_data_uri }}" alt="Lusty Library"><h1>Setup</h1>{% else %}<h1>Lusty Library Setup</h1>{% endif %}
      </div>
      <div class="board-info">
        {{ board.model }} &middot; {{ board.os_codename }} &middot; {{ board.arch }}
      </div>

      <div id="wizard-body">
        <div class="progress" id="segs"></div>
        <div class="step-count" id="step-count"></div>
        <h2 class="step-title" id="step-title"></h2>

        <form id="setup-form" novalidate>

          <section class="step" data-title="Wi-Fi hotspot">
            <p class="step-intro">This is the Wi-Fi network patrons join to reach the library. It works with no other internet connection.</p>
            <label>Network name (SSID)
              <input name="wifi_ssid" value="{{ cfg.wifi.ssid }}" placeholder="e.g. LustyLibrary" autocomplete="off">
            </label>
            <label>Password
              <input name="wifi_password" value="{{ cfg.wifi.password }}" placeholder="e.g. lustybooks123" autocomplete="off">
              <small>Must be 8-63 characters &mdash; WPA2 won't accept anything shorter.</small>
            </label>
            <label>Hotspot IP address (wlan0)
              <input name="wifi_ip" value="{{ cfg.wifi.ip }}" placeholder="e.g. 10.10.10.10" inputmode="decimal" autocomplete="off">
              <small>Uses NetworkManager on newer Raspberry Pi OS, or hostapd/dhcpcd on older images &mdash; picked automatically.</small>
            </label>
          </section>

          <section class="step" data-title="Storage">
            <p class="step-intro">Where the books and audiobooks are kept.</p>
            <label>Media folder
              <input name="media_root" value="{{ cfg.storage.media_root }}" placeholder="e.g. /mnt/media" autocomplete="off">
              <small>Subfolders <code>audiobooks</code>, <code>books</code>, <code>ingest</code> and <code>config</code> are created here.</small>
            </label>
            <label>Drive to use
              <select name="storage_device" id="storage_device">
                <option value="" data-state="none">Don't use a drive (store on the SD card)</option>
                {% for d in devices %}
                  <option value="{{ d.path }}" data-state="{{ 'ready' if d.ready_part else 'blank' }}"{% if d.ready_part and d.ready_mountpoint == cfg.storage.media_root %} selected{% endif %}>{{ d.model }} &middot; {{ d.size }} &middot; {{ 'ready to use, files kept' if d.ready_part else 'will be erased' }}</option>
                {% endfor %}
              </select>
            </label>
            {% if not devices %}<small>No external drive found. Plug in your SSD or USB drive, wait a few seconds, then reload this page.</small>{% endif %}
            <label class="check" id="erase-row" hidden>
              <input type="checkbox" id="format_device" name="format_device" data-warn="format-warning">
              <span>Erase this drive and start fresh</span>
            </label>
            <div class="warn-box" id="format-warning" hidden>&#9888; This erases everything on the selected drive.</div>
          </section>

          <section class="step" data-title="Apps">
            <p class="step-intro">Pick what to install. Docker is installed automatically first if it isn't already there.</p>
            <label class="check">
              <input type="checkbox" id="abs" name="install_audiobookshelf" {% if cfg.apps.install_audiobookshelf %}checked{% endif %}>
              <span>Audiobookshelf <small style="display:inline;">&mdash; audiobooks</small></span>
            </label>
            <label class="check">
              <input type="checkbox" id="cweb" name="install_calibre_web" {% if cfg.apps.install_calibre_web %}checked{% endif %}>
              <span>Calibre-Web Automated <small style="display:inline;">&mdash; ebooks</small></span>
            </label>
            <div class="opt-details" id="patron-details">
              <div class="row">
                <div>
                  <label>Patron username
                    <input name="patron_username" value="{{ cfg.apps.patron_username }}" autocomplete="off" autocapitalize="none">
                  </label>
                </div>
                <div>
                  <label>Patron password
                    <input name="patron_password" value="{{ cfg.apps.patron_password }}" autocomplete="off" autocapitalize="none">
                  </label>
                </div>
              </div>
              <small>Setup creates this account automatically in whichever app(s) you tick, and shows it on the welcome page. You don't need to log into either app first.</small>
            </div>
          </section>

          <section class="step" data-title="Request page">
            <p class="step-intro">A simple page anyone on the hotspot can use to ask for a book or audiobook. Requests are saved to <code>requests.csv</code> in the media folder, with a one-tap &ldquo;mark fulfilled&rdquo;.</p>
            <label class="check">
              <input type="checkbox" id="requests_enabled" name="requests_enabled" data-controls="requests-details" {% if cfg.requests_page.enabled %}checked{% endif %}>
              <span>Enable the request page</span>
            </label>
            <div class="opt-details" id="requests-details">
              <label>Port
                <input name="requests_port" value="{{ cfg.requests_page.port }}" inputmode="numeric" autocomplete="off">
              </label>
            </div>
          </section>

          <section class="step" data-title="Auto-sync (optional)">
            <p class="step-intro">Copy audiobooks and books from another computer or NAS on your network, automatically, the moment an Ethernet cable is plugged in. New books are added to the library by themselves. Skip this if you don't need it.</p>
            <label class="check">
              <input type="checkbox" id="enable_sync" name="enable_sync" data-controls="sync-details" {% if cfg.sync.enable_sync %}checked{% endif %}>
              <span>Enable auto-sync from a server</span>
            </label>
            <div class="opt-details" id="sync-details">
              <label>Server IP
                <input name="server_ip" value="{{ cfg.sync.server_ip }}" placeholder="e.g. 192.168.0.139" inputmode="decimal" autocomplete="off">
              </label>
              <div class="row">
                <div>
                  <label>Username <small style="display:inline;">(blank = guest)</small>
                    <input name="server_username" value="{{ cfg.sync.server_username }}" placeholder="e.g. ryan" autocomplete="off" autocapitalize="none">
                  </label>
                </div>
                <div>
                  <label>Password
                    <input type="password" name="server_password" value="{{ cfg.sync.server_password }}" placeholder="blank if guest" autocomplete="off">
                  </label>
                </div>
              </div>
              <div class="picker-label">Audiobooks folder on the server</div>
              <button type="button" class="picker" id="pick-audio" data-kind="audio"><span class="picker-text"></span><span class="picker-go">Browse</span></button>
              <input type="hidden" name="server_share_audio" value="{{ cfg.sync.server_share_audio }}">
              <input type="hidden" name="server_path_audio" value="{{ cfg.sync.server_path_audio }}">

              <div class="picker-label">Books folder on the server</div>
              <button type="button" class="picker" id="pick-books" data-kind="books"><span class="picker-text"></span><span class="picker-go">Browse</span></button>
              <input type="hidden" name="server_share_books" value="{{ cfg.sync.server_share_books }}">
              <input type="hidden" name="server_path_books" value="{{ cfg.sync.server_path_books }}">
              <small>Tap Browse to look through the server and pick each folder. Nothing is copied until a cable is plugged in.</small>
            </div>
          </section>

          <section class="step" data-title="Hardware (optional)">
            <p class="step-intro">Only tick these if you've actually wired them to the Pi's GPIO pins. Each one is tested for real during setup.</p>

            <label class="check">
              <input type="checkbox" id="leds_enabled" name="leds_enabled" data-controls="leds-details" {% if cfg.leds.enabled %}checked{% endif %}>
              <span>Status LEDs (Wi-Fi / Calibre-Web / Audiobookshelf)</span>
            </label>
            <div class="opt-details" id="leds-details">
              <div class="row">
                <div>
                  <label>Wi-Fi LED pin
                    <input name="pin_wifi" value="{{ cfg.leds.pin_wifi }}" inputmode="numeric" autocomplete="off">
                  </label>
                </div>
                <div>
                  <label>Calibre-Web LED pin
                    <input name="pin_cweb" value="{{ cfg.leds.pin_cweb }}" inputmode="numeric" autocomplete="off">
                  </label>
                </div>
                <div>
                  <label>Audiobookshelf LED pin
                    <input name="pin_abs" value="{{ cfg.leds.pin_abs }}" inputmode="numeric" autocomplete="off">
                  </label>
                </div>
              </div>
              <small>BCM (GPIO) numbers. Each LED is held on so you can confirm it really lights up.</small>
            </div>

            <label class="check">
              <input type="checkbox" id="shutdown_enabled" name="shutdown_enabled" data-controls="shutdown-details" {% if cfg.shutdown_button.enabled %}checked{% endif %}>
              <span>Shutdown button (press and hold)</span>
            </label>
            <div class="opt-details" id="shutdown-details">
              <div class="row">
                <div>
                  <label>Button pin
                    <input name="shutdown_pin" value="{{ cfg.shutdown_button.pin }}" inputmode="numeric" autocomplete="off">
                  </label>
                </div>
                <div>
                  <label>Hold time (seconds)
                    <input name="shutdown_hold_secs" value="{{ cfg.shutdown_button.hold_secs }}" inputmode="decimal" autocomplete="off">
                  </label>
                </div>
              </div>
              <small>Setup will ask you to press and hold the button for real before installing the always-on watcher.</small>
            </div>
          </section>

          <section class="step" data-title="Review &amp; install">
            <p class="step-intro">Check everything, then start. Tap <em>Edit</em> on any section to change it.</p>
            <div id="review"></div>
            {% if installed_before %}
            <label class="check" style="margin-top:12px"><input type="checkbox" name="rerun_all" value="1"><span>Re-run every step (repair). Normally only the steps for settings you changed run.</span></label>
            {% endif %}
            <small>When setup finishes, a printable welcome page (also as a PDF) is generated with the logo, Wi-Fi info, the patron account and QR codes for what you installed.</small>
          </section>

          <div class="step-error" id="step-error" role="alert"></div>

          <div class="nav">
            <button type="button" class="secondary" id="back-btn">Back</button>
            <button type="button" id="next-btn">Next</button>
            <button type="submit" id="apply-btn" hidden>Apply &amp; Set Up</button>
          </div>
        </form>
      </div>
    </div>

    <div class="card" id="progress-card" hidden>
      <div class="progress-head">
        <h2>Progress</h2>
        <button type="button" class="secondary" id="edit-settings" hidden>&larr; Change settings</button>
      </div>
      <div id="banner"></div>
      <ul id="steps">
        {% for id, label in step_order %}
          <li data-step="{{ id }}"><span class="badge pending">&bull;</span><span class="label">{{ label }}</span></li>
        {% endfor %}
      </ul>
      <div id="console"></div>
    </div>
  </div>

  <div id="browse-overlay" hidden>
    <div class="browse-box" role="dialog" aria-modal="true" aria-labelledby="br-title">
      <div class="br-head">
        <h3 id="br-title">Choose a folder</h3>
        <button type="button" class="secondary" id="br-close" aria-label="Close">&#10005;</button>
      </div>
      <div class="br-crumbs" id="br-crumbs"></div>
      <div class="br-status" id="br-status"></div>
      <div class="br-list" id="br-list"></div>
      <div class="br-manual" id="br-manual" hidden>
        <small style="margin-top:0;">Don't see your share? Type its name:</small>
        <div class="row">
          <div><input id="br-share-input" placeholder="e.g. media" autocomplete="off" autocapitalize="none" style="margin-top:6px;"></div>
          <div style="flex:0 0 auto;"><button type="button" class="secondary" id="br-share-go">Open</button></div>
        </div>
      </div>
      <div class="br-foot">
        <button type="button" class="secondary" id="br-cancel">Cancel</button>
        <button type="button" id="br-use" disabled>Use this folder</button>
      </div>
    </div>
  </div>

  <div id="confirm-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.6);
       align-items:center; justify-content:center; z-index:50; padding:16px;">
    <div style="background:#1f2937; padding:24px; border-radius:16px; max-width:420px; text-align:center;">
      <p id="confirm-question" style="font-size:16px; margin-bottom:18px;"></p>
      <div style="display:flex; gap:12px; justify-content:center;">
        <button id="confirm-yes" type="button">Yes</button>
        <button id="confirm-no" type="button" class="secondary">No / skip</button>
      </div>
    </div>
  </div>

<script>
const STEP_LABELS = {{ step_order_json|safe }};
{% raw %}
const badgeChar = {pending:"•", running:"…", done:"✓", error:"✕", skipped:"–"};

const form = document.getElementById("setup-form");
const steps = Array.from(document.querySelectorAll(".step"));
const segsEl = document.getElementById("segs");
const backBtn = document.getElementById("back-btn");
const nextBtn = document.getElementById("next-btn");
const applyBtn = document.getElementById("apply-btn");
const errEl = document.getElementById("step-error");
const wizardBody = document.getElementById("wizard-body");
const progressCard = document.getElementById("progress-card");
const editBtn = document.getElementById("edit-settings");
let current = 0, maxVisited = 0, es = null;

const val = (n) => { const el = form.elements[n]; return el ? String(el.value).trim() : ""; };
const on = (n) => { const el = form.elements[n]; return !!(el && el.checked); };
const isIPv4 = (s) => /^[0-9]{1,3}([.][0-9]{1,3}){3}$/.test(s) && s.split(".").every(p => Number(p) <= 255);
const isPin = (s) => /^[0-9]{1,2}$/.test(s) && Number(s) >= 2 && Number(s) <= 27;

// ---- per-step validation (index matches the order of the <section class="step"> blocks) ----
const validators = [
  () => {  // Wi-Fi
    const ssidBytes = new TextEncoder().encode(val("wifi_ssid")).length;
    const pw = val("wifi_password");
    if (ssidBytes < 1 || ssidBytes > 32) return "The Wi-Fi network name must be 1-32 characters.";
    if (pw.length < 8 || pw.length > 63) return "The Wi-Fi password must be 8-63 characters (WPA2 won't accept anything shorter). Yours is " + pw.length + ".";
    if (!isIPv4(val("wifi_ip"))) return "Enter the hotspot IP like 10.10.10.10.";
    return "";
  },
  () => {  // Storage
    if (!val("media_root").startsWith("/")) return "The media folder must be a full path starting with /, like /mnt/media.";
    return "";
  },
  () => {  // Apps
    if ((on("install_audiobookshelf") || on("install_calibre_web")) && (!val("patron_username") || !val("patron_password"))) {
      return "Enter a patron username and password.";
    }
    return "";
  },
  () => {  // Request page
    if (on("requests_enabled")) {
      const p = Number(val("requests_port"));
      if (!(Number.isInteger(p) && p >= 1 && p <= 65535)) return "The request page port must be a number between 1 and 65535.";
    }
    return "";
  },
  () => {  // Auto-sync
    if (on("enable_sync")) {
      if (!val("server_ip")) return "Enter the server's IP address, or untick auto-sync.";
      if (!val("server_share_audio") || !val("server_share_books")) {
        return "Tap Browse to pick the audiobooks folder and the books folder on the server, or untick auto-sync.";
      }
    }
    return "";
  },
  () => {  // Hardware
    const used = [];
    if (on("leds_enabled")) {
      for (const [n, label] of [["pin_wifi", "Wi-Fi LED"], ["pin_cweb", "Calibre-Web LED"], ["pin_abs", "Audiobookshelf LED"]]) {
        if (!isPin(val(n))) return "The " + label + " pin must be a GPIO number from 2 to 27.";
        used.push([val(n), label]);
      }
    }
    if (on("shutdown_enabled")) {
      if (!isPin(val("shutdown_pin"))) return "The shutdown button pin must be a GPIO number from 2 to 27.";
      const secs = Number(val("shutdown_hold_secs"));
      if (!(secs > 0 && secs <= 30)) return "The hold time must be more than 0 and at most 30 seconds.";
      used.push([val("shutdown_pin"), "shutdown button"]);
    }
    const seen = {};
    for (const [pin, label] of used) {
      if (seen[pin]) return "GPIO" + pin + " is chosen for both the " + seen[pin] + " and the " + label + ".";
      seen[pin] = label;
    }
    return "";
  },
  () => "",  // Review
];

// ---- wizard navigation ----
steps.forEach((s, i) => {
  const seg = document.createElement("span");
  seg.className = "seg";
  seg.setAttribute("role", "button");
  seg.setAttribute("tabindex", "0");
  seg.setAttribute("aria-label", "Step " + (i + 1) + ": " + s.dataset.title);
  seg.title = s.dataset.title;
  const go = () => { if (i <= maxVisited) jumpTo(i); };
  seg.addEventListener("click", go);
  seg.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  segsEl.appendChild(seg);
});

function showErr(msg) { errEl.textContent = msg; errEl.classList.add("show"); }
function clearErr() { errEl.textContent = ""; errEl.classList.remove("show"); }

function show(i, scroll = true) {
  current = i;
  maxVisited = Math.max(maxVisited, i);
  steps.forEach((s, idx) => s.classList.toggle("active", idx === i));
  Array.from(segsEl.children).forEach((seg, idx) => {
    seg.className = "seg" + (idx < i ? " done" : idx === i ? " current" : "") + (idx <= maxVisited ? " clickable" : "");
  });
  document.getElementById("step-count").textContent = "Step " + (i + 1) + " of " + steps.length;
  document.getElementById("step-title").textContent = steps[i].dataset.title;
  const last = i === steps.length - 1;
  backBtn.hidden = i === 0;
  nextBtn.hidden = last;
  applyBtn.hidden = !last;
  if (last) renderReview();
  clearErr();
  // Bring the step into view (skipping past the logo) so each Next lands on its content.
  if (scroll) segsEl.scrollIntoView({behavior: "smooth", block: "start"});
}

function goNext() {
  const err = validators[current]();
  if (err) { showErr(err); return; }
  show(current + 1);
}

function jumpTo(i) {
  if (i === current) return;
  if (i > current) {
    for (let k = current; k < i; k++) {
      const err = validators[k]();
      if (err) { show(k); showErr(err); return; }
    }
  }
  show(i);
}

backBtn.addEventListener("click", () => { if (current > 0) show(current - 1); });
nextBtn.addEventListener("click", goNext);
// Enter in a field means "next", never "submit the whole form from step 1".
form.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "checkbox") {
    e.preventDefault();
    if (current < steps.length - 1) goNext();
  }
});

// ---- show/hide dependent fields ----
function syncToggles() {
  document.querySelectorAll("[data-controls]").forEach((cb) => {
    document.getElementById(cb.dataset.controls).hidden = !cb.checked;
  });
  document.getElementById("patron-details").hidden = !(on("install_audiobookshelf") || on("install_calibre_web"));
  const sd = form.elements["storage_device"];
  const st = sd.options[sd.selectedIndex].dataset.state;
  document.getElementById("erase-row").hidden = st !== "ready";
  if (st !== "ready") form.elements["format_device"].checked = false;
  document.getElementById("format-warning").hidden = !(st === "blank" || on("format_device"));
}
form.addEventListener("change", syncToggles);
syncToggles();

// ---- server folder picker (Auto-sync step) ----
const BR_TITLES = {audio: "Choose the audiobooks folder", books: "Choose the books folder"};
let br = null;
let prepared = false;

function formatLoc(share, path) {
  if (!share) return "—";
  return [share].concat(String(path || "").split("/").filter(Boolean)).join(" › ");
}

function renderPickers() {
  for (const kind of ["audio", "books"]) {
    const share = val("server_share_" + kind);
    const el = document.querySelector("#pick-" + kind + " .picker-text");
    el.textContent = share ? formatLoc(share, val("server_path_" + kind)) : "Choose a folder…";
    el.classList.toggle("empty", !share);
  }
}

const brEl = (id) => document.getElementById(id);

function closeBrowser() {
  if (br) br.token++;
  br = null;
  brEl("browse-overlay").hidden = true;
}

function openBrowser(kind) {
  const server = val("server_ip");
  if (!/^[A-Za-z0-9][A-Za-z0-9.-]*$/.test(server)) {
    showErr("Enter the server's IP address first, then tap Browse.");
    return;
  }
  clearErr();
  br = {kind: kind, share: val("server_share_" + kind), parts: String(val("server_path_" + kind)).split("/").filter(Boolean), token: 0};
  brEl("br-title").textContent = BR_TITLES[kind];
  brEl("browse-overlay").hidden = false;
  loadBrowser();
}

function brStatus(text, isErr, retry) {
  const s = brEl("br-status");
  s.textContent = text;
  s.classList.toggle("err", !!isErr);
  if (retry) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "secondary";
    b.textContent = "Try again";
    b.addEventListener("click", loadBrowser);
    s.appendChild(b);
  }
}

function brCrumbs() {
  const box = brEl("br-crumbs");
  box.textContent = "";
  const add = (label, onClick, current) => {
    if (box.childNodes.length) { const sep = document.createElement("span"); sep.textContent = "›"; box.appendChild(sep); }
    if (current) {
      const c = document.createElement("span"); c.className = "cur"; c.textContent = label; box.appendChild(c);
    } else {
      const a = document.createElement("a"); a.href = "#"; a.textContent = label;
      a.addEventListener("click", (e) => { e.preventDefault(); onClick(); });
      box.appendChild(a);
    }
  };
  add(val("server_ip"), () => { br.share = ""; br.parts = []; loadBrowser(); }, !br.share);
  if (br.share) {
    add(br.share, () => { br.parts = []; loadBrowser(); }, br.parts.length === 0);
    br.parts.forEach((name, i) => {
      add(name, () => { br.parts = br.parts.slice(0, i + 1); loadBrowser(); }, i === br.parts.length - 1);
    });
  }
}

async function loadBrowser() {
  const my = ++br.token;
  brCrumbs();
  brEl("br-list").textContent = "";
  brEl("br-manual").hidden = true;
  brEl("br-use").disabled = true;
  brEl("br-use").textContent = br.share && br.parts.length === 0 ? "Use whole share" : "Use this folder";
  brStatus("Connecting to the server…");
  const slow = setTimeout(() => {
    if (br && br.token === my) brStatus("Still working… the first time, a small tool is installed on the Pi, which can take a minute.");
  }, 4000);
  try {
    const resp = await fetch("/setup/browse", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        server: val("server_ip"), username: val("server_username"), password: form.elements["server_password"].value,
        share: br.share, path: "/" + br.parts.join("/"),
      }),
    });
    const data = await resp.json().catch(() => ({error: "Unexpected reply from the Pi."}));
    clearTimeout(slow);
    if (!br || br.token !== my) return;
    if (!resp.ok) {
      brStatus(data.error || "Something went wrong.", true, true);
      brEl("br-manual").hidden = !!br.share;   // can't list shares? let them type one
      return;
    }
    renderBrowser(data);
  } catch (e) {
    clearTimeout(slow);
    if (br && br.token === my) brStatus("Couldn't reach the setup service on the Pi.", true, true);
  }
}

function renderBrowser(data) {
  const list = brEl("br-list");
  list.textContent = "";
  const row = (label, enabled, onClick) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "br-row";
    b.disabled = !enabled;
    const n = document.createElement("span"); n.className = "name"; n.textContent = label;
    const g = document.createElement("span"); g.className = "go"; g.textContent = enabled ? "›" : "";
    b.append(n, g);
    if (enabled) b.addEventListener("click", onClick);
    list.appendChild(b);
  };
  if (!br.share) {
    brEl("br-manual").hidden = false;
    brEl("br-share-input").value = "";
    if (data.shares.length === 0) brStatus("No shares were listed. If you know the share's name, type it below.");
    else brStatus("Shares on this server — tap one to look inside:");
    data.shares.forEach((s) => row(s, true, () => { br.share = s; br.parts = []; loadBrowser(); }));
    return;
  }
  brEl("br-use").disabled = false;
  data.folders.forEach((f) => row(f.ok ? f.name : f.name + "  (name not supported)", f.ok, () => { br.parts.push(f.name); loadBrowser(); }));
  const files = data.files === 1 ? "1 file" : data.files + " files";
  const folders = data.folders.length === 0 ? "no folders" : (data.folders.length === 1 ? "1 folder" : data.folders.length + " folders");
  brStatus("Here: " + files + ", " + folders + ".");
}

function useBrowserFolder() {
  if (!br || !br.share) return;
  form.elements["server_share_" + br.kind].value = br.share;
  form.elements["server_path_" + br.kind].value = "/" + br.parts.join("/");
  renderPickers();
  closeBrowser();
}

document.getElementById("pick-audio").addEventListener("click", () => openBrowser("audio"));
document.getElementById("pick-books").addEventListener("click", () => openBrowser("books"));
brEl("br-close").addEventListener("click", closeBrowser);
brEl("br-cancel").addEventListener("click", closeBrowser);
brEl("br-use").addEventListener("click", useBrowserFolder);
brEl("br-share-go").addEventListener("click", () => {
  const name = brEl("br-share-input").value.trim();
  if (name && br) { br.share = name; br.parts = []; loadBrowser(); }
});
brEl("br-share-input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); brEl("br-share-go").click(); } });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && br) closeBrowser(); });
// A different server means the earlier folder choices no longer apply.
form.elements["server_ip"].addEventListener("change", () => {
  for (const kind of ["audio", "books"]) {
    form.elements["server_share_" + kind].value = "";
    form.elements["server_path_" + kind].value = "";
  }
  renderPickers();
});
// Start installing the browsing tool in the background as soon as sync is switched on.
form.elements["enable_sync"].addEventListener("change", () => {
  if (on("enable_sync") && !prepared) {
    prepared = true;
    fetch("/setup/browse/prepare", {method: "POST"}).catch(() => {});
  }
});
renderPickers();

// ---- review screen ----
function renderReview() {
  const dev = form.elements["storage_device"];
  const devText = dev.value ? dev.options[dev.selectedIndex].text : "SD card (no external drive)";
  const devState = dev.value ? dev.options[dev.selectedIndex].dataset.state : "none";
  const yn = (b) => b ? "Yes" : "No";
  const groups = [];

  groups.push({title: "Wi-Fi hotspot", step: 0, rows: [
    ["Network name", val("wifi_ssid")], ["Password", val("wifi_password")], ["Hotspot IP", val("wifi_ip")]]});

  const storage = [["Media folder", val("media_root")], ["Drive", devText]];
  if (dev.value && (devState === "blank" || on("format_device"))) storage.push(["Erase", "ERASES " + dev.value, "warn"]);
  groups.push({title: "Storage", step: 1, rows: storage});

  const apps = [["Audiobookshelf", yn(on("install_audiobookshelf"))], ["Calibre-Web", yn(on("install_calibre_web"))]];
  if (on("install_audiobookshelf") || on("install_calibre_web")) {
    apps.push(["Patron login", val("patron_username") + " / " + val("patron_password")]);
  }
  groups.push({title: "Apps", step: 2, rows: apps});

  groups.push({title: "Request page", step: 3, rows: on("requests_enabled")
    ? [["Enabled", "Yes, port " + val("requests_port")]] : [["Enabled", "No"]]});

  groups.push({title: "Auto-sync", step: 4, rows: on("enable_sync") ? [
    ["Server", val("server_ip")],
    ["Login", val("server_username") ? val("server_username") + " / ••••" : "Guest"],
    ["Audiobooks", formatLoc(val("server_share_audio"), val("server_path_audio"))],
    ["Books", formatLoc(val("server_share_books"), val("server_path_books"))]] : [["Enabled", "No"]]});

  const hw = [];
  hw.push(["Status LEDs", on("leds_enabled")
    ? "GPIO " + val("pin_wifi") + " / " + val("pin_cweb") + " / " + val("pin_abs") : "No"]);
  hw.push(["Shutdown button", on("shutdown_enabled")
    ? "GPIO " + val("shutdown_pin") + ", hold " + val("shutdown_hold_secs") + "s" : "No"]);
  groups.push({title: "Hardware", step: 5, rows: hw});

  const review = document.getElementById("review");
  review.textContent = "";
  for (const g of groups) {
    const box = document.createElement("div");
    box.className = "rv-group";
    const head = document.createElement("div");
    head.className = "rv-head";
    const h = document.createElement("h3");
    h.textContent = g.title;
    const a = document.createElement("a");
    a.href = "#";
    a.textContent = "Edit";
    a.addEventListener("click", (e) => { e.preventDefault(); jumpTo(g.step); });
    head.append(h, a);
    box.append(head);
    for (const [k, v, cls] of g.rows) {
      const row = document.createElement("div");
      row.className = "rv-row" + (cls ? " " + cls : "");
      const kk = document.createElement("span");
      kk.textContent = k;
      const vv = document.createElement("strong");
      vv.textContent = v === "" ? "—" : v;
      row.append(kk, vv);
      box.append(row);
    }
    review.append(box);
  }
}

// ---- progress / live console ----
function showMode(mode) {
  const progress = mode === "progress";
  wizardBody.hidden = progress;
  progressCard.hidden = !progress;
  if (progress) progressCard.scrollIntoView({behavior: "smooth", block: "start"});
}

function setBadge(stepId, status) {
  const li = document.querySelector('li[data-step="' + stepId + '"]');
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

function showBanner(ok, needsReboot) {
  const b = document.getElementById("banner");
  b.className = ok ? "ok" : "fail";
  if (ok && needsReboot === false) {
    b.innerHTML = 'Settings applied. No reboot needed. ' +
      '<a href="/monitor">Open the Monitor</a> &middot; ' +
      '<a href="/welcome" target="_blank">View the welcome page</a>';
  } else if (ok) {
    b.innerHTML = 'Setup finished successfully. ' +
      '<a href="/welcome" target="_blank">View the welcome page</a> &middot; ' +
      '<a href="/welcome.pdf" target="_blank">Download the printable PDF</a>' +
      '<div style="margin-top:10px;font-weight:400">One last step: reboot the Pi to start your Wi-Fi hotspot. ' +
      'After it restarts, join the Wi-Fi network you chose and open the welcome page there. ' +
      'This page will stop working when the Pi reboots.</div>' +
      '<button type="button" id="reboot-btn" style="margin-top:10px;padding:10px 16px;border-radius:999px;border:0;font-weight:700;background:#10b981;color:#052e22;cursor:pointer">Reboot now</button>';
    const rb = document.getElementById("reboot-btn");
    rb.onclick = function () {
      if (!confirm("Reboot the Pi now? This page will disconnect.")) return;
      rb.disabled = true; rb.textContent = "Rebooting...";
      fetch("/setup/reboot", {method: "POST"})
        .then(function (r) { return r.json(); })
        .then(function (d) { rb.textContent = d.error ? "Could not reboot" : "Rebooting - rejoin the hotspot in about a minute"; })
        .catch(function () { rb.textContent = "Rebooting - rejoin the hotspot in about a minute"; });
    };
  } else {
    b.textContent = "Setup stopped due to an error — see the console below. You can go back, change settings and try again.";
  }
}

function showConfirm(data) {
  document.getElementById("confirm-question").textContent = data.question;
  document.getElementById("confirm-yes").textContent = data.yes_label || "Yes";
  document.getElementById("confirm-no").textContent = data.no_label || "No / skip";
  document.getElementById("confirm-overlay").style.display = "flex";

  const answer = (v) => {
    document.getElementById("confirm-overlay").style.display = "none";
    fetch("/setup/confirm", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirm_id: data.confirm_id, answer: v}),
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
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply & Set Up";
      editBtn.hidden = false;
      document.getElementById("confirm-overlay").style.display = "none";
      showBanner(data.ok, data.needs_reboot);
    } else if (data.kind === "start") {
      document.getElementById("console").innerHTML = "";
      document.getElementById("banner").className = "";
      document.getElementById("confirm-overlay").style.display = "none";
      editBtn.hidden = true;
      showMode("progress");
    }
  };
}

editBtn.addEventListener("click", () => { showMode("edit"); show(steps.length - 1); });

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  for (let i = 0; i < validators.length; i++) {
    const err = validators[i]();
    if (err) { show(i); showErr(err); return; }
  }
  applyBtn.disabled = true;
  applyBtn.textContent = "Running...";
  document.getElementById("banner").className = "";
  Object.keys(STEP_LABELS).forEach(id => setBadge(id, "pending"));

  const fd = new FormData(form);
  const payload = {};
  for (const [k, v] of fd.entries()) payload[k] = v;
  payload.format_device = fd.has("format_device");
  payload.rerun_all = fd.has("rerun_all");
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
    const msg = err.error || resp.statusText;
    if (es) es.close();
    applyBtn.disabled = false;
    applyBtn.textContent = "Apply & Set Up";
    showMode("edit");
    show(/wi-?fi/i.test(msg) ? 0 : steps.length - 1);
    showErr("Couldn't start: " + msg);
  }
});

// First paint, and reconnect to an in-progress run on page load/refresh.
show(0, false);
fetch("/setup/state").then(r => r.json()).then(state => {
  Object.entries(state.steps).forEach(([id, status]) => setBadge(id, status));
  if (state.running) {
    applyBtn.disabled = true;
    applyBtn.textContent = "Running...";
    showMode("progress");
    connectStream();
    if (state.pending_confirm) showConfirm(state.pending_confirm);
  } else if (state.finished) {
    showMode("progress");
    editBtn.hidden = false;
    showBanner(state.ok, state.needs_reboot);
  }
});
{% endraw %}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    if ADMIN["installed"]():
        return redirect(url_for("monitor"))
    return redirect(url_for("setup"))


@app.route("/setup", methods=["GET"])
def setup():
    cfg = load_config()
    devices = get_storage_devices()
    board = load_board_info()
    return render_template_string(
        FORM_TEMPLATE,
        cfg=cfg,
        installed_before=load_applied_snapshot() is not None,
        devices=devices,
        board=board,
        step_order=STEP_ORDER,
        step_order_json=json.dumps({sid: label for sid, label in STEP_ORDER}),
        logo_data_uri=LOGO_ON_DARK_DATA_URI,
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
    for kind in ("audio", "books"):
        for field in (f"server_share_{kind}", f"server_path_{kind}"):
            cfg["sync"][field] = (data.get(field) or "").strip() or cfg["sync"][field]

    if cfg["sync"]["enable_sync"]:
        # Same checks the folder browser uses: nothing unvalidated may reach
        # the sync script that runs as root.
        try:
            cfg["sync"]["server_ip"] = smb_clean_host(cfg["sync"]["server_ip"])
            for kind in ("audio", "books"):
                cfg["sync"][f"server_share_{kind}"] = smb_clean_share(cfg["sync"][f"server_share_{kind}"])
                cfg["sync"][f"server_path_{kind}"] = "/" + "/".join(smb_clean_path(cfg["sync"][f"server_path_{kind}"]))
        except ValueError as e:
            return jsonify({"error": f"Auto-sync: {e}"}), 400

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

    # Which steps need to run? Compare with what the last successful run
    # applied, so changing one setting doesn't redo the whole install.
    old = load_applied_snapshot()
    storage_changed = bool(format_device)
    if storage_device and not storage_changed:
        chosen = next((d for d in get_storage_devices() if d["path"] == storage_device), None)
        if chosen and chosen.get("ready_mountpoint") != cfg["storage"]["media_root"]:
            storage_changed = True
    plan, needs_reboot = plan_changes(old, cfg, storage_changed=storage_changed, force_all=bool(data.get("rerun_all")))

    save_config(cfg)

    thread = threading.Thread(target=run_pipeline, args=(cfg, storage_device, format_device, plan, needs_reboot), daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/setup/browse", methods=["POST"])
def setup_browse():
    """Lists the shares on a server (no share given) or the folders inside a
    share, for the folder picker on the Auto-sync step."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        server = smb_clean_host(data.get("server"))
        username = _smb_clean_login(data.get("username"), "username").strip()
        password = _smb_clean_login(data.get("password"), "password")
        share = smb_clean_share(data["share"]) if (data.get("share") or "").strip() else ""
        parts = smb_clean_path(data.get("path")) if share else []
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    problem = ensure_smbclient()
    if problem:
        return jsonify({"error": problem}), 503
    result, problem = smb_browse(server, username, password, share, parts)
    if problem:
        return jsonify({"error": problem}), 502
    return jsonify(result)


@app.route("/setup/browse/prepare", methods=["POST"])
def setup_browse_prepare():
    """Starts installing the folder-browser tool in the background as soon as
    auto-sync is ticked, so it's ready by the time Browse is tapped."""
    start_smbclient_install_async()
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


@app.route("/setup/reboot", methods=["POST"])
def setup_reboot():
    """Reboot the Pi (after a successful setup) so the saved hotspot starts."""
    with STATE_LOCK:
        allowed = STATE["finished"] and STATE["ok"] and not STATE["running"]
    if not allowed:
        return jsonify({"error": "Setup has not finished successfully yet."}), 409
    try:
        subprocess.Popen(
            ["systemd-run", "--on-active=3", "systemctl", "reboot"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        return jsonify({"error": f"Could not start the reboot: {e}"}), 500
    return jsonify({"ok": True})


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
                "needs_reboot": STATE["needs_reboot"],
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


# Login, Monitor and Settings pages (see admin_ui.py). Registered last so its
# before_request gate covers every route above.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin_ui  # noqa: E402

ADMIN = admin_ui.register(app, os.environ.get("LUSTYLIBRARY_ADMIN_DIR") or INSTALL_DIR, load_config, STATE, STATE_LOCK)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, threaded=True)
