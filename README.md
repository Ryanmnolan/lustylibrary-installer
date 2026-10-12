# Lusty Library Installer

Turn a Raspberry Pi into an **offline library**: its own Wi-Fi hotspot that
patrons join to read ebooks, listen to audiobooks and ask for new titles, with
no internet needed.

Supports **Raspberry Pi 3, 4 and 5**, on both the older dhcpcd/hostapd network
stack and the newer NetworkManager stack used by current Raspberry Pi OS.

## Original idea

My wife fell in love with reading all over again after our second child was born. Through our oldest child's school, she made some wonderful friends, and together they started a monthly book club.

A little over two years ago, I built what we now call The Lusty Library. It started as a fun little project using a Raspberry Pi 4, an old 500GB SSD, and a whole lot of spaghetti code I wrote myself.

What started as a small project quickly became a huge hit at book club, and now we want to share it with all of you!

I originally wrote all the code myself, but recently used Claude AI to help clean things up and turn it into something much more polished and user-friendly.

The best part? The whole thing is built around a small case from Harbor Freight, a 3D-printed insert and leftover computer parts I had lying around.

It's been a fun project to build, improve, and watch everyone enjoy. I'm excited to finally share The Lusty Library with you!

## What you get

- A **Wi-Fi hotspot** (name, password and IP are yours to choose)
- [Audiobookshelf](https://www.audiobookshelf.org/) for audiobooks and
  [Calibre-Web](https://github.com/janeczku/calibre-web) for ebooks, running
  in Docker, with a shared **patron login** created for you
- A **request page** where patrons ask for a book or audiobook (saved to a CSV
  you can read, with a one-tap "mark fulfilled")
- A **printable welcome sheet** (landscape, one page, also as a PDF) with the
  Wi-Fi details, logins and QR codes
- Optional **auto-sync**: plug in an Ethernet cable and new books and
  audiobooks copy over from another computer or NAS on your network
- Optional **status LEDs** and a **press-and-hold shutdown button** on the
  Pi's GPIO pins

---

## Before you start

- A Raspberry Pi 3, 4 or 5 running Raspberry Pi OS, **connected to your home
  network** (Wi-Fi or Ethernet) with internet access. The install downloads
  Docker and the apps, so it needs the internet.
- A phone or computer on the **same network**, to run the setup wizard.
- Optional: a USB drive for the books, status LEDs and a shutdown button.

## Step 1 - Install

SSH into the Pi (or open a terminal on it) and run:

```bash
curl -sSL https://raw.githubusercontent.com/ryanmnolan/lustylibrary-installer/main/install.sh | sudo bash
```

This installs git, Python and Docker, copies the installer to
`/opt/lustylibrary-installer`, and starts the setup wizard as a service.
The log is at `/var/log/lustylibrary-install.log`.

When it finishes, open the wizard from your phone or computer:

```
http://<pi-ip>:9000/setup
```

`<pi-ip>` is the Pi's address on your home network, for example
`192.168.0.86`. If you don't know it, run `hostname -I` on the Pi.

> **Important:** keep the Pi on your home network until the wizard says it's
> done. The hotspot only starts after the final reboot, so the setup page and
> the internet keep working during the install.

---

## Step 2 - The setup wizard

The wizard has 7 short screens. Use **Next** and **Back** to move around; each
screen checks your answers before letting you continue.

### Screen 1 - Wi-Fi hotspot

![Wi-Fi hotspot screen](docs/images/01-wifi.jpg)

This is the Wi-Fi network patrons will join.

1. **Network name (SSID)** - what patrons see in their Wi-Fi list.
2. **Password** - must be **8 to 63 characters** (a WPA2 rule).
3. **Hotspot IP address** - leave the default (`10.10.10.10`) unless you have
   a reason to change it. This is the address patrons use to reach the library.
4. Tap **Next**.

### Screen 2 - Storage

![Storage screen](docs/images/02-storage.jpg)

Where the books and audiobooks are kept.

1. **Media folder** - the default `/mnt/media` is fine. The installer creates
   `audiobooks`, `books` and `config` folders inside it.
2. **USB drive** - leave on *Keep existing storage as-is* to use the SD card.
   To use a USB drive, plug it in and choose it from the list.
3. Only tick **Format the selected drive as ext4 and mount it at the media
   folder** if you want the drive **erased**. **This deletes everything on it.**
   If the drive already holds your library, leave it unticked.
4. Tap **Next**.

### Screen 3 - Apps

![Apps screen](docs/images/03-apps.jpg)

1. Tick **Audiobookshelf** (audiobooks), **Calibre-Web** (ebooks), or both.
   Docker is installed automatically if it isn't already.
2. Choose the **patron username and password**. This one shared login works in
   both apps and is printed on the welcome sheet. The default is `book` /
   `book`.
3. Tap **Next**.

You don't need to log in to either app first; setup creates the account for
you and checks that it really works.

### Screen 4 - Request page

![Request page screen](docs/images/04-request-page.jpg)

A simple page anyone on the hotspot can use to ask for a book or audiobook.

1. Leave **Enable the request page** ticked to turn it on.
2. **Port** - the default `5000` is fine.
3. Tap **Next**.

Patrons see this (open `http://10.10.10.10:5000` while on the hotspot):

![What patrons see on the request page](docs/images/patron-request-page.jpg)

Requests are saved to `requests.csv` in your media folder.

### Screen 5 - Auto-sync (optional)

![Auto-sync screen](docs/images/05-auto-sync.jpg)

Copies audiobooks and books from another computer or NAS on your network, by
itself, the moment an Ethernet cable is plugged into the Pi. **Skip this
screen** (leave the box unticked) if you don't need it.

1. Tick **Enable auto-sync from a server**.
2. **Server IP** - the address of the computer or NAS that holds your books,
   for example `192.168.0.139`.
3. **Username / Password** - leave both blank if the server allows guest
   access.
4. Under **Audiobooks folder on the server**, tap **Browse**.

   ![Folder browser](docs/images/05-folder-picker.jpg)

   - The Pi lists the server's shared folders. Tap one to open it, then tap
     into the folders inside it.
   - The path across the top (`192.168.0.139 › data › media › audiobook`)
     shows where you are. Tap any part of it to jump back.
   - When you're in the right folder, tap **Use this folder**.
   - If the server doesn't list its shares, you can type a share name by hand.
5. Do the same for **Books (Calibre) folder on the server**.
6. Tap **Next**.

Notes:

- The first time you tap **Browse**, the Pi installs a small tool (`smbclient`),
  so it needs internet at that moment. It may say "getting ready" for a minute.
- Nothing is copied until an Ethernet cable is plugged in.
- The **contents** of the folder you choose are copied into `audiobooks` and
  `books` on the Pi. Existing files are not overwritten.
- For ebooks, choose a real **Calibre library** (a folder that contains a
  `metadata.db` file). After the books are copied, the sync also updates the
  Pi's `metadata.db` and restarts Calibre-Web, so the new books show up on
  their own. Audiobookshelf finds new audiobooks by itself.

### Screen 6 - Hardware (optional)

Only tick these if you have actually wired them to the Pi's GPIO pins. Each one
is tested for real during setup. With nothing ticked, the screen is just two
unticked boxes:

![Hardware screen with nothing ticked](docs/images/06-hardware-none.jpg)

**Status LEDs.** Three LEDs show when each part is ready: green for Wi-Fi,
blue for Audiobookshelf, yellow for Calibre-Web.

![Status LEDs ticked](docs/images/06-hardware-leds.jpg)

1. Tick **Status LEDs**.
2. Enter the **BCM (GPIO) pin number** for each LED. The defaults are
   Wi-Fi `17`, Calibre-Web `27` and Audiobookshelf `22`. These are GPIO
   numbers, not the physical pin numbers on the header.
3. During setup each LED is switched on so you can confirm it really lights up.

**Shutdown button.** Press and hold to shut the Pi down safely.

![Shutdown button ticked](docs/images/06-hardware-shutdown.jpg)

1. Tick **Shutdown button (press and hold)**.
2. **Button pin** - default GPIO `26`. Wire a push button between that pin and
   a ground (GND) pin.
3. **Hold time** - how many seconds it must be held (default `2.0`).
4. During setup you'll be asked to press and hold the button for real before
   the always-on watcher is installed.

Tap **Next**.

### Screen 7 - Review & install

![Review screen](docs/images/07-review.jpg)

1. Check every section. Tap **Edit** on any of them to go back and change it.
2. Tap **Apply & Set Up** to start.

---

## Step 3 - Installing

![Install progress](docs/images/install-progress.jpg)

The page switches to a live progress list with a console below it. It takes a
while (Docker images are downloaded), so leave it open. You can ignore the red
`unknown connection 'LustyLibraryAP'` line near the top; on a first install
there is no old hotspot to delete.

If something fails, the step turns red and the console says why. Tap **Change
settings**, fix it and run it again.

## Step 4 - Reboot

![Setup finished](docs/images/install-finished.jpg)

When every step shows a green tick, setup is finished. Tap **Reboot now**
(or run `sudo reboot`). After the restart:

1. The Pi starts the hotspot you chose.
2. On your phone, join that Wi-Fi network (default `LustyLibrary`, password
   `lustybooks123`, unless you changed them).
3. Open the welcome page: `http://10.10.10.10:9000/welcome`.

The setup page stops working at reboot, which is expected.

## Using the library

Once you're on the hotspot (default addresses):

| What | Address | Login |
| --- | --- | --- |
| Ebooks (Calibre-Web) | `http://10.10.10.10:8083` | patron login (`book` / `book` by default) |
| Audiobooks (Audiobookshelf) | `http://10.10.10.10:13378` | patron login |
| Request a title | `http://10.10.10.10:5000` | none |
| Welcome sheet | `http://10.10.10.10:9000/welcome` | none |

For audiobooks, patrons install the free **Audiobookshelf** app on their phone
*before* connecting to the hotspot (the hotspot has no internet), then enter
`http://10.10.10.10:13378` as the server address. The welcome sheet has QR codes
for the iOS and Android apps.

### The welcome sheet

The welcome page is a one-page landscape sheet made to be printed and left next
to the library. Open `/welcome` and tap **Print**, or download the PDF at
`/welcome.pdf`. It also lives in your media folder as `welcome.html` and
`welcome.pdf`. Sections for anything you didn't install are left out.

Here is an example of what it looks like (a real one, made by the installer):

![Example welcome sheet](docs/images/welcome-sheet.png)

[Download the example PDF](docs/lusty-library-welcome-example.pdf)

---

## Case & 3D print

A 3D-printable insert for the LEDs and shutdown button is available on
Printables:

> **Printables link: coming soon.**

Once it's up, this section will also have:

- Photos of the finished case
- Recommended print settings (material, supports, infill)
- A short build guide for fitting the Pi, LEDs and button

The case is from Harbor Freight:

- APACHE 1800 Weatherproof Protective Case, Small, Black
  ([Harbor Freight product page](https://www.harborfreight.com/1800-weatherproof-protective-case-small-black-64550.html))

## Wiring (optional hardware)

- **LEDs:** connect each LED's long leg to its GPIO pin through a resistor
  (about 330 ohm), and its short leg to ground.
- **Shutdown button:** one side to the button GPIO pin, the other side to
  ground. No resistor is needed.
- To check your wiring outside the wizard, run
  `sudo python3 /opt/lustylibrary-installer/gpio_diagnostic.py`.

## Uninstall

```bash
sudo bash /opt/lustylibrary-installer/uninstall.sh                # keep library data
sudo bash /opt/lustylibrary-installer/uninstall.sh --purge-data   # wipe it too
sudo bash /opt/lustylibrary-installer/uninstall.sh --full         # remove everything, incl. Docker
```

## Updating

```bash
cd /opt/lustylibrary-installer
sudo git pull
sudo systemctl restart lustylibrary-setup
```

## Troubleshooting

| Problem | What to do |
| --- | --- |
| The setup page won't load | Check the service: `sudo systemctl status lustylibrary-setup`. Make sure your phone is on the same network as the Pi and you used port `9000`. |
| "Password must be 8-63 characters" | Pick a longer Wi-Fi password (WPA2 needs at least 8). |
| I can't see the hotspot after setup | Reboot the Pi. The hotspot starts on the next boot. |
| The page froze during the install | Leave it. Reload `http://<pi-ip>:9000/setup`; it reconnects to the running install. |
| Patron login doesn't work | Setup checks the login at the end. If the step shows an error, create the user in Calibre-Web as `admin` / `admin123` (Admin, then Add new user). |
| Folder browser says it can't reach the server | Check the server IP, that the server is on the same network, and the username and password. |
| A step failed | Read the console on the progress page and `/var/log/lustylibrary-install.log`, fix the cause, then use **Change settings** and run it again. |

## Files

| File | Purpose |
| --- | --- |
| `install.sh` | One-line installer |
| `setup_gui.py` | The setup wizard (Flask app on port 9000) |
| `uninstall.sh` | Removes what the installer added |
| `gpio_diagnostic.py` | Tests LED and button wiring |
| `lustylibrary-setup.service` | systemd unit for the wizard |
