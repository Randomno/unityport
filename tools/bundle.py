#!/usr/bin/env python3
"""Build dist/unityport (dist\\unityport.exe on Windows), a single executable for end users.

It carries Python, unityport's dependencies, the shader translator and 7-Zip,
so users need nothing installed. Run without arguments it opens the window;
with arguments it is the usual command line. It is built for the platform
this runs on: run it on Windows for the Windows executable.

Usage: tools/bundle.py [--python <python with tkinter>]

The Python used must have tkinter. On Linux use the distro's Python with its
tkinter package (python3-tk on Debian/Ubuntu): its Tk draws antialiased text
with Xft. uv's Python builds include a Tk without Xft, whose fonts look
jagged. The python.org Windows installer includes tkinter by default.

Linux: the executable needs a glibc at least as new as the build machine's,
so build on the oldest distro you want to support.
Windows: needs CMake and Visual Studio's C++ compiler for the translator.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "bundle"
DIST = ROOT / "dist"

WINDOWS = os.name == "nt"
SEVEN_ZIP_RELEASE = "https://github.com/ip7z/7zip/releases/download/24.09/"
SEVEN_ZIP_SHA256 = {
    # Statically linked Linux build
    "7z2409-linux-x64.tar.xz": "914c7e20ad5ef8e4d3cf08620ff8894b28fe11b7eb99809d6930870fbe48a281",
    # Windows installer (a 7z archive holding 7z.exe and 7z.dll), and the
    # 7z-only 7zr.exe to unpack it
    "7z2409-x64.exe": "bdd1a33de78618d16ee4ce148b849932c05d0015491c34887846d431d29f308e",
    "7zr.exe": "d2c0045523cf053a6b43f9315e9672fc2535f06aeadd4ffa53c729cd8b2b6dfe",
}
PYINSTALLER = "pyinstaller==6.22.3"

ENTRY = """from unityport.__main__ import main
main()
"""


def run(*args, **kw):
    print("+", " ".join(map(str, args)))
    subprocess.run(list(map(str, args)), check=True, **kw)


def build_translator() -> Path:
    """hlslcc_tool with the C++ runtime linked in, so it runs without a matching libstdc++
    (or, on Windows, without the Visual C++ redistributable)."""
    out = BUILD / "native"
    if WINDOWS:
        run("cmake", "-S", ROOT / "native", "-B", out, "-DCMAKE_POLICY_DEFAULT_CMP0091=NEW",
            "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded")
        run("cmake", "--build", out, "--config", "Release", "--target", "hlslcc_tool")
        return next(p for p in (out / "Release" / "hlslcc_tool.exe", out / "hlslcc_tool.exe") if p.exists())
    run("cmake", "-S", ROOT / "native", "-B", out, "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc")
    run("cmake", "--build", out, "--target", "hlslcc_tool", "-j")
    run("strip", out / "hlslcc_tool")
    return out / "hlslcc_tool"


def download(name: str) -> Path:
    path = BUILD / name
    if not path.exists():
        url = SEVEN_ZIP_RELEASE + name
        print(f"Downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != SEVEN_ZIP_SHA256[name]:
            raise SystemExit(f"Checksum mismatch for {url}")
        path.write_bytes(data)
    return path


def fetch_7zip() -> list[Path]:
    """7-Zip's program files and its licence, to bundle."""
    out = BUILD / "7zip"
    out.mkdir(exist_ok=True)
    if WINDOWS:
        installer, sevenzr = download("7z2409-x64.exe"), download("7zr.exe")
        names = ["7z.exe", "7z.dll", "License.txt"]
        run(sevenzr, "x", "-y", f"-o{out}", installer, *names, stdout=subprocess.DEVNULL)
    else:
        names = ["7zz", "License.txt"]
        with tarfile.open(download("7z2409-linux-x64.tar.xz")) as tf:
            # 7zzs is the statically linked one
            for name, dest in (("7zzs", "7zz"), ("License.txt", "License.txt")):
                with tf.extractfile(name) as src, open(out / dest, "wb") as f:
                    shutil.copyfileobj(src, f)
        (out / "7zz").chmod(0o755)
    licence = out / "7zip-License.txt"
    (out / "License.txt").replace(licence)
    return [out / n for n in names[:-1]] + [licence]


def make_venv(python: str) -> Path:
    venv = BUILD / "venv"
    py = venv / ("Scripts/python.exe" if WINDOWS else "bin/python")
    run(python, "-m", "venv", "--clear", venv)  # fresh, in case --python changed
    if subprocess.run([py, "-c", "import tkinter; tkinter.Tcl()"], capture_output=True).returncode:
        raise SystemExit(f"{python} has no working tkinter; see `{Path(__file__).name} --help`")
    if not WINDOWS:
        fonts = subprocess.run([py, "-c", "import tkinter; r = tkinter.Tk(); r.withdraw(); "
                                          "print(r.tk.call('::tk::pkgconfig', 'get', 'fontsystem'))"],
                               capture_output=True, text=True).stdout.strip()
        if fonts != "xft":
            print(f"Warning: {python}'s Tk was built without Xft, so the window's text will look jagged; "
                  f"see `{Path(__file__).name} --help`")
    run(py, "-m", "pip", "install", "-q", "--upgrade", ROOT, PYINSTALLER)
    return py


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=sys.executable, help="Python to build with (default: this one)")
    ap.add_argument("--folder", action="store_true",
                    help="Build dist/unityport/, a folder holding the executable and its files, instead of one "
                         "executable: it starts much faster, as it has nothing to unpack first")
    args = ap.parse_args()

    BUILD.mkdir(parents=True, exist_ok=True)
    tool = build_translator()
    seven_zip = fetch_7zip()
    py = make_venv(args.python)
    entry = BUILD / "unityport_entry.py"
    entry.write_text(ENTRY)
    files = [arg for f in [tool] + seven_zip for arg in ("--add-binary", f"{f}{os.pathsep}.")]
    # Without a console, so that double-clicking it opens no terminal window (which a
    # console program gets before it can hide it). Run from a terminal with arguments, it
    # attaches to that terminal to print (unityport.__main__._attach_console).
    extra = ["--windowed"] if WINDOWS else []
    layout = "--onedir" if args.folder else "--onefile"
    run(py, "-m", "PyInstaller", "--noconfirm", "--clean", layout, "--name", "unityport",
        "--distpath", DIST, "--workpath", BUILD / "pyinstaller", "--specpath", BUILD,
        *files, *extra, "--collect-all", "UnityPy", "--collect-all", "tkinterdnd2", "--collect-data", "archspec",
        "--hidden-import", "tkinter", entry)
    exe = "unityport.exe" if WINDOWS else "unityport"
    print(f"Built {DIST / 'unityport' / exe if args.folder else DIST / exe}")


if __name__ == "__main__":
    main()
