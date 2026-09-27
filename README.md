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
  [Calibre-Web](https://github.com/janeczku/calibre-web) via Docker
- A patron-facing book/audiobook request page on port 5000, saving to a
  CSV in the media folder
- A printable "Welcome to the Lusty Library" page, generated automatically
  once setup finishes, with the Lusty Library logo, Wi-Fi info, and QR
  codes for eBooks, Audiobooks and requests
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

## Welcome page

Once setup finishes, a printable instructions page is generated at:

```
http://<pi-ip>:9000/welcome
```

A static copy is also saved to `welcome.html` in the media folder (e.g.
`/mnt/media/welcome.html`) so it can be opened or printed without the
wizard running. It's built from whatever you actually enabled — only
showing eBooks, Audiobooks, requests, the shutdown button, or the LED
legend for the pieces that are actually installed — and includes:

- The Lusty Library logo and Wi-Fi network name/password
- A QR code (and the URL) for eBooks (Calibre-Web), if installed
- The Audiobookshelf server address plus QR codes to install the app on
  iOS/Android, if installed
- A QR code for the book/audiobook request page, if enabled
- Shutdown button instructions, if wired up
- The LED color legend (Green = Wi-Fi ready, Yellow = eBooks ready,
  Blue = Audiobooks ready), if status LEDs are wired up

The login notes shown under eBooks/Audiobooks are editable in the wizard
under "Welcome / Instructions Page" (they're just a display hint — this
installer doesn't provision per-patron accounts). QR codes are generated
offline with the `qrcode` package (as inline SVGs); if that package isn't
installed, the page still works, it just omits the QR images and shows the
plain URLs instead.

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

## Known limitations

- The setup wizard has no login and runs as root — only expose port 9000
  on a trusted network.
- The request page (port 5000) also has no login and its "mark
  fulfilled" toggle has no confirmation — fine on a private hotspot,
  not for an open network.
- `config.yml` (the wizard's own saved settings) stores the Wi-Fi and SMB
  passwords in plain text.
