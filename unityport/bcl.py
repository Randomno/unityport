"""Linux class libraries (mscorlib, System, ...) for Unity 2021.2+ games.

From 2021.2 Unity builds Mono's class libraries per platform
(MonoBleedingEdge/lib/mono/unityjit-<platform> in the editor), and a Windows
game ships the win32 ones, which P/Invoke kernel32 for file access, so
System.Console and all file I/O fail on Linux. Player packages do not carry
them; only the editor does. They are copied from an editor install given with
--editor, or else streamed out of the Linux editor archive (the download stops
once they have been read, about 600 MB into the 3 GB archive), and cached.
"""

import shutil
import tarfile
import urllib.request
from pathlib import Path

from .player import BASE, CACHE
from .version import UnityVersion

PROFILE = Path("MonoBleedingEdge") / "lib" / "mono" / "unityjit-linux"
ARCHIVE_PROFILE = (Path("Editor") / "Data" / PROFILE).as_posix() + "/"
EDITOR_ARCHIVE = "LinuxEditorInstaller/Unity.tar.xz"
# Only in the win32 profile's mscorlib
WIN32_MARKER = b"SetThreadErrorMode"


def needs_linux_bcl(managed: Path) -> bool:
    try:
        return WIN32_MARKER in (managed / "mscorlib.dll").read_bytes()
    except OSError:
        return False


def _from_editor(editor: Path) -> Path:
    # The folder may be given at any level: <version>, Editor, Editor/Data, or Unity.app
    for sub in (Path("Editor") / "Data", Path("Data"), Path("."), Path("Unity.app") / "Contents", Path("Contents")):
        if (editor / sub / PROFILE / "mscorlib.dll").is_file():
            return editor / sub / PROFILE
    raise SystemExit(f"No {PROFILE.as_posix()} in {editor}")


def _download(uv: UnityVersion, dest: Path) -> None:
    if not uv.revision:
        raise SystemExit(f"Unknown build revision for Unity {uv.version}; pass its editor with --editor")
    url = BASE.format(rev=uv.revision) + EDITOR_ARCHIVE
    print(f"Downloading the Linux class libraries from {url}\n"
          "  (streamed until they have been read, typically about 600 MB)")
    dest.mkdir(parents=True)
    root = dest.resolve()
    found = False
    with urllib.request.urlopen(url, timeout=60) as r, tarfile.open(fileobj=r, mode="r|xz") as tf:
        for m in tf:
            name = m.name.removeprefix("./")
            if not name.startswith(ARCHIVE_PROFILE):
                if found:
                    break  # the profile's entries are contiguous; skip the rest of the archive
                continue
            found = True
            rel = name[len(ARCHIVE_PROFILE):]
            target = (dest / rel).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Bad path in editor archive: {m.name}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile() and not rel.endswith((".pdb", ".mdb")):
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    if not (dest / "mscorlib.dll").is_file():
        raise SystemExit(f"No {ARCHIVE_PROFILE} in {url}; pass the editor with --editor")


def linux_bcl(uv: UnityVersion, editor: Path | None = None) -> Path:
    """The unityjit-linux profile directory for a Unity version, cached on first use."""
    cached = CACHE / uv.version / "unityjit-linux"
    if (cached / "mscorlib.dll").is_file():
        return cached
    tmp = cached.with_name(cached.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    cached.parent.mkdir(parents=True, exist_ok=True)
    if editor:
        src = _from_editor(editor)
        print(f"Copying Linux class libraries from {src}")
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("*.pdb", "*.mdb"))
    else:
        _download(uv, tmp)
    tmp.rename(cached)
    return cached


def install(managed: Path, bcl: Path) -> int:
    """Replace the game's class libraries with the Linux ones. Returns the number replaced."""
    replaced = 0
    for dll in managed.glob("*.dll"):
        for src in (bcl / dll.name, bcl / "Facades" / dll.name):
            if src.is_file():
                shutil.copy2(src, dll)
                replaced += 1
                break
    return replaced
