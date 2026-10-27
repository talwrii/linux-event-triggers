"""linux-event-triggers: run shell commands on Linux input device events.

Opens /dev/input/event*, grabs it exclusively (so X stops seeing the keys),
reads input_event structs, runs the configured shell command on each
matching key press. Pure stdlib.

Usage:
    evtrig /dev/input/by-path/...-event-kbd \\
        --bind "volumeup=pactl set-sink-volume @DEFAULT_SINK@ +5%" \\
        --bind "volumedown=pactl set-sink-volume @DEFAULT_SINK@ -5%"
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path


# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# On 64-bit Linux: 8 + 8 + 2 + 2 + 4 = 24 bytes.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY = 0x01
KEY_PRESS = 1


def _IOC(direction: int, ioc_type: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ioc_type << 8) | nr


# EVIOCGRAB = _IOW('E', 0x90, int)  →  0x40044590 on x86/arm.
EVIOCGRAB = _IOC(1, ord("E"), 0x90, 4)


# Subset of linux/input-event-codes.h. Keys are the symbolic name without the
# KEY_ prefix, uppercase. Extend as needed.
NAME_TO_CODE = {
    "ESC": 1,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20,
    "Y": 21, "U": 22, "I": 23, "O": 24, "P": 25,
    "LEFTBRACE": 26, "RIGHTBRACE": 27, "ENTER": 28, "LEFTCTRL": 29,
    "A": 30, "S": 31, "D": 32, "F": 33, "G": 34,
    "H": 35, "J": 36, "K": 37, "L": 38,
    "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41,
    "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53,
    "RIGHTSHIFT": 54, "KPASTERISK": 55,
    "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63,
    "F6": 64, "F7": 65, "F8": 66, "F9": 67, "F10": 68,
    "NUMLOCK": 69, "SCROLLLOCK": 70,
    "KP7": 71, "KP8": 72, "KP9": 73, "KPMINUS": 74,
    "KP4": 75, "KP5": 76, "KP6": 77, "KPPLUS": 78,
    "KP1": 79, "KP2": 80, "KP3": 81, "KP0": 82, "KPDOT": 83,
    "F11": 87, "F12": 88,
    "KPENTER": 96, "RIGHTCTRL": 97, "KPSLASH": 98, "SYSRQ": 99,
    "RIGHTALT": 100,
    "HOME": 102, "UP": 103, "PAGEUP": 104,
    "LEFT": 105, "RIGHT": 106,
    "END": 107, "DOWN": 108, "PAGEDOWN": 109,
    "INSERT": 110, "DELETE": 111,
    "MUTE": 113, "VOLUMEDOWN": 114, "VOLUMEUP": 115,
    "POWER": 116, "PAUSE": 119,
    "LEFTMETA": 125, "RIGHTMETA": 126, "COMPOSE": 127,
    "STOP": 128, "AGAIN": 129, "UNDO": 131,
    "COPY": 133, "PASTE": 135, "FIND": 136, "CUT": 137,
    "HELP": 138, "MENU": 139, "CALC": 140,
    "SLEEP": 142, "WAKEUP": 143,
    "WWW": 150, "SCREENLOCK": 152,
    "BACK": 158, "FORWARD": 159,
    "NEXTSONG": 163, "PLAYPAUSE": 164, "PREVIOUSSONG": 165, "STOPCD": 166,
    "RECORD": 167, "REWIND": 168,
    "F13": 183, "F14": 184, "F15": 185, "F16": 186, "F17": 187, "F18": 188,
    "F19": 189, "F20": 190, "F21": 191, "F22": 192, "F23": 193, "F24": 194,
    "PRINT": 210, "EMAIL": 215, "SEARCH": 217,
    "BRIGHTNESSDOWN": 224, "BRIGHTNESSUP": 225,
    "KBDILLUMTOGGLE": 228, "KBDILLUMDOWN": 229, "KBDILLUMUP": 230,
    "MICMUTE": 248,
}


def normalize_key_name(name: str) -> str:
    """Accept 'a', 'A', 'KEY_A', 'key_a' etc. Return canonical 'A'."""
    name = name.strip().upper()
    if name.startswith("KEY_"):
        name = name[4:]
    return name


def parse_bind(arg: str) -> tuple[int, str]:
    """Parse 'KEY=COMMAND' into (keycode, command_string)."""
    if "=" not in arg:
        raise ValueError(f"--bind {arg!r} missing '='; expected KEY=COMMAND")
    key_part, command = arg.split("=", 1)
    name = normalize_key_name(key_part)
    if name not in NAME_TO_CODE:
        raise ValueError(
            f"unknown key {key_part!r}. Use the symbolic name from "
            f"linux/input-event-codes.h, e.g. A, VOLUMEUP, F13, LEFTCTRL"
        )
    if not command.strip():
        raise ValueError(f"--bind {arg!r} has empty command")
    return NAME_TO_CODE[name], command


def _log(msg: str) -> None:
    """Timestamped stderr log line."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def _stable_chown_path(device: str) -> str:
    """A /dev/input/by-path/...-event-kbd symlink that currently resolves to
    `device`, if one exists. `chown` follows symlinks by default, so this
    changes nothing about which file actually gets chowned -- but it means
    the *command string* run is the same across replugs even though the
    raw /dev/input/eventN target isn't, which is what a device needs for a
    sudoers NOPASSWD entry to match it without listing every possible
    eventN number (or every USB port, in --usb-port mode). Falls back to
    `device` itself if no matching by-path symlink is found."""
    by_path = Path("/dev/input/by-path")
    if not by_path.is_dir():
        return device
    try:
        target = Path(device).resolve()
    except OSError:
        return device
    for entry in sorted(by_path.glob("*-event-kbd")):
        try:
            if entry.resolve() == target:
                return str(entry)
        except OSError:
            continue
    return device


