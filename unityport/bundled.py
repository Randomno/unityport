"""Locate the helper programs, which a self-contained build (tools/bundle.py) carries inside itself."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# PyInstaller unpacks bundled files here
BUNDLE_DIR = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else None
EXE = ".exe" if os.name == "nt" else ""
# 7-Zip's Linux build is one static binary; the Windows one needs 7z.dll beside it
SEVEN_ZIP = "7z.exe" if os.name == "nt" else "7zz"
# For running the helpers: the Windows executable has no console, so without this each
# console program it starts would open a window of its own
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def hlslcc_tool() -> Path:
    if BUNDLE_DIR:
        return BUNDLE_DIR / f"hlslcc_tool{EXE}"
    build = Path(__file__).resolve().parent.parent / "native" / "build"
    # Visual Studio puts it in a per-configuration folder
    for path in (build / f"hlslcc_tool{EXE}", build / "Release" / f"hlslcc_tool{EXE}"):
        if path.exists():
            return path
    return build / f"hlslcc_tool{EXE}"


def seven_zip() -> str:
    if BUNDLE_DIR and (BUNDLE_DIR / SEVEN_ZIP).is_file():
        return str(BUNDLE_DIR / SEVEN_ZIP)
    found = shutil.which("7z") or shutil.which("7zz")
    if not found and os.name == "nt":
        default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe"
        if default.is_file():
            return str(default)
    return found or "7z"


def host_env() -> dict[str, str]:
    """Environment for running the system's own programs (file dialogs, xdg-open).
    PyInstaller points LD_LIBRARY_PATH at the bundled libraries, which they must not load."""
    env = dict(os.environ)
    if BUNDLE_DIR:
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env
