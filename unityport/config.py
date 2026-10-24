"""unityport's own folder, and the settings saved in it.

Everything unityport keeps is in one folder (UNITYPORT_HOME overrides it):
settings.json, set in the window's Advanced settings and also read by the command
line, and cache/, the downloaded Linux players and class libraries.
"""

import json
import os
import shutil
from pathlib import Path

if os.name == "nt":
    _DEFAULT_HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "unityport"
else:
    _DEFAULT_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "unityport"
HOME = Path(os.environ.get("UNITYPORT_HOME", _DEFAULT_HOME))
CACHE = HOME / "cache"
SETTINGS = HOME / "settings.json"

ARCHES = ("x86_64", "x86")
DEFAULTS = {
    # The Linux player: 64-bit, or 32-bit, which only older Unity versions include
    "arch": "x86_64",
    # Per-build folder holding the manifests (unity-<version>-<platform>.ini), the
    # installers they list and LinuxEditorInstaller/Unity.tar.xz
    "download_url": "https://download.unity3d.com/download_unity/{rev}/",
    # Looks up a version's build revision, when the game's files don't contain it
    "releases_api": "https://services.api.unity.com/unity/editor/release/v1/releases?version={version}",
}
# The placeholder each URL must contain, and what it stands for
PLACEHOLDERS = {"download_url": ("{rev}", "build revision"), "releases_api": ("{version}", "Unity version")}

_overrides: dict[str, str] = {}


def migrate() -> None:
    """Move a cache from where earlier versions kept it: ~/.cache/unityport on Linux, and
    straight in the folder, without cache/, on Windows."""
    if "UNITYPORT_HOME" in os.environ or CACHE.exists():
        return
    try:
        if os.name == "nt":
            for p in [p for p in HOME.iterdir() if p.is_dir()] if HOME.is_dir() else []:
                CACHE.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(CACHE / p.name))
        else:
            old = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "unityport"
            if old.is_dir():
                HOME.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(CACHE))
    except OSError:
        pass  # downloaded again when needed


def check(name: str, value: str) -> str | None:
    """Why a value can't be used, or None if it can."""
    if name == "arch":
        return None if value in ARCHES else f"It must be one of {', '.join(ARCHES)}."
    if not value.startswith(("http://", "https://")):
        return "It must start with http:// or https://."
    placeholder, meaning = PLACEHOLDERS[name]
    if placeholder not in value:
        return f"It must contain {placeholder}, where unityport puts the {meaning}."
    return None


def _read() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved() -> dict[str, str]:
    """The saved values that differ from the defaults."""
    return {k: v for k, v in _read().items() if k in DEFAULTS and isinstance(v, str) and not check(k, v)}


def save(values: dict[str, str]) -> None:
    """Save the values, leaving out those equal to the defaults."""
    data = _read()
    for name in DEFAULTS:
        data.pop(name, None)
        if values.get(name, DEFAULTS[name]) != DEFAULTS[name]:
            data[name] = values[name]
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def override(name: str, value: str) -> None:
    """Use a value for this run only (the command line's options)."""
    problem = check(name, value)
    if problem:
        raise SystemExit(f"{value}: {problem}")
    _overrides[name] = value


def get(name: str) -> str:
    return _overrides.get(name) or saved().get(name) or DEFAULTS[name]


def download_base(rev: str) -> str:
    """The folder URL for a build revision, ending in /."""
    url = get("download_url").replace("{rev}", rev)
    return url if url.endswith("/") else url + "/"


def releases_api(version: str) -> str:
    return get("releases_api").replace("{version}", version)
