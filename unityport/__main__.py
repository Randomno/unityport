"""unityport: turn a Windows Unity (Mono) game into a native Linux build."""

import argparse
from pathlib import Path

from . import assemble, player, settings, shaders, version


def main() -> None:
    ap = argparse.ArgumentParser(prog="unityport", description=__doc__)
    ap.add_argument("game_dir", type=Path, help="Windows game folder (contains <Game>.exe and <Game>_Data)")
    ap.add_argument("-o", "--output", type=Path, help="Output folder (default: <game_dir>-linux)")
    ap.add_argument("--arch", choices=["x86_64", "x86"], default="x86_64")
    ap.add_argument("--info", action="store_true", help="Only print detected version info")
    args = ap.parse_args()

    game_dir = args.game_dir.resolve()
    data_dir = version.find_data_dir(game_dir)
    uv = version.detect(game_dir)
    print(f"Unity version: {uv.version} (revision {uv.revision or 'unknown'})")
    if (game_dir / "GameAssembly.dll").exists():
        raise SystemExit("IL2CPP games are not supported (GameAssembly.dll found)")
    if args.info:
        return

    variation = player.pick_variation(player.variations_dir(uv), args.arch)
    print(f"Using player {variation.name}")
    out_dir = (args.output or game_dir.with_name(game_dir.name + "-linux")).resolve()
    if out_dir == game_dir or game_dir in out_dir.parents:
        raise SystemExit("Output folder must be outside the game folder")
    out_data = assemble.assemble(game_dir, data_dir, variation, out_dir, args.arch)
    settings.patch_for_opengl(out_data)
    print("Translating D3D11 shaders to OpenGL Core")
    shaders.convert_data_dir(out_data)
    print(f"Linux build written to {out_dir}")


if __name__ == "__main__":
    main()
