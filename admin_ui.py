"""
Lusty Library admin area: login, Monitor page and Settings page.

Once a first install has finished, the setup wizard stops being open to
everyone: the wizard, Monitor and Settings all sit behind one fixed password.
(The password is deliberately a constant, not a setting - there is nothing to
change or reset.) The welcome sheet stays public because patrons scan it.

Everything here is registered onto the setup_gui Flask app with register().
"""
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from flask import (Response, jsonify, redirect, render_template_string, request,
                   session, url_for)

ADMIN_PASSWORD = "LustyLibrary"
SYNC_LOG = Path("/var/log/sync_from_server.log")
SYNC_FLAG = Path("/tmp/sync_in_progress")
ETH_CARRIER = Path("/sys/class/net/eth0/carrier")
CONTAINERS = [("audiobookshelf", "Audiobookshelf"), ("calibre-web-automated", "Calibre-Web Automated")]

# Pages/endpoints that work without logging in. Only the setup wizard (and its
# /setup/... actions) is password-protected after the first install; Monitor and
# Settings are open.
PUBLIC_ENDPOINTS = {"login", "logout", "welcome", "welcome_pdf", "static",
                    "monitor", "settings", "api_status", "admin_sync", "admin_reboot",
                    "admin_shutdown", "admin_hardware_test"}
# Needed to finish the very first install (reboot button etc.) in the same
# browser session that ran it, before the service restarts.
FIRST_RUN_ENDPOINTS = {"setup", "setup_state", "setup_stream", "setup_reboot", "setup_confirm"}


# ---------------------------------------------------------------------------
# status helpers (each one is defensive: a failing probe must never break the page)
# ---------------------------------------------------------------------------

def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def wifi_status(cfg):
    want_ip = cfg["wifi"]["ip"]
    rc, out = _run(["ip", "-4", "-o", "addr", "show", "wlan0"])
    up = rc == 0 and want_ip in out
    clients = None
    if up and shutil.which("iw"):
        rc, out = _run(["iw", "dev", "wlan0", "station", "dump"])
        if rc == 0:
            clients = len(re.findall(r"^Station ", out, re.M))
    return {"up": up, "ssid": cfg["wifi"]["ssid"], "ip": want_ip, "clients": clients}


def docker_status():
    rc, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}|{{.Status}}"], timeout=10)
    found = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                found[parts[0]] = parts
    res = []
    for name, label in CONTAINERS:
        row = found.get(name)
        res.append({
            "name": name, "label": label,
            "up": bool(row and row[1] == "running"),
            "detail": row[2] if row else ("not installed" if rc == 0 else "Docker not reachable"),
        })
    return res


def parse_throttled(raw):
    """vcgencmd get_throttled -> flags. Bit 0 = under-voltage now, bit 16 =
    under-voltage since boot; bit 1/17 = frequency capped; bit 2/18 = throttled;
    bit 3/19 = soft temp limit."""
    m = re.search(r"0x([0-9a-fA-F]+)", raw or "")
    if not m:
        return None
    v = int(m.group(1), 16)
    return {
        "raw": f"0x{v:x}",
        "undervoltage_now": bool(v & 0x1),
        "undervoltage_past": bool(v & 0x10000),
        "throttled_now": bool(v & 0x4),
        "throttled_past": bool(v & 0x40000),
        "ok": v == 0,
    }


def power_status():
    if not shutil.which("vcgencmd"):
        return None
    rc, out = _run(["vcgencmd", "get_throttled"])
    return parse_throttled(out) if rc == 0 else None


def temperature_c():
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def disk_status(media_root):
    p = Path(media_root)
    try:
        mounted = os.path.ismount(p)
        u = shutil.disk_usage(p)
    except OSError:
        return {"ok": False, "mounted": False, "path": str(media_root)}
    return {"ok": True, "mounted": mounted, "path": str(media_root),
            "total": u.total, "free": u.free, "used": u.used}


def _count_files(root, limit=100000):
    n = 0
    try:
        for _dp, _dn, files in os.walk(root):
            n += len([f for f in files if not f.startswith(".")])
            if n > limit:
                break
    except OSError:
        return None
    return n


