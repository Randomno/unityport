"""unityport: turn a Windows Unity (Mono) game into a native Linux build."""

import argparse
import struct
from pathlib import Path

from . import assemble, bcl, player, settings, shaders, version


def _il2cpp_metadata_version(data_dir: Path) -> int | None:
    """global-metadata.dat starts with magic 0xFAB11BAF, then the version as int32."""
    path = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    try:
        with path.open("rb") as f:
            magic, ver = struct.unpack("<Ii", f.read(8))
    except (OSError, struct.error):
        return None
    return ver if magic == 0xFAB11BAF else None


def main() -> None:
    ap = argparse.ArgumentParser(prog="unityport", description=__doc__)
    ap.add_argument("game_dir", type=Path, help="Windows game folder (contains <Game>.exe and <Game>_Data)")
    ap.add_argument("-o", "--output", type=Path, help="Output folder (default: <game_dir>-linux)")
    ap.add_argument("--arch", choices=["x86_64", "x86"], default="x86_64")
    ap.add_argument("--info", action="store_true", help="Only print detected version info")
    ap.add_argument("--editor", type=Path,
                    help="Unity editor install of the game's version (2021.2+ games need its Linux class libraries)")
    args = ap.parse_args()

    game_dir = args.game_dir.resolve()
    data_dir = version.find_data_dir(game_dir)
    uv = version.detect(game_dir)
    print(f"Unity version: {uv.version} (revision {uv.revision or 'unknown'})")
    if (game_dir / "GameAssembly.dll").exists():
        meta = _il2cpp_metadata_version(data_dir)
        raise SystemExit("IL2CPP games are not supported (GameAssembly.dll found"
                         + (f", IL2CPP metadata version {meta})" if meta else ")"))
    if args.info:
        return

    linux_bcl = bcl.linux_bcl(uv, args.editor) if bcl.needs_linux_bcl(data_dir / "Managed") else None
    variation = player.pick_variation(player.variations_dir(uv), args.arch)
    print(f"Using player {variation.name}")
    out_dir = (args.output or game_dir.with_name(game_dir.name + "-linux")).resolve()
    if out_dir == game_dir or game_dir in out_dir.parents:
        raise SystemExit("Output folder must be outside the game folder")
    out_data = assemble.assemble(game_dir, data_dir, variation, out_dir, args.arch)
    if linux_bcl:
        print(f"Class libraries: {bcl.install(out_data / 'Managed', linux_bcl)} replaced with Linux ones")
    settings.patch_for_opengl(out_data)
    print("Translating D3D11 shaders to OpenGL Core")
    shaders.convert_data_dir(out_data)
    print(f"Linux build written to {out_dir}")


if __name__ == "__main__":
    main()
