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

IS_WINDOWS = os.name == "nt"
HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, "snap", "firefox", "common", ".mozilla", "firefox")
CLASSIC_ROOT = os.path.join(HOME, ".mozilla", "firefox")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
WINDOWS_ROOT = os.path.join(APPDATA, "Mozilla", "Firefox")

# Windows: run helper programs without flashing a console window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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
    if IS_WINDOWS:
        return _find_firefox_windows()
    if os.path.exists("/snap/bin/firefox"):
        return "/snap/bin/firefox"
    return None


def _find_firefox_windows() -> Optional[str]:
    """Firefox on Windows is rarely on the PATH: ask the registry, then try the usual folders."""
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe")
                path = winreg.QueryValue(key, None)
            except OSError:
                continue
            if path and os.path.isfile(path.strip('"')):
                return path.strip('"')
    except ImportError:
        pass
    folders = [os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    folders.append(LOCALAPPDATA)
    for base in filter(None, folders):
        for app in ("Mozilla Firefox", "Firefox Developer Edition", "Firefox Nightly"):
            path = os.path.join(base, app, "firefox.exe")
            if os.path.isfile(path):
                return path
    return None


def is_snap_firefox(binary: Optional[str]) -> bool:
    if not binary or IS_WINDOWS:
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
    if IS_WINDOWS:
        return WINDOWS_ROOT
    if is_snap_firefox(binary) and os.path.isdir(SNAP_ROOT):
        return SNAP_ROOT
    if os.path.isdir(CLASSIC_ROOT):
        return CLASSIC_ROOT
    return SNAP_ROOT if os.path.isdir(SNAP_ROOT) else CLASSIC_ROOT


def separate_profile_dir(binary: Optional[str]) -> str:
    # A snap-confined Firefox can only use folders under ~/snap/firefox/ or
    # non-hidden folders in $HOME, so keep the dedicated profile there.
    if IS_WINDOWS:
        return os.path.join(LOCALAPPDATA, "tab-autoclicker", "firefox-profile")
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
        path = os.path.normpath(os.path.join(root, rel) if relative else rel)
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
    """Return the PID of the Firefox currently using the profile, if any.

    On Windows the PID is not known cheaply, so a profile in use gives 0;
    compare the result with None, never test its truth value.
    """
    if IS_WINDOWS:
        return 0 if _windows_profile_locked(profile_dir) else None
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


def _windows_profile_locked(profile_dir: str) -> bool:
    """A running Firefox holds parent.lock open without sharing it; after a
    clean exit the file is gone, after a crash it can be opened again."""
    try:
        with open(os.path.join(profile_dir, "parent.lock"), "rb"):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _windows_firefox_cmdlines() -> list[list[str]]:
    """The argument lists of all running firefox.exe processes."""
    import ctypes
    from ctypes import wintypes
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | "
             "ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    to_argv = ctypes.windll.shell32.CommandLineToArgvW
    to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    result = []
    for line in out.splitlines():
        if not line.strip():
            continue
        n = ctypes.c_int()
        argv = to_argv(line, ctypes.byref(n))
        if not argv:
            continue
        try:
            result.append([argv[i] for i in range(n.value)])
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    return result


def _arg_value(args: list[str], name: str) -> Optional[str]:
    """The value of ``--name value``, ``--name=value`` or ``-name value`` in args."""
    for i, arg in enumerate(args):
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
        if arg in (f"--{name}", f"-{name}") and i + 1 < len(args):
            return args[i + 1]
    return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _windows_profile_serving_port(profiles: list[Profile], port: int) -> Optional[Profile]:
    for args in _windows_firefox_cmdlines():
        value = _arg_value(args, "remote-debugging-port")
        if value is None or value.strip() != str(port):
            continue
        path = _arg_value(args, "profile")
        if path:
            return next((p for p in profiles if _same_path(p.path, path)), None)
        # Started without --profile: that is the default profile, if it is in use.
        return next((p for p in profiles if p.is_default and _windows_profile_locked(p.path)), None)
    return None


def profile_serving_port(profiles: list[Profile], port: int) -> Optional[Profile]:
    """The profile of the running Firefox started with ``--remote-debugging-port port``."""
    if IS_WINDOWS:
        return _windows_profile_serving_port(profiles, port)
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


def remove_user_pref(profile_dir: str, name: str) -> bool:
    """Remove the app's own ``user.js`` line for ``name`` (lines the user wrote
    stay). Firefox keeps the last value until it is changed in Firefox.
    Returns True if a line was removed."""
    path = os.path.join(profile_dir, "user.js")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return False
    needle = f'user_pref("{name}",'
    kept = [ln for ln in lines if not (ln.strip().startswith(needle) and USER_JS_MARK in ln)]
    if len(kept) == len(lines):
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + ("\n" if kept else ""))
    return True


def hide_sidebar(profile_dir: str, hide: bool) -> bool:
    """Hide Firefox's sidebar launcher (the strip of tool icons on the left),
    as unticking "Open tools from the sidebar" in Firefox's "Customize
    sidebar" does: the toolbar's sidebar button still opens the tools. With
    vertical tabs the equivalent is "hide-sidebar". ``hide=False`` removes the
    app's setting again. Returns True if user.js changed."""
    if not hide:
        return remove_user_pref(profile_dir, "sidebar.visibility")
    try:
        with open(os.path.join(profile_dir, "prefs.js"), encoding="utf-8", errors="replace") as fh:
            vertical = 'user_pref("sidebar.verticalTabs", true);' in fh.read()
    except OSError:
        vertical = False
    return set_user_pref(profile_dir, "sidebar.visibility", "hide-sidebar" if vertical else "hide-launcher")


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


def windows_args() -> list[str]:
    """Extra Firefox arguments needed on Windows.

    Windows Firefox starts through a small launcher process that hands over to
    the browser and quits at once; ``--wait-for-browser`` keeps it until the
    browser exits, so the returned process tells whether Firefox still runs.
    """
    return ["--wait-for-browser"] if IS_WINDOWS else []


def prepare_profile(profile_dir: str) -> None:
    """Profile settings the app needs on this system (written to user.js)."""
    if IS_WINDOWS:
        # Windows Firefox pauses pages whose window is covered by other
        # windows; the game would stop while the bot plays in the background.
        set_user_pref(profile_dir, "widget.windows.window_occlusion_tracking.enabled", False)


def launch(binary: str, profile_dir: str, port: int) -> subprocess.Popen:
    os.makedirs(profile_dir, exist_ok=True)
    prepare_profile(profile_dir)
    args = [binary, "--new-instance", "--profile", profile_dir,
            "--remote-debugging-port", str(port), *windows_args()]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def stop(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    """End a Firefox started by this app or the tests, with all its child processes."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=_NO_WINDOW)
    else:
        proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


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
