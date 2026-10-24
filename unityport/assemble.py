"""Combine a Windows game's data with the Linux player into a new build folder."""

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import bundled, version

# Per-platform runtime pieces in <Game>_Data that come from the Linux player instead.
PLATFORM_DIRS = {"Mono", "MonoBleedingEdge", "Plugins"}
# "Compressed" builds pack the serialized files into one bundle.
DATA_BUNDLE = "data.unity3d"
DEFAULT_RESOURCES = Path("Resources") / "unity default resources"


def assemble(game_dir: Path, data_dir: Path, variation: Path, out_dir: Path, arch: str) -> Path:
    """Build the Linux game in out_dir and return its <Game>_Data directory."""
    name = version.game_name(game_dir, data_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    out_data = out_dir / f"{name}_Data"
    linux_data = variation / "Data"

    # Game data, minus the Windows runtime.
    def ignore(src, names):
        if Path(src) == data_dir:
            return [n for n in names if n in PLATFORM_DIRS or n == DATA_BUNDLE]
        return []

    shutil.copytree(data_dir, out_data, ignore=ignore)
    if (data_dir / DATA_BUNDLE).is_file():
        _unpack_bundle(data_dir / DATA_BUNDLE, out_data)

    # Engine assemblies: Linux versions replace Windows ones. Assemblies that
    # only the game ships (Assembly-CSharp, UnityEngine.UI, mscorlib...) stay.
    for dll in (linux_data / "Managed").glob("*.dll"):
        shutil.copy2(dll, out_data / "Managed" / dll.name)

    # Scripting runtime: match whichever one the game was built against.
    # (2018.x+ Windows builds keep MonoBleedingEdge next to the exe instead of in _Data.)
    uses_mbe = (data_dir / "MonoBleedingEdge").is_dir() or (game_dir / "MonoBleedingEdge").is_dir()
    runtime = "MonoBleedingEdge" if uses_mbe else "Mono"
    if not (linux_data / runtime).is_dir():
        raise RuntimeError(f"Linux player has no {runtime} runtime")
    shutil.copytree(linux_data / runtime, out_data / runtime)
    if (linux_data / "Plugins").is_dir():
        shutil.copytree(linux_data / "Plugins", out_data / "Plugins")

    # Built-in resources (contains engine shaders) must match the player platform.
    shutil.copy2(linux_data / DEFAULT_RESOURCES, out_data / DEFAULT_RESOURCES)

    _write_icon(game_dir, data_dir, out_data)

    # Player executable and, on newer versions, UnityPlayer.so etc.
    for item in variation.iterdir():
        if item.name == "Data":
            continue
        if item.name == "LinuxPlayer":
            dest = out_dir / f"{name}.{arch}"
        else:
            dest = out_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # Match official builds: only the player binary is executable.
    for p in out_dir.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    (out_dir / f"{name}.{arch}").chmod(0o755)
    return out_data


def _unpack_bundle(bundle: Path, out_data: Path) -> None:
    """Extract data.unity3d into loose files, which the player loads when the
    bundle is absent. The settings and shader passes then work on them as usual."""
    import UnityPy

    files = UnityPy.load(str(bundle)).file.files
    print(f"Unpacking {bundle.name} ({len(files)} files)")
    root = out_data.resolve()
    for name, f in files.items():
        dest = (out_data / name).resolve()
        if root not in dest.parents:
            raise RuntimeError(f"Bad path in {bundle.name}: {name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.reader.bytes if hasattr(f, "reader") else f.bytes)


def _write_icon(game_dir: Path, data_dir: Path, out_data: Path) -> None:
    """Linux players take their window icon from Resources/UnityPlayer.png;
    Windows builds embed it in the exe instead."""
    dest = out_data / "Resources" / "UnityPlayer.png"
    exe = game_dir / (version.game_name(game_dir, data_dir) + ".exe")
    if dest.exists() or not exe.exists():
        return
    from PIL import Image  # installed with UnityPy

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([bundled.seven_zip(), "x", "-y", f"-o{tmp}", str(exe), ".rsrc/ICON/*"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **bundled.NO_WINDOW)
        best = None
        for ico in Path(tmp).glob(".rsrc/ICON/*"):
            try:
                # From memory: an open file would stop Windows deleting the folder
                im = Image.open(io.BytesIO(ico.read_bytes()))
                im.load()
            except Exception:
                continue
            if best is None or im.size[0] > best.size[0]:
                best = im
        if best is not None:
            dest.parent.mkdir(exist_ok=True)
            best.convert("RGBA").save(dest)
