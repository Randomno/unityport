"""Paths across WSL: Windows paths for the Linux build running in WSL, and WSL paths for
the Windows build, so either accepts paths copied from the other side."""

import functools
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from . import bundled

STEAM_COMMON = Path("Program Files (x86)") / "Steam" / "steamapps" / "common"


def in_wsl() -> bool:
    if os.name == "nt":
        return False
    if "WSL_DISTRO_NAME" in os.environ:
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def distros() -> list[str]:
    """Installed WSL distributions, the default first (Windows only)."""
    if os.name != "nt":
        return []
    import winreg

    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss") as key:
            try:
                default = winreg.QueryValueEx(key, "DefaultDistribution")[0]
            except OSError:
                default = None
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, guid) as sub:
                        name = winreg.QueryValueEx(sub, "DistributionName")[0]
                except OSError:
                    continue
                if name.startswith("docker-desktop"):
                    continue  # Docker Desktop's internal distributions
                names.insert(0, name) if guid == default else names.append(name)
    except OSError:
        pass
    return names


@functools.lru_cache(maxsize=None)
def preferred_distros() -> tuple[str, ...]:
    """distros(), running ones first: a Linux path copied from a terminal is from one of those."""
    names = distros()
    try:
        r = subprocess.run(["wsl.exe", "--list", "--running", "--quiet"], capture_output=True, timeout=10,
                           **bundled.NO_WINDOW)
        # UTF-16 unless WSL_UTF8 is set
        out = r.stdout.decode("utf-16-le" if b"\0" in r.stdout else "utf-8", "replace")
        running = [n for n in names if n in out.split()]
    except (OSError, subprocess.SubprocessError):
        running = []
    return tuple(running + [n for n in names if n not in running])


def normalize(text: str, windows: bool | None = None, wsl: bool | None = None,
              distro: str | None = None) -> str:
    """Turn a typed, pasted or dropped path into one this system can open.

    Strips quotes ("Copy as path" adds them) and file:// URIs. On Windows, /mnt/c/...
    becomes C:\\... and other Linux paths go through \\\\wsl.localhost; in WSL, C:\\...
    becomes /mnt/c/... and \\\\wsl.localhost\\<distro>\\... becomes a plain path.
    """
    windows = os.name == "nt" if windows is None else windows
    wsl = in_wsl() if wsl is None else wsl
    text = text.strip().strip('"\'')
    if text.startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
        if re.match(r"^/[A-Za-z]:/", text):
            text = text[1:]
    if windows:
        m = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", text)
        if m:
            return f"{m.group(1).upper()}:\\" + (m.group(2) or "").replace("/", "\\")
        if text.startswith("/") and not text.startswith("//"):  # "//server/share" is UNC
            distro = distro or next(iter(preferred_distros()), None)
            if distro:
                return f"\\\\wsl.localhost\\{distro}" + text.replace("/", "\\")
    elif wsl:
        m = re.match(r"^([A-Za-z]):(?:[\\/](.*))?$", text)
        if m:
            return f"/mnt/{m.group(1).lower()}/" + (m.group(2) or "").replace("\\", "/")
        m = re.match(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+(\\.*)?$", text, re.IGNORECASE)
        if m:
            return (m.group(1) or "/").replace("\\", "/")
    return text


def windows_start() -> str:
    """Where to start browsing Windows folders from WSL: Steam's games folder if there is
    one, else the Windows user's folder."""
    steam = Path("/mnt/c") / STEAM_COMMON
    if steam.is_dir():
        return str(steam)
    try:
        r = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], capture_output=True, text=True,
                           timeout=10, cwd="/mnt/c")
        profile = normalize(r.stdout.strip(), windows=False, wsl=True)
        if Path(profile).is_dir():
            return profile
    except (OSError, subprocess.SubprocessError):
        pass
    return "/mnt/c"


def linux_start(distro: str) -> str:
    """Where to start browsing a WSL distribution from Windows: the home folder."""
    home = Path(f"\\\\wsl.localhost\\{distro}\\home")
    try:
        users = [p for p in home.iterdir() if p.is_dir()]
    except OSError:
        return f"\\\\wsl.localhost\\{distro}"
    return str(users[0]) if len(users) == 1 else str(home)


# File systems reached over a network protocol, where every small read or write is slow:
# from WSL, Windows drives are 9p (drvfs); from Windows, WSL is a network share.
REMOTE_FS = {"9p", "drvfs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs", "virtiofs"}


def remote_fs(path: Path) -> bool:
    """Whether a path is on a network or cross-OS file system."""
    if os.name == "nt":
        if str(path).startswith(("\\\\", "//")):
            return True
        import ctypes

        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(f"{path.drive}\\") == DRIVE_REMOTE
    path = Path(os.path.abspath(path))
    best, fstype = "", ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 3:
                    continue
                # Mount points escape spaces and the like as octal, e.g. \040
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])
                if (path == Path(point) or Path(point) in path.parents) and len(point) >= len(best):
                    best, fstype = point, fields[2]
    except OSError:
        return False
    return fstype in REMOTE_FS


def make_executable(path: Path) -> bool:
    """chmod +x a file that the Windows build wrote into a WSL distribution, through wsl.exe.
    Returns False if the path isn't in WSL or the command fails."""
    m = re.match(r"^[\\/]{2}wsl(?:\.localhost|\$)[\\/]([^\\/]+)([\\/].*)$", str(path), re.IGNORECASE)
    if os.name != "nt" or not m:
        return False
    try:
        r = subprocess.run(["wsl.exe", "-d", m.group(1), "--", "chmod", "+x", m.group(2).replace("\\", "/")],
                           capture_output=True, timeout=60, **bundled.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
