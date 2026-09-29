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

## Uninstall

```bash
sudo bash /opt/lustylibrary-installer/uninstall.sh          # keep library data
sudo bash /opt/lustylibrary-installer/uninstall.sh --purge-data   # wipe it too
```
