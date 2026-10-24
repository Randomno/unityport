"""unityport: turn a Windows Unity (Mono) game into a native Linux build."""

import argparse
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

from . import __version__, assemble, bcl, player, plugins, report, settings, shaders, config, version, wsl


def _il2cpp_metadata_version(data_dir: Path) -> int | None:
    """global-metadata.dat starts with magic 0xFAB11BAF, then the version as int32."""
    path = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    try:
        with path.open("rb") as f:
            magic, ver = struct.unpack("<Ii", f.read(8))
    except (OSError, struct.error):
        return None
    return ver if magic == 0xFAB11BAF else None


def port(game_dir: Path, output: Path | None = None, arch: str = "x86_64", editor: Path | None = None,
         info: bool = False) -> tuple[Path | None, str]:
    """Port a game, printing progress. Returns the output folder (None with info)
    and the warning about plugins that were not converted ("" if none)."""
    game_dir = game_dir.resolve()
    data_dir = version.find_data_dir(game_dir)
    uv = version.detect(game_dir)
    print(f"Unity version: {uv.version} (revision {uv.revision or 'unknown'})")
    if (game_dir / "GameAssembly.dll").exists():
        meta = _il2cpp_metadata_version(data_dir)
        raise SystemExit("IL2CPP games are not supported (GameAssembly.dll found"
                         + (f", IL2CPP metadata version {meta})" if meta else ")"))
    if info:
        return None, ""

    linux_bcl = bcl.linux_bcl(uv, editor) if bcl.needs_linux_bcl(data_dir / "Managed") else None
    variation = player.pick_variation(player.variations_dir(uv), arch)
    print(f"Using player {variation.name}")
    out_dir = (output or game_dir.with_name(game_dir.name + "-linux")).resolve()
    if out_dir == game_dir or game_dir in out_dir.parents:
        raise SystemExit("Output folder must be outside the game folder")
    # Building in place over a network file system (e.g. Windows drives from WSL, or WSL from
    # Windows) is very slow, as the shader pass makes many small reads and writes. Build
    # locally instead, then copy the result over in one go, which runs at normal speed.
    staged = wsl.remote_fs(out_dir)
    work_dir = Path(tempfile.mkdtemp(prefix="unityport-")) / out_dir.name if staged else out_dir
    try:
        out_data = assemble.assemble(game_dir, data_dir, variation, work_dir, arch)
        if linux_bcl:
            print(f"Class libraries: {bcl.install(out_data / 'Managed', linux_bcl)} replaced with Linux ones")
        settings.patch_for_opengl(out_data)
        print("Translating D3D11 shaders to OpenGL Core")
        shaders.convert_data_dir(out_data)
        if staged:
            print(f"Copying the build to {out_dir}")
            if out_dir.exists():
                shutil.rmtree(out_dir)
            shutil.copytree(work_dir, out_dir)
    finally:
        if staged:
            shutil.rmtree(work_dir.parent, ignore_errors=True)
    print(f"Linux build written to {out_dir}")
    name = version.game_name(game_dir, data_dir)
    exe = f"{name}.{arch}"
    if os.name == "nt" and not wsl.make_executable(out_dir / exe):
        print(f"Note: Windows can't mark {exe} as executable. After copying the build to Linux, run "
              f"chmod +x on it, or open its Properties and allow executing it as a program.")
    found = plugins.windows_plugins(data_dir)
    warning = plugins.report(found, f"{name}_Data", arch)
    if any(not p.note for p in found):
        # Where the warning says to put the replacements
        (out_dir / f"{name}_Data" / "Plugins" / arch).mkdir(parents=True, exist_ok=True)
    if warning:
        print("\n" + warning)
    return out_dir, warning


def _attach_console() -> None:
    """Give the Windows executable's output to the terminal it was run from.

    It is built without a console, so double-clicking it opens no terminal window, and
    then has nowhere to print to. The terminal doesn't wait for such a program, so its
    prompt can come back before the output ends; `start /wait unityport ...` waits."""
    if os.name != "nt" or sys.stdout is not None:
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        print()  # off the line with the prompt


def main() -> None:
    config.migrate()
    if len(sys.argv) == 1:
        from . import gui
        if gui.available():
            return gui.main()
    _attach_console()
    ap = argparse.ArgumentParser(
        prog="unityport", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Run without arguments to open the window.\n\n"
               f"unityport keeps its downloads and settings in {config.HOME}\n"
               f"(set UNITYPORT_HOME to move it). Settings saved in the window's Advanced\n"
               f"settings apply here too, unless overridden by the options above.\n\n"
               f"Project page: {report.REPO_URL}")
    ap.add_argument("game_dir", type=Path, help="Windows game folder (contains <Game>.exe and <Game>_Data)")
    ap.add_argument("-o", "--output", type=Path, metavar="DIR", help="output folder (default: <game_dir>-linux)")
    ap.add_argument("--arch", choices=config.ARCHES,
                    help="Linux player to use: x86_64 (default), or the 32-bit x86, which only older Unity "
                         "versions include")
    ap.add_argument("--editor", type=Path, metavar="DIR",
                    help="Unity editor install of the game's version, for 2021.2+ games, which need its Linux "
                         "class libraries. Without it, they are downloaded (about 600 MB, once per version)")
    ap.add_argument("--info", action="store_true",
                    help="only print the detected Unity version and build revision")
    ap.add_argument("--report", action="store_true",
                    help=f"print a problem report for {report.NEW_ISSUE_URL}, with the ported game's "
                         "Player.log, instead of porting")
    ap.add_argument("--download-url", metavar="URL",
                    help="where Unity's per-build downloads are, with {rev} for the build revision "
                         f"(default: {config.DEFAULTS['download_url']})")
    ap.add_argument("--releases-api", metavar="URL",
                    help="Unity's release lookup, used to find a build revision the game's files don't "
                         "contain, with {version} for the Unity version "
                         f"(default: {config.DEFAULTS['releases_api']})")
    ap.add_argument("--version", action="version", version=f"unityport {__version__}")
    args = ap.parse_args()
    for name in ("arch", "download_url", "releases_api"):
        if getattr(args, name):
            config.override(name, getattr(args, name))
    if args.report:
        game_dir = args.game_dir.resolve()
        output = (args.output or game_dir.with_name(game_dir.name + "-linux")).resolve()
        print(report.build("", game_dir, output), end="")
        return
    port(args.game_dir, args.output, config.get("arch"), args.editor, args.info)


if __name__ == "__main__":
    main()