def library_status(media_root):
    """waiting = books not yet in the library: files in the importer's ingest
    folder plus books the sync has copied to its staging folder but not yet
    handed over (on a first sync the copy can run for hours, during which the
    ingest folder alone would read 0)."""
    root = Path(media_root)
    ingest = root / "ingest"
    stage = root / ".sync_state" / "staging"
    failed_dir = root / "config" / "calibre" / "processed_books" / "failed"
    in_ingest = _count_files(ingest) if ingest.exists() else None
    in_stage = _count_files(stage) if stage.exists() else 0
    waiting = None if in_ingest is None else in_ingest + (in_stage or 0)
    return {
        "waiting": waiting,
        "importing": in_ingest,
        "copying": in_stage or 0,
        "failed": _count_files(failed_dir) if failed_dir.exists() else 0,
        "failed_names": failed_names(media_root),
        "ebooks_total": count_library_books(media_root),
        "audiobooks_total": count_audiobooks(media_root),
    }


AUDIO_EXT = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wav"}


def count_library_books(media_root):
    """Books Calibre-Web has imported (rows in its Calibre metadata.db)."""
    db = Path(media_root) / "books" / "metadata.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            return con.execute("select count(*) from books").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def count_audiobooks(media_root, limit=200000):
    """Audiobooks = folders that hold audio files (one per book)."""
    root = Path(media_root) / "audiobooks"
    if not root.exists():
        return None
    folders, seen = set(), 0
    try:
        for dp, _dn, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                    folders.add(dp)
                    break
            seen += 1
            if seen > limit:
                break
    except OSError:
        return None
    return len(folders)


def failed_names(media_root, limit=10):
    d = Path(media_root) / "config" / "calibre" / "processed_books" / "failed"
    names = []
    try:
        for _dp, _dn, files in os.walk(d):
            names += [f for f in files if not f.startswith(".")]
            if len(names) > limit:
                break
    except OSError:
        pass
    return sorted(names)[:limit]


def books_to_copy(text):
    """'Books: N new file(s) to hand to the library.' from the latest sync."""
    found = re.findall(r"^Books: (\d+) new file", text, re.M)
    return int(found[-1]) if found else None


def parse_sync_log(text):
    """Finds the most recent sync in the log. Returns dict(state, when, result)."""
    start_re = re.compile(r"^==== (.+?) — sync start ====", re.M)
    done_re = re.compile(r"^==== (.+?) — sync done ====", re.M)
    starts = list(start_re.finditer(text))
    if not starts:
        return {"last": None, "result": None}
    last = starts[-1]
    block = text[last.start():]
    done = done_re.search(block)
    if not done:
        return {"last": last.group(1), "result": "running"}
    body = block[:done.start()]
    if re.search(r"ERROR|WARNING", body):
        res = "problem"
    else:
        res = "ok"
    return {"last": done.group(1), "result": res}


def sync_status():
    try:
        text = SYNC_LOG.read_text(errors="replace")[-200000:]
    except OSError:
        text = ""
    info = parse_sync_log(text)
    info["books_to_copy"] = books_to_copy(text) if info.get("result") == "running" else None
    info["running"] = SYNC_FLAG.exists()
    try:
        info["ethernet"] = ETH_CARRIER.read_text().strip() == "1"
    except OSError:
        info["ethernet"] = False
    return info


def collect_status(cfg):
    media_root = cfg["storage"]["media_root"]
    return {
        "time": time.strftime("%F %T"),
        "wifi": wifi_status(cfg),
        "containers": docker_status(),
        "sync": sync_status(),
        "library": library_status(media_root),
        "disk": disk_status(media_root),
        "power": power_status(),
        "temp_c": temperature_c(),
    }


# ---------------------------------------------------------------------------
# hardware test (LEDs + power button) - runs in a background thread
# ---------------------------------------------------------------------------

TEST = {"running": False, "lines": [], "done": False}
TEST_LOCK = threading.Lock()


def _t(line):
    with TEST_LOCK:
        TEST["lines"].append(line)


def _release_gpio():
    """Closes this process's GPIO handle so no pin stays claimed (a claimed
    pin would stop the LED and button services from starting again)."""
    try:
        import gpiozero
        if gpiozero.Device.pin_factory is not None:
            gpiozero.Device.pin_factory.close()
            gpiozero.Device.pin_factory = None
    except Exception:  # noqa: BLE001
        pass


