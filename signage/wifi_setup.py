"""Boot-time Wi-Fi setup wizard, driven with just a keyboard on the TV.

Runs on the console (tty1) before the player starts. If the Pi joins a known
network within a short wait, it exits quietly. Otherwise it shows a full-screen
list of Wi-Fi networks: pick one with the arrow keys, type the password, and it
connects with NetworkManager (nmcli), shows the admin address, and exits so the
signage can start. Esc leaves without changing anything. If nobody presses a key for 30 seconds, it gives up and starts
the signage anyway (the Pi keeps trying saved networks in the background).

    python -m signage.wifi_setup            # normal boot behaviour
    python -m signage.wifi_setup --force    # show the wizard even if online
    python -m signage.wifi_setup --demo     # fake networks, for trying the UI
"""
from __future__ import annotations

import argparse
import curses
import locale
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

WAIT_FOR_KNOWN_SECONDS = 45
# At boot, if nobody touches the keyboard for this long, give up and start the
# signage: the videos are stored on the Pi, and NetworkManager keeps trying the
# saved networks in the background.
IDLE_SECONDS = 30
SUCCESS_SECONDS = 15
CONSOLE_FONT = "Uni2-TerminusBold32x16"

# Linux console palette: slot -> brand colour (Project X Creative)
PALETTE = {
    0: "08003E",  # black   -> navy (background)
    1: "C50089",  # red     -> magenta
    2: "01C518",  # green   -> green
    3: "F7B800",  # yellow  -> amber
    4: "170F55",  # blue    -> lighter navy (highlights)
    5: "FF5CC0",  # magenta -> pink on navy
    6: "0080E1",  # cyan    -> blue
    7: "FFFFFF",  # white   -> white
}


# --------------------------------------------------------------------------
# NetworkManager


@dataclass
class Network:
    ssid: str
    signal: int
    secure: bool
    in_use: bool = False


