# Lusty Library Installer

A drop-in setup wizard for building an offline ebook/audiobook server on a
Raspberry Pi. Supports **Raspberry Pi 3, 4, and 5**, on both the older
dhcpcd/hostapd network stack and the newer NetworkManager stack used by
current Raspberry Pi OS images.

It sets up:

- A Wi-Fi hotspot (custom SSID/password/IP) so the library is reachable
  without any other network
- Optional storage formatting/mounting for an attached USB drive
- [Audiobookshelf](https://www.audiobookshelf.org/) and/or
  [Calibre-Web](https://github.com/janeczku/calibre-web) via Docker, with a
  patron account automatically created in whichever you install
- A patron-facing book/audiobook request page on port 5000, saving to a
  CSV in the media folder
- A printable "Welcome to the Lusty Library" page (HTML and PDF), generated
  automatically once setup finishes, with the Lusty Library logo, Wi-Fi
  info, the patron account, and QR codes for eBooks, Audiobooks and
  requests
- Optional one-way sync of audiobooks/books from another server on your
  network, triggered automatically the instant an Ethernet cable is
  plugged in (event-driven — no polling), with optional SMB
  username/password if the share isn't guest-accessible
- Optional status LEDs (Wi-Fi / Calibre-Web / Audiobookshelf) on GPIO,
  tested during setup and kept updated afterward by a background service
- Optional GPIO shutdown button (press-and-hold), hardware-tested during
  setup before its watcher service is installed

## Quick install

```bash
curl -sSL https://raw.githubusercontent.com/ryanmnolan/lustylibrary-installer/main/install.sh | sudo bash
```

This installs git/python/Docker, clones the repo to
`/opt/lustylibrary-installer`, and starts the setup wizard as a systemd
service. Then open:

```
http://<pi-ip>:9000/setup
```

## What the wizard shows you

Every step (Wi-Fi, storage, Docker, apps, sync, LEDs, shutdown button)
runs live in the browser: a checklist shows each step as pending /
running / done / error, and a console panel streams the exact commands
being run and their output as they happen. If a step fails, it's shown
clearly with the real error message and the remaining steps are skipped
rather than the page silently reloading.

## Managing the service

```bash
sudo systemctl status lustylibrary-setup.service
sudo systemctl restart lustylibrary-setup.service
```

Install log: `/var/log/lustylibrary-install.log`
Sync log (if enabled): `/var/log/sync_from_server.log`

## Book/audiobook request page

Runs on port 5000 (`http://<pi-ip>:5000/`) as its own systemd service,
`lustylibrary-requests.service`, separate from the setup wizard on port
9000. Anyone on the hotspot can submit a title/author/type/notes; each
request is appended to `requests.csv` in the media folder (e.g.
`/mnt/media/requests.csv`) and immediately shown in a table on the same
page, newest first, with a one-click "mark fulfilled" toggle. Writes are
locked and done via a write-then-rename so concurrent submissions can't
corrupt the file. Toggle it off in the wizard, or change its port, under
"Book/Audiobook Requests."

## Logo

The Lusty Library logo (`lusty_library_logo.png`, background removed) is
embedded directly into every page the wizard serves — the setup wizard
itself (port 9000), the book request page (port 5000), and the welcome
page below — so nothing extra needs to be hosted or linked separately.

## Patron account

Under "Apps to install," set a single patron username/password (default
`book`/`book`). During setup, the **"accounts"** step logs into whichever
apps you installed and creates that account for real:

- **Calibre-Web**: logs in with the app's documented first-run admin
  account (`admin`/`admin123`) and submits its own "add user" form to
  create the patron account — the same form the web UI itself uses, not a
  private API.
- **Audiobookshelf**: uses the server's one-time setup flow (`/status` +
  `/init`) to set the patron username/password as the root account. If the
  server was already initialized (e.g. re-running setup), it just confirms
  those credentials still log in instead of overwriting anything.

Both are best-effort and non-fatal — if a login page or form has changed
in a newer app version, the step logs exactly what failed (status codes,
etc.) and setup keeps going rather than aborting. Whatever actually
happened (`created`, `already exists`, or `failed: ...`) is what shows up
on the welcome page — never a hand-typed note that might not match reality.

## Welcome page

Once setup finishes, a printable instructions page is generated at:

```
http://<pi-ip>:9000/welcome        (HTML)
http://<pi-ip>:9000/welcome.pdf    (PDF, for printing)
```

Static copies are also saved to `welcome.html` and `welcome.pdf` in the
media folder (e.g. `/mnt/media/welcome.pdf`) so they can be opened or
printed without the wizard running. It's built entirely from what the
wizard actually configured/did — only showing eBooks, Audiobooks,
requests, the shutdown button, or the LED legend for the pieces that are
actually installed — and includes:

- The Lusty Library logo and Wi-Fi network name/password
- A QR code (and the URL) for eBooks (Calibre-Web), if installed, plus the
  patron login it actually ended up with
- The Audiobookshelf server address plus QR codes to install the app on
  iOS/Android, if installed, plus the patron login
- A QR code for the book/audiobook request page, if enabled
- Shutdown button instructions, if wired up
- The LED color legend (Green = Wi-Fi ready, Yellow = eBooks ready,
  Blue = Audiobooks ready), if status LEDs are wired up

### QR codes, installed and removed automatically

QR codes are generated offline (no external QR API). The **"qr_codes"**
step installs the `qrcode` Python package just long enough to generate
every QR image this install needs, verifies each one actually opened as a
valid image, caches the results to `qr_cache.json` next to the installer,
and then **uninstalls `qrcode` again** — it's only ever needed once, so it
isn't kept as a permanent dependency. The welcome page and PDF read from
that cache afterward, so they keep working correctly even after the
package is gone and across service restarts. If installing `qrcode` fails
(e.g. no network at that moment), the page just falls back to showing
plain URLs instead of QR images. If the uninstall step itself can't remove
the package afterward (rare, seen on some `externally-managed-environment`
Python setups), the log says so honestly instead of claiming success — it's
harmless either way since the QR images are already cached by that point.

## Auto-sync trigger

When auto-sync is enabled, the wizard detects your wired interface
(`eth0`/`end0`) and installs whichever hook matches your OS's network
stack:

- **NetworkManager** (current Raspberry Pi OS): a dispatcher script at
  `/etc/NetworkManager/dispatcher.d/99-lustylibrary-sync`
- **dhcpcd** (older images): a udev rule at
  `/etc/udev/rules.d/99-lustylibrary-sync.rules` that fires on the
  interface's link (carrier) state changing to up

Either way, plugging in the cable starts `lustylibrary-sync.service`
(a systemd oneshot unit) immediately — no polling, no cron job. You can
also trigger it by hand to test:

```bash
sudo systemctl start lustylibrary-sync.service
journalctl -u lustylibrary-sync.service -f
```

If the server share needs a login instead of guest access, enter a
username/password in the wizard; they're stored in
`/etc/lustylibrary-sync-credentials` (root-only, mode 600) and referenced
by the mount, rather than embedded in the sync script itself.

## Status LEDs (optional)

Check "I have status LEDs wired up" in the wizard and set the BCM pin for
each of the Wi-Fi, Calibre-Web and Audiobookshelf LEDs (defaults 17/27/22,
matching a stock 3-LED build). Setup then:

1. Runs a **test screen** up front: blinks every configured LED twice and
   asks you to confirm they all lit up, before installing anything.
2. Tests **each LED again right when its own feature comes up** — the
   Wi-Fi LED after the hotspot is configured, and the Calibre-Web /
   Audiobookshelf LEDs after their containers start (only for apps you
   chose to install) — each with its own yes/no confirmation. Setup
   doesn't move past that step until you answer (or 15 minutes pass).
3. Only after everything else is verified does it install
   `/usr/local/bin/status_leds.py` and `lustylibrary-leds.service`, so
   the ongoing status service isn't fighting the tests for the same
   GPIO pins.

The status service shows: Wi-Fi LED solid once the hotspot IP is up,
fast-toggling while a sync is in progress, and blinking otherwise;
Calibre-Web/Audiobookshelf LEDs solid once their container is running,
blinking otherwise. If GPIO hardware isn't detected (e.g. running this on
non-Pi hardware, or during development), all LED steps are skipped
automatically rather than failing setup.