def _hardware_test(cfg):
    units = ("lustylibrary-leds.service", "lustylibrary-shutdown-button.service")
    try:
        _t("Pausing the status LEDs and the power-button watcher...")
        for u in units:
            _run(["systemctl", "stop", u], timeout=20)
        _run(["pkill", "-f", "/usr/local/bin/status_leds.py"])
        time.sleep(0.8)
        try:
            from gpiozero import LED, Button
        except ImportError:
            _t("ERROR: gpiozero isn't installed, so the hardware can't be tested.")
            return
        leds = cfg["leds"]
        if leds["enabled"]:
            for label, pin in (("Wi-Fi", leds["pin_wifi"]), ("Calibre-Web", leds["pin_cweb"]),
                               ("Audiobookshelf", leds["pin_abs"])):
                _t(f"Flashing the {label} LED (GPIO{pin})...")
                led = None
                try:
                    led = LED(pin)
                    for _ in range(3):
                        led.on(); time.sleep(0.35); led.off(); time.sleep(0.25)
                except Exception as e:  # noqa: BLE001
                    _t(f"ERROR: couldn't drive GPIO{pin}: {e}")
                finally:
                    if led is not None:
                        try:
                            led.close()
                        except Exception:  # noqa: BLE001
                            pass
        else:
            _t("Status LEDs aren't enabled - skipping the LED test.")
        sb = cfg["shutdown_button"]
        if sb["enabled"]:
            _t(f"Now press the power button (GPIO{sb['pin']}) - you have 15 seconds. It will NOT shut the Pi down.")
            btn = None
            try:
                btn = Button(sb["pin"], pull_up=True)
                if btn.wait_for_press(timeout=15):
                    _t("Button press detected - the button works.")
                    btn.wait_for_release(timeout=5)
                else:
                    _t("No button press seen in 15 seconds. Check the wiring/pin.")
            except Exception as e:  # noqa: BLE001
                _t(f"ERROR: couldn't read GPIO{sb['pin']}: {e}")
            finally:
                if btn is not None:
                    try:
                        btn.close()
                    except Exception:  # noqa: BLE001
                        pass
        else:
            _t("Power button isn't enabled - skipping the button test.")
    finally:
        _release_gpio()
        time.sleep(0.5)
        for u in units:
            _run(["systemctl", "start", u], timeout=20)
        _t("Status LEDs and power-button watcher restarted. Test finished.")
        with TEST_LOCK:
            TEST["running"] = False
            TEST["done"] = True


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

BASE_CSS = """
:root { --bg:#111827; --card:#1f2937; --line:#374151; --muted:#9ca3af; --accent:#10b981; --bad:#f87171; --warn:#fbbf24; }
* { box-sizing:border-box; }
body { font-family:system-ui,sans-serif; background:var(--bg); color:#f9fafb; margin:0; }
.wrap { max-width:720px; margin:0 auto; padding:18px 14px 40px; }
.top { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:8px; }
.top h1 { font-size:22px; margin:0; }
nav a { color:#6ee7b7; margin-left:14px; text-decoration:none; font-size:15px; }
nav a.cur { color:#fff; font-weight:600; }
.card { background:var(--card); border-radius:14px; padding:16px; margin-bottom:14px; }
.card h2 { font-size:16px; margin:0 0 10px; color:#e5e7eb; }
.row { display:flex; justify-content:space-between; gap:10px; padding:7px 0; border-bottom:1px solid var(--line); font-size:15px; }
.row:last-child { border-bottom:0; }
.row .k { color:var(--muted); } .row .v { text-align:right; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; background:var(--muted); }
.good .dot { background:var(--accent); } .bad .dot { background:var(--bad); } .warn .dot { background:var(--warn); }
.bad { color:#fecaca; } .warn { color:#fde68a; }
button { background:var(--accent); color:#022c22; border:0; padding:12px 18px; border-radius:999px; font-size:15px; font-weight:600; cursor:pointer; }
button.danger { background:var(--bad); color:#450a0a; } button.ghost { background:#374151; color:#e5e7eb; }
button:disabled { opacity:.5; cursor:not-allowed; }
.btns { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }
.note { color:var(--muted); font-size:14px; line-height:1.45; margin:8px 0 0; }
input { width:100%; padding:12px; border-radius:8px; border:1px solid #4b5563; background:#030712; color:#f9fafb; font-size:16px; }
pre { background:#030712; border-radius:8px; padding:10px; font-size:13px; white-space:pre-wrap; max-height:220px; overflow:auto; }
.err { color:#fca5a5; margin-top:10px; }
"""

