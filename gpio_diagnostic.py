#!/usr/bin/env python3
"""
Lusty Library — standalone GPIO diagnostic.

Run this by hand over SSH when the setup wizard's LED test or shutdown
button test didn't work, but also logged no error. That combination
usually means gpiozero and the pin objects are working fine in software —
so the problem is either (a) gpiozero silently using a fake/no-op pin
factory instead of real hardware, or (b) the wiring itself (wrong pin,
reversed LED polarity, missing resistor, button wired to the wrong rail).
This script isolates those two possibilities from each other and from the
rest of the installer, one pin at a time, with you watching/pressing in
real time.

Usage (defaults match the wizard's default pins):

    sudo python3 gpio_diagnostic.py
    sudo python3 gpio_diagnostic.py --led-pins 17 27 22 --button-pin 26

Run as root (same as the installer/setup wizard) so it has the same GPIO
access the real setup would have.
"""
import argparse
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--led-pins", type=int, nargs="*", default=[17, 27, 22],
                         help="BCM pin numbers to test as LEDs (default: 17 27 22, the wizard's defaults)")
    parser.add_argument("--button-pin", type=int, default=26,
                         help="BCM pin number to test as the shutdown button (default: 26)")
    parser.add_argument("--skip-leds", action="store_true", help="Skip the LED tests")
    parser.add_argument("--skip-button", action="store_true", help="Skip the button test")
    args = parser.parse_args()

    try:
        import gpiozero
        from gpiozero import LED, Button
        from gpiozero.pins.mock import MockFactory
    except ImportError:
        print("gpiozero isn't installed. Install it first, e.g.:")
        print("  sudo apt-get install -y python3-gpiozero python3-rpi-lgpio")
        print("  # or: sudo pip3 install --break-system-packages gpiozero rpi-lgpio")
        sys.exit(1)

    try:
        gpiozero.Device.ensure_pin_factory()
    except Exception as e:  # noqa: BLE001 - this IS the failure we're diagnosing
        print(f"Could not set up any GPIO backend at all: {e}")
        print("This means gpiozero itself never gets far enough to touch a pin — the LED and")
        print("button tests in the wizard would fail the same way. On newer Raspberry Pi OS")
        print("releases (Bookworm/trixie) this is almost always a missing/broken lgpio backend:")
        print("  sudo apt-get install -y python3-rpi-lgpio")
        print("  # or: sudo pip3 install --break-system-packages rpi-lgpio")
        sys.exit(1)

    factory = gpiozero.Device.pin_factory
    factory_name = type(factory).__module__ + "." + type(factory).__name__
    print(f"gpiozero pin factory in use: {factory_name}")
    if isinstance(factory, MockFactory) or "mock" in factory_name.lower():
        print()
        print("!! This is the MOCK pin factory — it accepts on()/off()/is_pressed calls")
        print("!! and never raises an error, but it never touches real hardware at all.")
        print("!! That alone would explain 'no error, but nothing happened' for both")
        print("!! the LED test and the button test. Something set the GPIOZERO_PIN_FACTORY")
        print("!! environment variable to 'mock' (check /etc/environment, ~/.bashrc, and")
        print("!! any systemd service's Environment= lines) — remove it and re-run.")
        print()
        answer = input("Continue anyway with the mock factory? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit(1)

    if not args.skip_leds:
        print()
        print("=== LED test ===")
        for pin in args.led_pins:
            input(f"Press Enter to turn GPIO{pin} ON solid for 5 seconds (watch the LED now)...")
            try:
                with LED(pin) as led:
                    led.on()
                    print(f"  GPIO{pin} is now ON. Look at the board...")
                    time.sleep(5)
                    led.off()
                    print(f"  GPIO{pin} is now OFF.")
            except Exception as e:  # noqa: BLE001 - report and keep testing the rest
                print(f"  Could not drive GPIO{pin}: {e}")
            seen = input(f"  Did GPIO{pin}'s LED actually light up? [y/N] ").strip().lower()
            if seen != "y":
                print(f"  -> GPIO{pin}: pin toggled in software with no error, but no light seen.")
                print("     Check: LED polarity (long leg/anode toward the resistor+GPIO, short")
                print("     leg/cathode to GND), a working current-limiting resistor (~220-330ohm)")
                print("     in the circuit, and that the breadboard's ground rail is actually")
                print("     jumpered to a Pi GND pin.")

    if not args.skip_button:
        print()
        print("=== Button test ===")
        print(f"Watching GPIO{args.button_pin} for 15 seconds — press and release the button now.")
        print("(Raw pin state prints below; it should flip when you press/release.)")
        try:
            btn = Button(args.button_pin, pull_up=True, bounce_time=0.05)
            deadline = time.time() + 15
            last = None
            changes = 0
            while time.time() < deadline:
                state = btn.is_pressed
                if state != last:
                    print(f"  GPIO{args.button_pin} is_pressed = {state}")
                    changes += 1
                    last = state
                time.sleep(0.05)
            btn.close()
            if changes == 0:
                print(f"  -> GPIO{args.button_pin} never changed state at all in 15s.")
                print("     Check: the button is wired between this GPIO pin and a GND pin")
                print("     (not 3.3V — pull_up=True expects the pin to idle HIGH and drop LOW")
                print("     on a press), and that it's the correct BCM pin number (BCM numbering,")
                print("     not the physical header position).")
            else:
                print(f"  -> Saw {changes} state change(s) — wiring and pin number are correct.")
        except Exception as e:  # noqa: BLE001
            print(f"  Could not read GPIO{args.button_pin}: {e}")

    print()
    print("Done. Re-run the setup wizard once the wiring/pin numbers are confirmed.")


if __name__ == "__main__":
    main()