def _chown_if_needed(device: str) -> bool:
    """Run sudo chown on the device (or the stable by-path symlink to it,
    see _stable_chown_path). Returns True on success."""
    device = _stable_chown_path(device)
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        _log("error: --chown needs $USER or $LOGNAME set in environment")
        return False
    try:
        subprocess.run(["sudo", "chown", user, device], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _log(f"error: sudo chown failed: {e}")
        return False


def _open_and_grab(device: str) -> int | None:
    """Open + EVIOCGRAB the device. Returns fd or None on failure (with logged
    reason). Does not log for ENOENT (caller decides how to handle missing)."""
    try:
        fd = os.open(device, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as e:
        _log(f"error: cannot open {device}: {e}")
        return None
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
    except OSError as e:
        os.close(fd)
        if e.errno == errno.EBUSY:
            _log(
                f"error: {device} is already grabbed by another process. "
                f"Find who: sudo lsof {device}"
            )
        else:
            _log(f"error: grab failed: {e}")
        return None
    return fd


def _wait_for_device(device: str, poll_seconds: float = 0.5) -> None:
    """Block until os.path.exists(device) is True. Logs at start."""
    _log(f"waiting for {device} to come back...")
    while not os.path.exists(device):
        time.sleep(poll_seconds)
    _log(f"{device} reappeared")


def find_event_device_for_usb_port(port: str) -> str | None:
    """Resolve a USB port path (e.g. '1-10.1', as shown by `lsusb -t` or the
    tail of a device's sysfs path) to whichever /dev/input/eventN currently
    sits on it, or None if nothing does right now.

    This exists for hardware with no usable serial number — two otherwise
    identical devices (same vendor, product, even 'serial' string on some
    cheap hardware) are only distinguishable by which physical port they're
    in. The port path itself is what's stable, not any eventN number, which
    can and does change across replugs — so this is re-resolved on every
    (re)connect rather than cached.

    Matches on an exact path *segment* (surrounded by '/' or string edges)
    so e.g. '1-10.1' doesn't also match '1-10.10'.

    Only nodes udev tags as keyboards count: one USB keyboard often exposes
    extra 'Consumer Control' / 'System Control' nodes on the same port, and
    during an unplug those can briefly outlive the keyboard node itself.
    """
    needle = f"/{port}/"
    for entry in sorted(Path("/sys/class/input").glob("event*")):
        try:
            real = entry.resolve()
        except OSError:
            continue
        if needle in f"/{real}/" and _is_udev_keyboard(entry):
            return f"/dev/input/{entry.name}"
    return None


def _is_udev_keyboard(entry: Path) -> bool:
    """True if udev has tagged this /sys/class/input/eventN as
    ID_INPUT_KEYBOARD (the same flag behind by-path '-event-kbd' links)."""
    try:
        dev = (entry / "dev").read_text().strip()
        data = Path(f"/run/udev/data/c{dev}").read_text()
    except OSError:
        return False
    return "E:ID_INPUT_KEYBOARD=1" in data.splitlines()


def _read_event_loop(fd: int, bindings: dict[int, str], dump: bool) -> str | None:
    """Read events until the device goes away or we hit EOF.

    Returns:
        None on clean EOF (rare),
        "disconnect" if the device disappeared (caller may want to reconnect),
        propagates KeyboardInterrupt.
    """
    while True:
        try:
            data = os.read(fd, EVENT_SIZE)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EIO):
                return "disconnect"
            raise
        if not data:
            return None
        if len(data) != EVENT_SIZE:
            continue
        _, _, ev_type, code, value = struct.unpack(EVENT_FORMAT, data)
        if ev_type != EV_KEY:
            continue
        if dump:
            action = {1: "press", 0: "release", 2: "repeat"}.get(value, str(value))
            name = next((n for n, c in NAME_TO_CODE.items() if c == code), f"code{code}")
            print(f"{name} {action}", flush=True)
        if value != KEY_PRESS:
            continue
        cmd = bindings.get(code)
        name = next((n for n, c in NAME_TO_CODE.items() if c == code), f"code{code}")
        _log(f"{name} pressed" + (f" -> {cmd}" if cmd else " (unbound)"))
        if cmd is None:
            continue
        try:
            subprocess.Popen(cmd, shell=True)
        except OSError as e:
            _log(f"error running {cmd!r}: {e}")


def cmd_run(args: argparse.Namespace) -> int:
    bindings: dict[int, str] = {}
    for raw in args.bind or []:
        try:
            code, cmd = parse_bind(raw)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        bindings[code] = cmd

    if not bindings and not args.dump:
        print("error: no --bind given and --dump not set; nothing to do",
              file=sys.stderr)
        return 2

    def resolve() -> str | None:
        """Current device path, or None if nothing's there right now.

        In --usb-port mode this is re-resolved every call rather than
        cached: the eventN a port maps to can (and does) change across
        replugs, even though the port path itself stays fixed.
        """
        if args.usb_port:
            return find_event_device_for_usb_port(args.usb_port)
        return args.device if os.path.exists(args.device) else None

    def wait_for_device() -> str:
        """Block until resolve() finds something, then return it."""
        label = f"port {args.usb_port}" if args.usb_port else args.device
        _log(f"waiting for {label} to come back...")
        while True:
            device = resolve()
            if device is not None:
                _log(f"{label} reappeared as {device}")
                return device
            time.sleep(0.5)

    # Initial existence check — fail loudly if the device isn't there at start.
    device = resolve()
    if device is None:
        label = f"port {args.usb_port}" if args.usb_port else args.device
        _log(f"error: {label} does not exist")
        return 1

    if args.chown and not _chown_if_needed(device):
        return 1

    # Don't accumulate zombies from fire-and-forget Popen calls.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    first_attempt = True
    try:
        while True:
            fd = _open_and_grab(device)
            if fd is None:
                if first_attempt:
                    # Device existed at startup but we can't open/grab — bail.
                    return 1
                # Was reconnected but open/grab failed transiently; wait and retry.
                _log("open/grab failed after reconnect, retrying in 1s")
                time.sleep(1.0)
                continue
            first_attempt = False
            _log(f"grabbed {device}")
            try:
                outcome = _read_event_loop(fd, bindings, args.dump)
            finally:
                try:
                    fcntl.ioctl(fd, EVIOCGRAB, 0)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass

            if outcome == "disconnect":
                if args.no_wait:
                    _log(f"{device} disconnected; exiting (--no-wait)")
                    return 1
                _log(f"{device} disconnected")
                device = wait_for_device()
                if args.chown:
                    _chown_if_needed(device)
                # Loop back round to open+grab.
                continue
            # Clean EOF — unusual but treat as exit.
            return 0
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="evtrig",
        description=(
            "Grab a Linux input device and run shell commands on key presses. "
            "Other processes (X, the focused application) stop seeing events from "
            "this device while evtrig is running."
        ),
    )
    parser.add_argument(
        "device",
        nargs="?",
        help="Path to event device, e.g. /dev/input/event5 or "
             "/dev/input/by-path/...-event-kbd. Omit if using --usb-port "
             "instead.",
    )
    parser.add_argument(
        "--usb-port",
        metavar="PORT",
        help="Identify the device by USB port path (e.g. '1-10.1', the tail "
             "of its sysfs path -- see `udevadm info -q path -n DEVICE`) "
             "instead of a fixed device path. For hardware with no usable "
             "serial number, where two otherwise-identical devices are only "
             "distinguishable by which port they're plugged into. Re-resolved "
             "on every (re)connect, since eventN numbers aren't stable across "
             "replugs even though the port path is. Mutually exclusive with "
             "the device argument.",
    )
    parser.add_argument(
        "--bind",
        action="append",
        metavar="KEY=COMMAND",
        help="Run COMMAND (shell-interpreted) when KEY is pressed. "
             "Repeatable. KEY is the symbolic name like A, VOLUMEUP, F13, "
             "LEFTCTRL (the KEY_ prefix is optional).",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Also print every key press/release to stdout. "
             "Useful for finding key names.",
    )
    parser.add_argument(
        "--chown",
        action="store_true",
        help="Run `sudo chown $USER DEVICE` before opening, so evtrig itself "
             "doesn't need to run as root. Pair with a NOPASSWD sudoers entry "
             "to make this passwordless. Ownership reverts when the device is "
             "unplugged or the system reboots. Re-applied automatically after "
             "reconnect.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Exit when the device is disconnected mid-run, instead of "
             "waiting for it to come back. Default behaviour is to log the "
             "disconnect, poll for the device to reappear, then re-grab.",
    )
    args = parser.parse_args()
    if bool(args.device) == bool(args.usb_port):
        parser.error("give exactly one of DEVICE or --usb-port")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())