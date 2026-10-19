"""Combine a Windows game's data with the Linux player into a new build folder."""

import shutil
import subprocess
import tempfile
from pathlib import Path

# Per-platform runtime pieces in <Game>_Data that come from the Linux player instead.
PLATFORM_DIRS = {"Mono", "MonoBleedingEdge", "Plugins"}
DEFAULT_RESOURCES = Path("Resources") / "unity default resources"


def assemble(game_dir: Path, data_dir: Path, variation: Path, out_dir: Path, arch: str) -> Path:
    """Build the Linux game in out_dir and return its <Game>_Data directory."""
    name = data_dir.name[: -len("_Data")]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    out_data = out_dir / data_dir.name
    linux_data = variation / "Data"

    # Game data, minus the Windows runtime.
    def ignore(src, names):
        if Path(src) == data_dir:
            return [n for n in names if n in PLATFORM_DIRS]
        return []

    shutil.copytree(data_dir, out_data, ignore=ignore)
    _report_windows_plugins(data_dir)

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


def _report_windows_plugins(data_dir: Path) -> None:
    plugins = data_dir / "Plugins"
    if not plugins.is_dir():
        return
    dlls = [p.relative_to(data_dir) for p in plugins.rglob("*.dll")]
    if dlls:
        print("Warning: native Windows plugins need Linux equivalents:")
        for d in dlls:
            print(f"  {d}")


def _write_icon(game_dir: Path, data_dir: Path, out_data: Path) -> None:
    """Linux players take their window icon from Resources/UnityPlayer.png;
    Windows builds embed it in the exe instead."""
    dest = out_data / "Resources" / "UnityPlayer.png"
    exe = game_dir / (data_dir.name[: -len("_Data")] + ".exe")
    if dest.exists() or not exe.exists():
        return
    from PIL import Image  # installed with UnityPy

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["7z", "x", "-y", f"-o{tmp}", str(exe), ".rsrc/ICON/*"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        best = None
        for ico in Path(tmp).glob(".rsrc/ICON/*"):
            try:
                im = Image.open(ico)
                im.load()
            except Exception:
                continue
            if best is None or im.size[0] > best.size[0]:
                best = im
        if best is not None:
            dest.parent.mkdir(exist_ok=True)
            best.convert("RGBA").save(dest)
