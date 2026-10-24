"""Find the native Windows plugins a game ships, which unityport cannot convert."""

from dataclasses import dataclass
from pathlib import Path

# Plugins whose absence doesn't need fixing in the game's code
NOTES = {
    "lib_burst_generated": "Burst-compiled code; Burst normally falls back to the managed versions of "
                           "those methods when it is missing",
    "libonigwrap": "Oniguruma regular expressions for Unity's editor tooling, which Unity copies into "
                   "Windows builds but leaves out of Linux ones",
}
HINTS = {
    "steam_api": "libsteam_api.so from the Steamworks SDK",
    "steam_api64": "libsteam_api.so from the Steamworks SDK",
}


@dataclass
class Plugin:
    path: Path                 # relative to <Game>_Data
    used_by: list[str]         # game assemblies that name it, e.g. in [DllImport]
    note: str | None = None


def windows_plugins(data_dir: Path) -> list[Plugin]:
    plugins = data_dir / "Plugins"
    if not plugins.is_dir():
        return []
    assemblies = {}
    for dll in sorted((data_dir / "Managed").glob("*.dll")):
        try:
            assemblies[dll.name] = dll.read_bytes().lower()
        except OSError:
            pass
    found = []
    for dll in sorted(plugins.rglob("*.dll")):
        # The name is in the assembly's string heap, with or without ".dll"
        stem = dll.stem.lower().encode()
        used_by = [name for name, data in assemblies.items() if stem in data]
        found.append(Plugin(dll.relative_to(data_dir), used_by, NOTES.get(dll.stem.lower())))
    return found


def report(plugins: list[Plugin], data_name: str, arch: str) -> str:
    """A warning listing the plugins that were not converted, or "" if there are none."""
    lines = []
    todo = [p for p in plugins if not p.note]
    if todo:
        lines.append("Warning: these native Windows plugins were not converted, and the game will "
                     "fail where it calls them:")
        for p in todo:
            used = ("used by " + ", ".join(p.used_by)) if p.used_by else \
                "not named by any game assembly, so possibly unused"
            lines.append(f"  {p.path.name} ({used})")
            # Mono maps DllImport("foo") to libfoo.so, keeping the Windows name, so
            # e.g. steam_api64 needs libsteam_api64.so although Valve ships libsteam_api.so.
            # A name already starting with "lib" is also tried as is, plus ".so".
            hint = HINTS.get(p.path.stem.lower(), "a Linux build of it")
            so = p.path.stem if p.path.stem.lower().startswith("lib") else f"lib{p.path.stem}"
            lines.append(f"    Linux replacement: {hint}, saved as "
                         f"{data_name}/Plugins/{arch}/{so}.so")
        lines.append("For each one, find a Linux version of the plugin, or remove its use from the "
                     "game's code.")
    if todo and len(todo) < len(plugins):
        lines.append("")
    for p in plugins:
        if p.note:
            lines.append(f"Note: {p.path.name} was not converted ({p.note}).")
    return "\n".join(lines)
