"""Fetch the Unity Linux player matching a game's engine version.

Unity publishes a per-build manifest at download_unity/<hash>/unity-<ver>-win.ini
listing each component installer. The "Linux Build Support" installer is an
NSIS executable (~100MB) that 7z can unpack, which is far smaller than the
full Linux editor tarball.
"""

import configparser
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .version import UnityVersion

BASE = "https://download.unity3d.com/download_unity/{rev}/"
CACHE = Path(os.environ.get("UNITYPORT_CACHE", Path.home() / ".cache" / "unityport"))


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.rename(dest)


def _linux_installer_urls(uv: UnityVersion) -> list[str]:
    """Candidate Linux player installers, best first.

    The macOS .pkg (xar -> gzip -> cpio) unpacks cleanly with 7z for every
    version tried. The Windows NSIS .exe also works for older versions, but
    from around 2020 its header flags are not understood by 7z.
    """
    base = BASE.format(rev=uv.revision)
    urls = []
    for platform in ("osx", "win"):
        try:
            with urllib.request.urlopen(base + f"unity-{uv.version}-{platform}.ini", timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except OSError:
            continue
        ini = configparser.ConfigParser(interpolation=None, strict=False)
        ini.read_string(text)
        # Newer versions split Linux support into Mono and IL2CPP installers.
        for section in ("Linux-Mono", "Linux"):
            if ini.has_section(section) and ini.has_option(section, "url"):
                urls.append(base + ini.get(section, "url"))
                break
    # Conventional name, for builds whose manifest is missing.
    urls.append(base + f"TargetSupportInstaller/UnitySetup-Linux-Support-for-Editor-{uv.version}.exe")
    return list(dict.fromkeys(urls))


def _extract_nested(archive: Path, dest: Path, depth: int = 3) -> None:
    """7z-extract an archive, then keep unpacking while the result is a lone archive."""
    subprocess.run(["7z", "x", "-y", f"-o{dest}", str(archive)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    files = [p for p in dest.rglob("*") if p.is_file()]
    if depth and len(files) == 1 and not any(dest.glob("**/Variations")):
        inner = files[0]
        _extract_nested(inner, dest / "inner", depth - 1)
        inner.unlink()


def variations_dir(uv: UnityVersion) -> Path:
    """Return the extracted LinuxStandaloneSupport/Variations directory."""
    root = CACHE / uv.version
    extract = root / "extract"
    found = list(extract.glob("**/Variations")) if extract.exists() else []
    if found:
        return found[0]

    if not uv.revision:
        raise RuntimeError(f"Unknown build revision for Unity {uv.version}; cannot download player")
    root.mkdir(parents=True, exist_ok=True)
    for url in _linux_installer_urls(uv):
        installer = root / ("linux-support" + Path(url).suffix)
        if not installer.exists():
            try:
                _download(url, installer)
            except OSError as e:
                print(f"  {e}")
                continue
        print(f"Extracting {installer}")
        tmp = root / "extract.part"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _extract_nested(installer, tmp)
        except subprocess.CalledProcessError:
            pass
        found = list(tmp.glob("**/Variations"))
        if found:
            shutil.rmtree(extract, ignore_errors=True)
            tmp.rename(extract)
            return next(extract.glob("**/Variations"))
        print(f"  no Linux player found in {installer.name}")
        shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError(f"Could not obtain a Linux player for Unity {uv.version}")


def pick_variation(variations: Path, arch: str) -> Path:
    """Choose the non-development Mono player with graphics for an architecture.

    arch is "x86_64" or "x86". Names differ between versions, e.g.
    linux64_withgfx_nondevelopment_mono (2017) vs linux64_player_nondevelopment_mono (2019+).
    """
    bits = "64" if arch == "x86_64" else "32"
    names = [p.name for p in variations.iterdir() if p.is_dir()]
    for name in names:
        if (name.startswith(f"linux{bits}_") and "nondevelopment" in name and name.endswith("_mono")
                and "headless" not in name and "server" not in name):
            return variations / name
    raise RuntimeError(f"No linux{bits} Mono player in {variations}: {names}")
