"""Detect which Unity version (and build revision) a game was made with."""

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Matches e.g. 2017.3.0f3, 5.4.4p1, 2021.3.11f1
VERSION_RE = rb"(\d{1,4}\.\d+\.\d+[abfpx]\d+)"

RELEASES_API = "https://services.api.unity.com/unity/editor/release/v1/releases?version={}"


@dataclass
class UnityVersion:
    version: str
    revision: str | None  # short changeset hash, e.g. a9f86dcd79df

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])


def find_data_dir(game_dir: Path) -> Path:
    """Return the <Game>_Data directory of a Windows build."""
    for exe in sorted(game_dir.glob("*.exe")):
        data = game_dir / (exe.stem + "_Data")
        if data.is_dir():
            return data
    candidates = [p for p in game_dir.glob("*_Data") if p.is_dir()]
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError(f"No <Game>_Data directory found in {game_dir}")


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


def _version_from_data(data_dir: Path) -> str | None:
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
        with urllib.request.urlopen(RELEASES_API.format(version), timeout=30) as r:
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
    version = _version_from_data(data_dir)
    if not version:
        raise RuntimeError(f"Could not determine Unity version from {data_dir}")
    revision = _revision_from_binaries(game_dir, version) or _revision_from_api(version)
    return UnityVersion(version, revision)