LOGIN_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lusty Library - Setup password</title>
<style>{{ css|safe }}</style></head><body><div class="wrap" style="max-width:420px;padding-top:60px">
<div class="card"><h2>Lusty Library setup</h2>
<form method="post"><input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<input type="hidden" name="next" value="{{ next }}">
<div class="btns"><button type="submit">Sign in</button></div></form></div></div></body></html>"""

LAYOUT_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lusty Library - {{ title }}</title>
<style>{{ css|safe }}</style></head><body><div class="wrap">
<div class="top"><h1>Lusty Library</h1><nav>
<a href="/monitor" class="{{ 'cur' if title=='Monitor' }}">Monitor</a>
<a href="/settings" class="{{ 'cur' if title=='Settings' }}">Settings</a></nav></div>"""

MONITOR_TEMPLATE = LAYOUT_HEAD + """
<div id="alerts"></div>
<div class="card"><h2>Services</h2><div id="services"></div></div>
<div class="card"><h2>Library</h2><div id="library"></div></div>
<div class="card"><h2>Sync</h2><div id="sync"></div></div>
<div class="card"><h2>Health</h2><div id="health"></div></div>
<p class="note" id="stamp"></p>
<script>
function row(k, v, cls){ return '<div class="row"><span class="k">'+k+'</span><span class="v '+(cls||'')+'">'+v+'</span></div>'; }
function dot(ok, text){ return '<span class="'+(ok?'good':'bad')+'"><span class="dot"></span>'+text+'</span>'; }
function gb(b){ return (b/1073741824).toFixed(1)+' GB'; }
function esc(s){ return String(s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function load(){
  let d;
  try { const r = await fetch('/api/status'); if (r.status==401){ location='/login'; return; } d = await r.json(); }
  catch(e){ document.getElementById('stamp').textContent='Can\\'t reach the Pi right now - retrying...'; return; }
  const alerts = []; let power = '';
  let s = row('Wi-Fi hotspot', dot(d.wifi.up, d.wifi.up ? 'Up ('+esc(d.wifi.ssid)+')' : 'Down'));
  if (d.wifi.clients!==null) s += row('Phones connected', d.wifi.clients);
  d.containers.forEach(c=>{ s += row(c.label, dot(c.up, c.up ? 'Running' : esc(c.detail))); if(!c.up) alerts.push(c.label+' is not running.'); });
  if (!d.wifi.up) alerts.push('The Wi-Fi hotspot is down.');
  document.getElementById('services').innerHTML = s;
  const L = d.library;
  let l = row('Ebooks in library', L.ebooks_total===null?'-':L.ebooks_total);
  l += row('Audiobooks in library', L.audiobooks_total===null?'-':L.audiobooks_total);
  l += row('Ebooks waiting to import', L.waiting===null?'-':L.waiting);
  if (L.copying>0) l += row('Still copying from server', L.copying+' so far');
  if (d.sync.running && d.sync.books_to_copy!==null) l += row('New ebooks this sync', d.sync.books_to_copy);
  l += row('Failed imports', L.failed===null?'-':L.failed, L.failed>0?'warn':'');
  if (L.failed>0){ alerts.push(L.failed+' ebook(s) failed to import.');
    l += '<div class="note">Failed: '+L.failed_names.map(esc).join(', ')+(L.failed>L.failed_names.length?' ...':'')+'</div>'; }
  if (d.disk.ok){ const pct=Math.round(100*d.disk.free/d.disk.total);
    l += row('Drive', dot(d.disk.mounted, d.disk.mounted ? 'Mounted' : 'Not a separate drive'));
    l += row('Free space', gb(d.disk.free)+' of '+gb(d.disk.total)+' ('+pct+'%)', pct<10?'bad':'');
    if (pct<10) alerts.push('The drive is almost full.');
  } else { l += row('Drive', dot(false,'Not found')); alerts.push('The media drive is not available.'); }
  document.getElementById('library').innerHTML = l;
  const S = d.sync;
  let y = row('Sync right now', S.running ? dot(true,'Running') : 'Idle');
  y += row('Ethernet cable', S.ethernet ? 'Plugged in' : 'Not plugged in');
  let last = S.last ? esc(S.last) : 'Never';
  if (S.last && S.result==='ok') last += ' - OK'; if (S.result==='problem'){ last += ' - finished with problems'; alerts.push('The last sync had problems.'); }
  y += row('Last sync', last);
  document.getElementById('sync').innerHTML = y;
  let h = '';
  if (d.power){ const p=d.power;
    h += row('Power', p.undervoltage_now ? dot(false,'Under-voltage right now') : p.undervoltage_past ? '<span class="warn"><span class="dot"></span>Dipped since boot</span>' : dot(true,'Good'));
    if (p.undervoltage_now||p.undervoltage_past) power = (p.undervoltage_now ? 'LOW POWER RIGHT NOW. ' : 'The Pi dipped to low power since it started. ') + 'Use the official power supply and a short, good cable. A weak supply can corrupt the drive.';
  } else h += row('Power', 'Not available');
  h += row('Temperature', d.temp_c===null?'-':d.temp_c+' °C', d.temp_c>=75?'bad':d.temp_c>=65?'warn':'');
  document.getElementById('health').innerHTML = h;
  document.getElementById('alerts').innerHTML = (power ? '<div class="card" style="background:#7f1d1d;color:#fff"><h2 style="color:#fff">⚡ Power problem</h2>'+esc(power)+'</div>' : '') + (alerts.length ? '<div class="card bad"><h2>Needs attention</h2>'+alerts.map(a=>'<div>• '+esc(a)+'</div>').join('')+'</div>' : '');
  document.getElementById('stamp').textContent = 'Updated '+d.time+' - refreshes every 5 seconds.';
}
load(); setInterval(load, 5000);
</script></div></body></html>"""