def split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on ':' while honouring '\\:' and '\\\\' escapes."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_wifi_list(output: str) -> list[Network]:
    """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`.

    Hidden networks (empty SSID) are dropped and each SSID is kept once, at its
    strongest signal. Sorted strongest first.
    """
    best: dict[str, Network] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = split_terse(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if not ssid:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        sec = security.strip() not in ("", "--")
        net = Network(ssid, strength, sec, in_use.strip() == "*")
        old = best.get(ssid)
        if old is None or strength > old.signal:
            net.in_use = net.in_use or (old.in_use if old else False)
            best[ssid] = net
        elif net.in_use:
            old.in_use = True
    return sorted(best.values(), key=lambda n: (-n.signal, n.ssid.lower()))


def parse_online(output: str) -> bool:
    """True if any Wi-Fi or Ethernet device is connected (from `nmcli -t -f TYPE,STATE device`)."""
    for line in output.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2 and parts[0] in ("wifi", "ethernet") and parts[1] == "connected":
            return True
    return False


class NM:
    """Thin wrapper around nmcli."""

    def _run(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)

    def has_wifi(self) -> bool:
        try:
            out = self._run("-t", "-f", "TYPE", "device").stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "wifi" in out.split()

    def online(self) -> bool:
        try:
            return parse_online(self._run("-t", "-f", "TYPE,STATE", "device").stdout)
        except (OSError, subprocess.TimeoutExpired):
            return False

    def scan(self) -> list[Network]:
        try:
            self._run("radio", "wifi", "on", timeout=10)
            out = self._run("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                            "--rescan", "yes", timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return parse_wifi_list(out)

    def connect(self, ssid: str, password: str, hidden: bool = False) -> tuple[bool, str]:
        """Join a network, keeping saved profiles tidy.

        If it fails, any profile the attempt created is removed and the Wi-Fi
        network that was active before is brought back up.
        """
        before = self.wifi_profiles()
        previous = next((p for p in before if p["active"]), None)
        args = ["--wait", "40", "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        if hidden:
            args += ["hidden", "yes"]
        try:
            r = self._run(*args, timeout=60)
            ok = r.returncode == 0
            msg = (r.stderr or r.stdout).strip().splitlines()
            err = "" if ok else friendly_error(msg[-1] if msg else "Could not connect.")
        except subprocess.TimeoutExpired:
            ok, err = False, "Timed out while connecting."
        except OSError as e:
            return False, str(e)

        old_uuids = {p["uuid"] for p in before}
        after = self.wifi_profiles()
        if ok:
            # Drop older duplicates for the same network, keep the one now in use.
            current = next((p for p in after if p["active"]), None)
            for p in after:
                if current and p["ssid"] == ssid and p["uuid"] != current["uuid"]:
                    self.forget(p["uuid"])
            return True, ""
        for p in after:
            if p["uuid"] not in old_uuids:
                self.forget(p["uuid"])
        if previous and not self.online():
            try:
                self._run("--wait", "40", "connection", "up", "uuid", previous["uuid"], timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return False, err

    def wifi_profiles(self) -> list[dict]:
        """Saved Wi-Fi networks: [{name, uuid, ssid, active}]."""
        try:
            out = self._run("-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show", timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        profiles = []
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 4 or parts[2] != "802-11-wireless":
                continue
            name, uuid, active = parts[0], parts[1], parts[3] == "yes"
            try:
                ssid = self._run("-g", "802-11-wireless.ssid", "connection", "show", "uuid", uuid,
                                 timeout=10).stdout.strip() or name
            except (OSError, subprocess.TimeoutExpired):
                ssid = name
            profiles.append({"name": name, "uuid": uuid, "ssid": ssid, "active": active})
        return profiles

    def forget(self, uuid: str) -> bool:
        try:
            return self._run("connection", "delete", "uuid", uuid, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def status(self) -> dict:
        """Current Wi-Fi network, signal and address on the Wi-Fi adapter."""
        info = {"ssid": None, "signal": None, "ip": None, "wired": False}
        try:
            devs = self._run("-t", "-f", "DEVICE,TYPE,STATE", "device", timeout=10).stdout
            for line in devs.splitlines():
                dev, typ, state = (split_terse(line) + ["", "", ""])[:3]
                if typ == "ethernet" and state == "connected":
                    info["wired"] = True
                if typ == "wifi" and state == "connected":
                    addr = self._run("-g", "IP4.ADDRESS", "device", "show", dev, timeout=10).stdout
                    info["ip"] = addr.split("|")[0].split("/")[0].strip() or None
            nets = parse_wifi_list(self._run("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi",
                                             "list", "--rescan", "no", timeout=15).stdout)
            here = next((n for n in nets if n.in_use), None)
            if here:
                info["ssid"], info["signal"] = here.ssid, here.signal
        except (OSError, subprocess.TimeoutExpired):
            pass
        return info

    def ip_address(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))  # no packets sent; picks the outgoing interface
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return ""


def friendly_error(msg: str) -> str:
    low = msg.lower()
    if "secrets were required" in low or "802-1x" in low or "psk" in low or "password" in low:
        return "The password was not accepted. Check it and try again."
    if "no network with ssid" in low:
        return "That network could not be found. Check the name, or move closer."
    if "timeout" in low or "timed out" in low:
        return "Timed out while connecting."
    return msg.removeprefix("Error: ")


class DemoNM(NM):
    """Fake networks so the UI can be tried anywhere."""

    def __init__(self, delay: float = 1.0):
        self._online = False
        self.delay = delay
        self.current = None
        self.saved = [{"name": "Home Wi-Fi", "uuid": "demo-home", "ssid": "Home Wi-Fi", "active": False}]

    def has_wifi(self):
        return True

    def online(self):
        return self._online

    def scan(self):
        time.sleep(self.delay * 0.6)
        return [Network("The Store Guest", 82, True, self.current == "The Store Guest"),
                Network("The Store Staff", 74, True, self.current == "The Store Staff"),
                Network("BT-Hub-7F2A", 51, True), Network("Cafe Free WiFi", 33, False)]

    def connect(self, ssid, password, hidden=False):
        time.sleep(self.delay * 1.5)
        if password == "wrong":
            return False, friendly_error("Error: Secrets were required, but not provided.")
        self._online = True
        self.current = ssid
        for p in self.saved:
            p["active"] = p["ssid"] == ssid
        if not any(p["ssid"] == ssid for p in self.saved):
            self.saved.append({"name": ssid, "uuid": f"demo-{len(self.saved)}", "ssid": ssid, "active": True})
        return True, ""

    def wifi_profiles(self):
        return [dict(p) for p in self.saved]

    def forget(self, uuid):
        n = len(self.saved)
        self.saved = [p for p in self.saved if p["uuid"] != uuid]
        return len(self.saved) < n

    def status(self):
        return {"ssid": self.current, "signal": 82 if self.current else None,
                "ip": "192.168.0.82" if self.current else None, "wired": False}

    def ip_address(self):
        return "192.168.0.82"


# --------------------------------------------------------------------------
# Screen


def set_palette(enable: bool) -> None:
    """Swap the Linux console colours to the brand palette (no-op elsewhere)."""
    if os.environ.get("TERM") != "linux":
        return
    if enable:
        seq = "".join(f"\033]P{slot:X}{rgb}" for slot, rgb in PALETTE.items())
    else:
        seq = "\033]R"
    sys.stdout.write(seq)
    sys.stdout.flush()


def set_console_font() -> None:
    if os.environ.get("TERM") != "linux":
        return
    try:
        subprocess.run(["setfont", CONSOLE_FONT], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def bars(signal: int) -> str:
    n = 4 if signal >= 75 else 3 if signal >= 50 else 2 if signal >= 25 else 1
    return "█" * n + "·" * (4 - n)


class IdleTimeout(Exception):
    """Nobody pressed a key for IDLE_SECONDS: start the signage."""


class Quit(Exception):
    """Esc pressed: leave the wizard and start the signage."""


ESC = 27


class UI:
    OTHER = "Other network (type its name)..."
    RESCAN = "Scan again"
    SKIP = "Skip: start the signage without Wi-Fi"

    def __init__(self, stdscr, nm: NM, hostname: str, wait: int = WAIT_FOR_KNOWN_SECONDS,
                 idle: int = IDLE_SECONDS):
        self.wait = wait
        self.idle = idle
        self.idle_until = None
        self.force = False
        self.s = stdscr
        self.nm = nm
        self.hostname = hostname
        curses.curs_set(0)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)    # body
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_RED)      # selected (magenta)
        curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)     # muted/blue
        curses.init_pair(4, curses.COLOR_MAGENTA, curses.COLOR_BLACK)  # pink accent
        curses.init_pair(5, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(6, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)     # input box
        self.s.bkgd(" ", curses.color_pair(1))
        self.s.keypad(True)

    # drawing helpers ------------------------------------------------------
    def frame(self, title: str) -> int:
        """Clear and draw the header. Returns the first free row."""
        self.s.erase()
        h, w = self.s.getmaxyx()
        x = self.left()
        self.put(2, x, "PROJECT X CREATIVE", curses.A_BOLD)
        dots = [(5, "■"), (3, "■"), (1, "■"), (6, "■")]
        dx = x + len("PROJECT X CREATIVE") + 2
        for pair, ch in dots:
            self.put(2, dx, ch, curses.color_pair(pair if pair != 1 else 4))
            dx += 2
        self.put(2, dx + 1, "TV Signage", curses.color_pair(3))
        self.put(4, x, title, curses.A_BOLD)
        return 6

    def left(self) -> int:
        w = self.s.getmaxyx()[1]
        return max(2, (w - 72) // 2)

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.s.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.s.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def footer(self, text: str) -> None:
        h = self.s.getmaxyx()[0]
        self.put(h - 2, self.left(), text, curses.color_pair(3))

    def message(self, title: str, lines: list[tuple[str, int]], foot: str = "") -> None:
        y = self.frame(title)
        for text, attr in lines:
            self.put(y, self.left(), text, attr)
            y += 1
        if foot:
            self.footer(foot)
        self.s.refresh()

    # idle timeout -------------------------------------------------------
    def touched(self) -> None:
        """A key was pressed: restart the countdown."""
        self.idle_until = time.monotonic() + self.idle if self.idle_until is not None else None

    def idle_left(self) -> int | None:
        """Seconds until the signage starts by itself, or None if no countdown."""
        if self.idle_until is None:
            return None
        left = int(self.idle_until - time.monotonic() + 0.999)
        if left <= 0:
            raise IdleTimeout
        return left

    def countdown(self) -> None:
        left = self.idle_left()
        if left is None:
            return
        h = self.s.getmaxyx()[0]
        self.put(h - 4, self.left(),
                 f"No key pressed: starting the signage without Wi-Fi in {left}s.",
                 curses.color_pair(6) | curses.A_BOLD)

    # screens --------------------------------------------------------------
    def wait_for_known(self, seconds: int) -> bool:
        """Wait for a saved network. Any key skips straight to the list."""
        self.s.timeout(1000)
        end = time.monotonic() + seconds
        while True:
            if self.nm.online():
                return True
            left = int(end - time.monotonic())
            if left <= 0:
                return False
            self.message("Connecting to the network...",
                         [(f"Looking for a saved Wi-Fi network or a network cable ({left}s).", 0)],
                         "Press any key to set up Wi-Fi now, or Esc to start the signage.")
            key = self.s.getch()
            if key == ESC:
                raise Quit
            if key != -1:
                return False

    def choose(self) -> tuple[str, Network | None]:
        """Network list. Returns (action, network): action is pick/other/skip/online."""
        networks = self.scan()
        sel = 0
        self.s.timeout(1000)
        last_check = time.monotonic()
        while True:
            items = [n.ssid for n in networks] + [self.OTHER, self.RESCAN, self.SKIP]
            sel = max(0, min(sel, len(items) - 1))
            y = self.frame("Choose a Wi-Fi network")
            x = self.left()
            if not networks:
                self.put(y, x, 'No Wi-Fi networks found. Choose "Scan again" or type a name.', curses.color_pair(6))
                y += 2
            h = self.s.getmaxyx()[0]
            room = max(3, h - y - 4)
            top = max(0, min(sel - room + 1, len(items) - room)) if sel >= room else 0
            for i, label in enumerate(items[top:top + room], start=top):
                attr = curses.color_pair(2) | curses.A_BOLD if i == sel else 0
                mark = ">" if i == sel else " "
                if i < len(networks):
                    n = networks[i]
                    right = ("Locked " if n.secure else "Open   ") + bars(n.signal)
                    line = f"{mark} {label[:52]:<54}{right}  "
                else:
                    line = f"{mark} {label:<66}"
                    if i == len(networks):
                        y += 1
                self.put(y, x, line, attr)
                y += 1
            self.footer("Up/Down choose   Enter select   R scan again   Esc start the signage")
            self.countdown()
            self.s.refresh()
            key = self.s.getch()
            if key == -1:
                # Nothing pressed: if a saved network came back on its own, carry on.
                if not self.force and time.monotonic() - last_check >= 3:
                    last_check = time.monotonic()
                    if self.nm.online():
                        return "online", None
                continue
            self.touched()
            if key == ESC:
                return "skip", None
            if key in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord("j"), 9):
                sel = (sel + 1) % len(items)
            elif key in (curses.KEY_HOME,):
                sel = 0
            elif key in (curses.KEY_END,):
                sel = len(items) - 1
            elif key in (ord("r"), ord("R")):
                networks = self.scan()
            elif key in (10, 13, curses.KEY_ENTER, ord(" ")):
                label = items[sel]
                if sel < len(networks):
                    return "pick", networks[sel]
                if label == self.OTHER:
                    return "other", None
                if label == self.RESCAN:
                    networks = self.scan()
                    sel = 0
                if label == self.SKIP:
                    return "skip", None

    def scan(self) -> list[Network]:
        self.message("Choose a Wi-Fi network", [("Scanning for networks...", curses.color_pair(3))])
        return self.nm.scan()

    def text_input(self, title: str, prompt: str, secret: bool, note: str = "") -> str | None:
        """Single-line input. Returns text, or None if Esc was pressed."""
        value = ""
        show = not secret
        self.s.timeout(1000)
        curses.curs_set(1)
        try:
            while True:
                y = self.frame(title)
                x = self.left()
                self.put(y, x, prompt)
                shown = value if show else "•" * len(value)
                box = f" {shown[-66:]:<68}"
                self.put(y + 2, x, box, curses.color_pair(7))
                if note:
                    self.put(y + 4, x, note, curses.color_pair(6))
                hint = "Enter connect   Esc back"
                if secret:
                    hint += "   Tab " + ("hide" if show else "show") + " password"
                self.footer(hint)
                self.countdown()
                try:
                    self.s.move(y + 2, x + 1 + min(len(shown), 66))
                except curses.error:
                    pass
                self.s.refresh()
                try:
                    ch = self.s.get_wch()
                except curses.error:
                    continue  # no key this second: redraw the countdown
                self.touched()
                if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                    return value
                if ch == "\x1b":
                    return None
                if ch == "\t" and secret:
                    show = not show
                elif ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
                    value = value[:-1]
                elif ch == "\x15":  # Ctrl+U clears
                    value = ""
                elif isinstance(ch, str) and ch.isprintable() and len(value) < 128:
                    value += ch
        finally:
            curses.curs_set(0)

    def connect(self, ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
        self.message("Connecting...", [(f'Joining "{ssid}". This can take up to 30 seconds.', 0)])
        try:
            return self.nm.connect(ssid, password, hidden)
        finally:
            self.touched()  # give them the full countdown to read any error

    def success(self, ssid: str) -> None:
        ip = self.nm.ip_address()
        lines = [(f'Connected to "{ssid}".' if ssid else "Connected to the network.", curses.color_pair(5) | curses.A_BOLD),
                 ("", 0),
                 ("Manage the screen from a phone or computer on the same network:", 0),
                 ("", 0),
                 (f"    http://{self.hostname}.local", curses.A_BOLD)]
        if ip:
            lines.append((f"    http://{ip}", curses.A_BOLD))
        self.s.timeout(1000)
        end = time.monotonic() + SUCCESS_SECONDS
        while (left := int(end - time.monotonic())) > 0:
            self.message("You're online", lines, f"Starting the signage in {left}s. Press Enter to start now.")
            if self.s.getch() in (10, 13, curses.KEY_ENTER):
                break

    def run(self, force: bool) -> int:
        self.force = force
        try:
            if not force and self.wait_for_known(self.wait):
                return 0
        except Quit:
            return 0
        # At boot nobody may be there to answer: count down to starting the
        # signage. Running it by hand (--force) means someone is at the keyboard.
        if not force and self.idle > 0:
            self.idle_until = time.monotonic() + self.idle
        try:
            return self.wizard()
        except IdleTimeout:
            return 0

    def wizard(self) -> int:
        note = ""
        while True:
            action, net = self.choose()
            if action == "online":
                self.success("")
                return 0
            if action == "skip":
                return 0
            hidden = action == "other"
            if hidden:
                ssid = self.text_input("Other network", "Network name (exactly as written, capitals matter):", secret=False)
                if not ssid:
                    continue
                secure = True
            else:
                ssid, secure = net.ssid, net.secure
            while True:
                password = ""
                if secure:
                    password = self.text_input(f'Password for "{ssid}"',
                                               "Wi-Fi password" + (" (leave empty if there isn't one)" if hidden else "") + ":",
                                               secret=True, note=note)
                    if password is None:
                        note = ""
                        break
                ok, err = self.connect(ssid, password, hidden)
                if ok:
                    self.success(ssid)
                    return 0
                note = err or "Could not connect."
                if not secure:
                    self.s.timeout(1000)
                    while True:
                        self.message("Couldn't connect", [(note, curses.color_pair(6))], "Press any key to go back.")
                        self.countdown()
                        self.s.refresh()
                        if self.s.getch() != -1:
                            self.touched()
                            break
                    note = ""
                    break


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--force", action="store_true", help="show the wizard even when already online")
    p.add_argument("--demo", action="store_true", help="use fake networks (for trying the UI)")
    p.add_argument("--wait", type=int, default=WAIT_FOR_KNOWN_SECONDS, help="seconds to wait for a saved network")
    p.add_argument("--idle", type=int, default=IDLE_SECONDS,
                   help="start the signage after this many seconds with no key pressed (0 = never)")
    args = p.parse_args(argv)

    nm = DemoNM() if args.demo else NM()
    if not args.demo and not args.force and nm.online():
        return 0  # fast path: nothing to show
    if not nm.has_wifi():
        print("No Wi-Fi adapter found; skipping Wi-Fi setup.")
        return 0

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    set_console_font()
    set_palette(True)
    try:
        return curses.wrapper(lambda scr: UI(scr, nm, socket.gethostname().removesuffix(".local"), args.wait, args.idle).run(args.force))
    except KeyboardInterrupt:
        return 0
    finally:
        set_palette(False)
        if os.environ.get("TERM") == "linux":
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
