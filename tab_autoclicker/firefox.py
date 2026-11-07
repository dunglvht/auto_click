"""Finding, preparing and launching Firefox with remote control enabled."""

from __future__ import annotations

import configparser
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, "snap", "firefox", "common", ".mozilla", "firefox")
CLASSIC_ROOT = os.path.join(HOME, ".mozilla", "firefox")

# Marker used for lines this app writes into a profile's user.js.
USER_JS_MARK = "// added by Firefox Tab Auto Clicker"


@dataclass
class Profile:
    name: str
    path: str
    is_default: bool = False
    separate: bool = False  # the app's own dedicated profile

    @property
    def label(self) -> str:
        if self.separate:
            return "Separate Auto Clicker profile (clean, no logins)"
        return f"{self.name}{'  (default)' if self.is_default else ''}"


def find_firefox() -> Optional[str]:
    for name in ("firefox", "firefox-esr", "firefox-developer-edition", "firefox-nightly"):
        path = shutil.which(name)
        if path:
            return path
    if os.path.exists("/snap/bin/firefox"):
        return "/snap/bin/firefox"
    return None


def is_snap_firefox(binary: Optional[str]) -> bool:
    if not binary:
        return False
    if "/snap/" in os.path.realpath(binary):
        return True
    try:
        with open(binary, "rb") as fh:
            head = fh.read(4096)
        return b"snap" in head and os.path.isdir("/snap/firefox")
    except OSError:
        return False


def profiles_root(binary: Optional[str]) -> str:
    if is_snap_firefox(binary) and os.path.isdir(SNAP_ROOT):
        return SNAP_ROOT
    if os.path.isdir(CLASSIC_ROOT):
        return CLASSIC_ROOT
    return SNAP_ROOT if os.path.isdir(SNAP_ROOT) else CLASSIC_ROOT


def separate_profile_dir(binary: Optional[str]) -> str:
    # A snap-confined Firefox can only use folders under ~/snap/firefox/ or
    # non-hidden folders in $HOME, so keep the dedicated profile there.
    if is_snap_firefox(binary):
        return os.path.join(HOME, "snap", "firefox", "common", "tab-autoclicker-profile")
    return os.path.join(HOME, ".local", "share", "tab-autoclicker", "firefox-profile")


def list_profiles(binary: Optional[str]) -> list[Profile]:
    root = profiles_root(binary)
    ini = configparser.RawConfigParser()
    ini.read(os.path.join(root, "profiles.ini"))
    installs = configparser.RawConfigParser()
    installs.read(os.path.join(root, "installs.ini"))
    install_defaults = {installs.get(s, "Default") for s in installs.sections()
                        if installs.has_option(s, "Default")}
    # [Install...] sections inside profiles.ini also name the default profile.
    for s in ini.sections():
        if s.startswith("Install") and ini.has_option(s, "Default"):
            install_defaults.add(ini.get(s, "Default"))

    profiles: list[Profile] = []
    for s in ini.sections():
        if not s.startswith("Profile") or not ini.has_option(s, "Path"):
            continue
        rel = ini.get(s, "Path")
        relative = ini.get(s, "IsRelative", fallback="1") == "1"
        path = os.path.join(root, rel) if relative else rel
        if not os.path.isdir(path):
            continue
        profiles.append(Profile(
            name=ini.get(s, "Name", fallback=os.path.basename(path)),
            path=path,
            is_default=(rel in install_defaults) if install_defaults
            else ini.get(s, "Default", fallback="0") == "1",
        ))
    profiles.sort(key=lambda p: (not p.is_default, p.name.lower()))
    profiles.append(Profile("auto-clicker", separate_profile_dir(binary), separate=True))
    return profiles


def profile_owner_pid(profile_dir: str) -> Optional[int]:
    """Return the PID of the Firefox currently using the profile, if any."""
    try:
        target = os.readlink(os.path.join(profile_dir, "lock"))  # "host:+PID"
        pid = int(target.rsplit("+", 1)[1])
    except (OSError, ValueError, IndexError):
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read()
    except OSError:
        return None  # stale lock left behind by a crash
    return pid if b"firefox" in cmd.lower() else None


def profile_serving_port(profiles: list[Profile], port: int) -> Optional[Profile]:
    """The profile of the running Firefox started with ``--remote-debugging-port port``."""
    for prof in profiles:
        pid = profile_owner_pid(prof.path)
        if pid is None:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                args = fh.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        for i, arg in enumerate(args):
            value = arg.split("=", 1)[1] if arg.startswith("--remote-debugging-port=") else (
                args[i + 1] if arg in ("--remote-debugging-port", "-remote-debugging-port")
                and i + 1 < len(args) else None)
            if value is not None and value.strip() == str(port):
                return prof
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def set_user_pref(profile_dir: str, name: str, value) -> bool:
    """Make sure ``user.js`` sets ``name`` to ``value``. Returns True if changed."""
    literal = ("true" if value else "false") if isinstance(value, bool) else (
        str(value) if isinstance(value, int) else '"%s"' % str(value).replace('"', '\\"'))
    line = f'user_pref("{name}", {literal}); {USER_JS_MARK}'
    path = os.path.join(profile_dir, "user.js")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    needle = f'user_pref("{name}",'
    for i, existing in enumerate(lines):
        if existing.strip().startswith(needle):
            if existing.strip() == line:
                return False
            lines[i] = line
            break
    else:
        lines.append(line)
    os.makedirs(profile_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return True


def request_session_restore(profile_dir: str) -> bool:
    """Ask Firefox to reopen the previous windows/tabs once on its next start.

    Only call this while Firefox is *not* running with this profile (Firefox
    rewrites prefs.js on exit). Firefox clears the preference after using it,
    so this is a one-time effect, exactly like Firefox's own "Restart".
    """
    path = os.path.join(profile_dir, "prefs.js")
    if not os.path.isfile(path):
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('user_pref("browser.sessionstore.resume_session_once", true);\n')
    return True


def launch(binary: str, profile_dir: str, port: int) -> subprocess.Popen:
    os.makedirs(profile_dir, exist_ok=True)
    args = [binary, "--new-instance", "--profile", profile_dir,
            "--remote-debugging-port", str(port)]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def wait_for_port(port: int, timeout: float = 45.0, proc: Optional[subprocess.Popen] = None,
                  cancelled=lambda: False) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cancelled():
        if port_open(port):
            return True
        if proc is not None and proc.poll() is not None:
            # The launcher exited (e.g. the profile was locked): give the port
            # a short grace period, then give up instead of waiting forever.
            deadline = min(deadline, time.monotonic() + 3.0)
        time.sleep(0.4)
    return port_open(port)