## Shutdown button (optional)

Check "I have a shutdown button wired up," set its BCM pin (default 26)
and hold time (default 2.0s). Setup then:

1. Actually waits for a real press on that pin (20-second window) and
   measures how long you held it — this is a hardware check, not a
   self-reported yes/no, so it also catches a wrong pin number or bad
   wiring on its own.
2. Installs `/usr/local/bin/shutdown_button.py` and
   `lustylibrary-shutdown-button.service` afterward regardless of the
   test result (a missed press during the 20s window doesn't mean the
   wiring is wrong), logging a clear warning if nothing was detected.

On a long press, the service stops `lustylibrary-leds.service` (if status
LEDs are enabled), flashes all configured LEDs a few times, then calls
`poweroff`. It runs as its own root systemd service, so it doesn't need
passwordless `sudo` configured for any user.

## Troubleshooting LEDs / the shutdown button

If a status LED or the shutdown button doesn't respond, but the setup log
shows no error for that step, the setup wizard now checks and logs which
GPIO backend gpiozero actually resolved to (`GPIO backend in use: ...`) —
check that line in the console/install log first. On newer Raspberry Pi OS
releases (Bookworm and trixie), the classic `RPi.GPIO` module can import
fine without being able to drive a pin at all, unless the `rpi-lgpio`
replacement is installed; the wizard now catches this and reports it
instead of silently continuing.

For a manual, standalone check (no need to re-run the whole wizard), SSH
into the Pi and run:

```bash
sudo python3 /opt/lustylibrary-installer/gpio_diagnostic.py
```

It prints the GPIO backend in use, then walks through each configured LED
(solid on for 5s, one at a time, asking you to confirm) and watches the
button pin's raw state for 15s so you can see whether a press registers at
all. That isolates a software/backend problem from a wiring problem (wrong
pin, reversed LED polarity, missing resistor, or a button wired to 3.3V
instead of GND).

## Known limitations

- The setup wizard has no login and runs as root — only expose port 9000
  on a trusted network.
- The request page (port 5000) also has no login and its "mark
  fulfilled" toggle has no confirmation — fine on a private hotspot,
  not for an open network.
- `config.yml` (the wizard's own saved settings) stores the Wi-Fi, SMB,
  and patron account passwords in plain text.
- Automatic patron-account creation depends on each app's current
  login/setup forms; if a future Calibre-Web or Audiobookshelf release
  changes them, that one step logs a clear failure and setup continues —
  you'd just create the account by hand in that app's own UI instead.
- The welcome PDF uses a simpler layout than the on-screen welcome page
  (the PDF library doesn't support the same CSS), so print styling may
  differ slightly, but the content is identical.