SETTINGS_TEMPLATE = LAYOUT_HEAD + """
<div class="card"><h2>Sync now</h2>
<p class="note" style="margin-top:0">Copies new books from your home server. It needs the Ethernet cable plugged in and your server set up under Auto-sync.</p>
<div class="btns"><button id="b-sync">Start sync</button></div><p class="note" id="m-sync"></p></div>
<div class="card"><h2>Test the LEDs and power button</h2>
<p class="note" style="margin-top:0">Flashes each LED, then waits for you to press the power button. The status LEDs pause during the test and come back after.</p>
<div class="btns"><button id="b-test" class="ghost">Run hardware test</button></div><pre id="t-out" style="display:none"></pre></div>
<div class="card"><h2>Power</h2>
<div class="btns"><button id="b-reboot" class="ghost">Reboot</button><button id="b-off" class="danger">Shut down</button></div>
<p class="note">Shut down before unplugging the Pi. The hotspot goes away until it is powered on again.</p><p class="note" id="m-power"></p></div>
<div class="card"><h2>Advanced</h2>
<p class="note" style="margin-top:0">Re-run the setup wizard to change the hotspot, drive, apps or sync settings. It asks for the admin password. This will restart the hotspot and apps.</p>
<div class="btns"><a href="/setup"><button class="ghost" type="button">Re-run setup</button></a></div></div>
<script>
async function post(url){ const r = await fetch(url,{method:'POST'}); let j={}; try{ j=await r.json(); }catch(e){} return [r.ok,j]; }
document.getElementById('b-sync').onclick = async ()=>{ const [ok,j]=await post('/admin/sync'); document.getElementById('m-sync').textContent = j.message||j.error||''; };
document.getElementById('b-reboot').onclick = async ()=>{ if(!confirm('Reboot the Lusty Library now?')) return; const [ok,j]=await post('/admin/reboot'); document.getElementById('m-power').textContent = j.message||j.error||''; };
document.getElementById('b-off').onclick = async ()=>{ if(!confirm('Shut down the Lusty Library now?')) return; const [ok,j]=await post('/admin/shutdown'); document.getElementById('m-power').textContent = j.message||j.error||''; };
document.getElementById('b-test').onclick = async ()=>{
  const out=document.getElementById('t-out'); out.style.display='block'; out.textContent='Starting...';
  const [ok,j]=await post('/admin/hardware-test'); if(!ok){ out.textContent=j.error||'Could not start.'; return; }
  const t=setInterval(async()=>{ const r=await fetch('/admin/hardware-test'); const d=await r.json(); out.textContent=d.lines.join('\\n'); if(d.done){ clearInterval(t);} },1000);
};
</script></div></body></html>"""


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def register(app, install_dir, load_config, state, state_lock):
    marker = Path(install_dir) / ".installed"

    key_path = Path(install_dir) / ".secret_key"
    try:
        key = key_path.read_bytes()
    except OSError:
        key = os.urandom(32)
        try:
            key_path.write_bytes(key)
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    app.secret_key = key
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    def installed():
        return marker.exists()

    def mark_installed():
        try:
            marker.write_text(time.strftime("%F %T") + "\n")
        except OSError:
            pass

    def first_run_window():
        with state_lock:
            return bool(state["finished"] and state["ok"] and not state["running"])

    @app.before_request
    def gate():
        # A finished successful run records that this Pi is installed.
        if not installed() and first_run_window():
            mark_installed()
        if not installed():
            return None
        ep = request.endpoint
        if ep in PUBLIC_ENDPOINTS or session.get("admin"):
            return None
        if ep in FIRST_RUN_ENDPOINTS and first_run_window():
            return None
        if request.path.startswith(("/api/", "/admin/")) or \
                (request.path.startswith("/setup/") and request.method != "GET"):
            return jsonify({"error": "Please sign in."}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = request.values.get("next") or "/setup"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/setup"
        error = None
        if request.method == "POST":
            given = (request.form.get("password") or "").encode()
            if hmac.compare_digest(given, ADMIN_PASSWORD.encode()):
                session["admin"] = True
                session.permanent = False
                return redirect(nxt)
            time.sleep(1)  # slow down guessing
            error = "That password isn't right."
        return render_template_string(LOGIN_TEMPLATE, css=BASE_CSS, error=error, next=nxt)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/monitor")
    def monitor():
        return render_template_string(MONITOR_TEMPLATE, css=BASE_CSS, title="Monitor")

    @app.route("/settings")
    def settings():
        return render_template_string(SETTINGS_TEMPLATE, css=BASE_CSS, title="Settings")

    @app.route("/api/status")
    def api_status():
        return jsonify(collect_status(load_config()))

    @app.route("/admin/sync", methods=["POST"])
    def admin_sync():
        if SYNC_FLAG.exists():
            return jsonify({"message": "A sync is already running."})
        if not Path("/usr/local/bin/sync_from_server.sh").exists():
            return jsonify({"error": "Auto-sync isn't set up. Use Re-run setup to enable it."}), 409
        try:
            if ETH_CARRIER.read_text().strip() != "1":
                return jsonify({"error": "No Ethernet cable detected. Plug the Pi into your home network with a cable, then try again."}), 409
        except OSError:
            return jsonify({"error": "No Ethernet port found on this Pi."}), 409
        rc, out = _run(["systemctl", "start", "--no-block", "lustylibrary-sync.service"], timeout=15)
        if rc != 0:
            return jsonify({"error": "Couldn't start the sync: " + out.strip()}), 500
        return jsonify({"message": "Sync started. Watch the Monitor page for progress."})

    def _power(action, msg):
        try:
            subprocess.Popen(["systemd-run", "--on-active=3", "systemctl", action],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            return jsonify({"error": f"Couldn't do that: {e}"}), 500
        return jsonify({"message": msg})

    @app.route("/admin/reboot", methods=["POST"])
    def admin_reboot():
        return _power("reboot", "Rebooting in a few seconds. Give it about a minute, then reconnect to the Wi-Fi.")

    @app.route("/admin/shutdown", methods=["POST"])
    def admin_shutdown():
        return _power("poweroff", "Shutting down in a few seconds. Wait for the green light to stop before unplugging.")

    @app.route("/admin/hardware-test", methods=["GET", "POST"])
    def admin_hardware_test():
        if request.method == "GET":
            with TEST_LOCK:
                return jsonify({"lines": list(TEST["lines"]), "done": TEST["done"], "running": TEST["running"]})
        with state_lock:
            if state["running"]:
                return jsonify({"error": "Setup is running right now."}), 409
        with TEST_LOCK:
            if TEST["running"]:
                return jsonify({"error": "A test is already running."}), 409
            TEST.update(running=True, done=False, lines=[])
        threading.Thread(target=_hardware_test, args=(load_config(),), daemon=True).start()
        return jsonify({"ok": True})

    return {"installed": installed}
