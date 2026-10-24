"""Detect which Unity version (and build revision) a game was made with."""

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

# Matches e.g. 2017.3.0f3, 5.4.4p1, 2021.3.11f1
VERSION_RE = rb"(\d{1,4}\.\d+\.\d+[abfpx]\d+)"



@dataclass
class UnityVersion:
    version: str
    revision: str | None  # short changeset hash, e.g. a9f86dcd79df

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])


def _game_exes(game_dir: Path) -> list[Path]:
    return [e for e in sorted(game_dir.glob("*.exe")) if not e.stem.startswith("UnityCrashHandler")]


def find_data_dir(game_dir: Path) -> Path:
    """Return the <Game>_Data directory of a Windows build, or a folder named Data, which
    the player also accepts (and which survives renaming the exe)."""
    for exe in _game_exes(game_dir):
        data = game_dir / (exe.stem + "_Data")
        if data.is_dir():
            return data
    candidates = [p for p in game_dir.glob("*_Data") if p.is_dir()]
    if len(candidates) == 1:
        return candidates[0]
    data = game_dir / "Data"
    if (data / "globalgamemanagers").is_file() or (data / "data.unity3d").is_file():
        return data
    raise FileNotFoundError(f"No <Game>_Data directory found in {game_dir}")


def game_name(game_dir: Path, data_dir: Path) -> str:
    """The game's name, for the Linux executable and its <name>_Data folder."""
    if data_dir.name.endswith("_Data"):
        return data_dir.name[: -len("_Data")]
    exes = _game_exes(game_dir)
    return exes[0].stem if len(exes) == 1 else game_dir.name


def _serialized_file_version(path: Path) -> str | None:
    """Read the Unity version string from a SerializedFile header.

    Headers before format 22 are: metadataSize, fileSize, version, dataOffset
    (4 bytes each), then endianness + 3 reserved bytes, then the version
    string. Format 22+ inserts 64-bit sizes. Rather than handle every layout,
    search the first bytes for a NUL-terminated version string.
    """
    with open(path, "rb") as f:
        head = f.read(128)
    m = re.search(rb"\x00" + VERSION_RE + rb"\x00", head)
    return m.group(1).decode() if m else None


def version_from_data(data_dir: Path) -> str | None:
    for name in ("globalgamemanagers", "mainData", "level0", "data.unity3d"):
        p = data_dir / name
        if p.is_file():
            if name == "data.unity3d":
                # UnityFS bundle: "UnityFS\0" <u32 format> "5.x.x\0" <engine version>\0
                with open(p, "rb") as f:
                    head = f.read(64)
                m = re.search(rb"\x00" + VERSION_RE + rb"\x00", head)
            else:
                v = _serialized_file_version(p)
                if v:
                    return v
                continue
            if m:
                return m.group(1).decode()
    return None


def _revision_from_binaries(game_dir: Path, version: str) -> str | None:
    """UnityPlayer.dll (2017.2+) or the game exe (older) embeds "<version>_<hash>"."""
    pattern_ascii = re.escape(version.encode()) + rb"_([0-9a-f]{12})"
    pattern_utf16 = re.escape(version.encode("utf-16-le")) + rb"_\x00((?:[0-9a-f]\x00){12})"
    binaries = [game_dir / "UnityPlayer.dll"] + sorted(game_dir.glob("*.exe"))
    for b in binaries:
        if not b.is_file():
            continue
        blob = b.read_bytes()
        m = re.search(pattern_ascii, blob)
        if m:
            return m.group(1).decode()
        m = re.search(pattern_utf16, blob)
        if m:
            return m.group(1).decode("utf-16-le")
    return None


def _revision_from_api(version: str) -> str | None:
    try:
        with urllib.request.urlopen(config.releases_api(version), timeout=30) as r:
            data = json.load(r)
    except OSError:
        return None
    for rel in data.get("results", []):
        if rel.get("version") != version:
            continue
        if rel.get("shortRevision"):
            return rel["shortRevision"]
        for dl in rel.get("downloads", []):
            m = re.search(r"/download_unity/([0-9a-f]{12})/", dl.get("url", ""))
            if m:
                return m.group(1)
    return None


def detect(game_dir: Path) -> UnityVersion:
    data_dir = find_data_dir(game_dir)
    version = version_from_data(data_dir)
    if not version:
        raise RuntimeError(f"Could not determine Unity version from {data_dir}")
    revision = _revision_from_binaries(game_dir, version) or _revision_from_api(version)
    return UnityVersion(version, revision)
