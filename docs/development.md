# Developing unityport

Building the standalone executables, making releases, the debugging tools and the tests. For setting up a source checkout, see [Running from source](../README.md#running-from-source) in the README; for what a port does, see [How it works](how-it-works.md).

## Standalone executables

`tools/bundle.py` builds a single file containing Python, the dependencies, the shader translator and 7-Zip: `dist/unityport` on Linux (about 40 MB) or `dist\unityport.exe` on Windows (about 24 MB). It builds for the system it runs on. Without arguments the executable opens the window, and with arguments it is the command line.

On Linux:

```bash
sudo apt install python3-tk
.venv/bin/python tools/bundle.py --python /usr/bin/python3
```

The Python passed with `--python` needs tkinter. Use the distro's Python and tkinter package, whose Tk draws antialiased text with Xft. uv's Python builds include a Tk without Xft, so the window's text would look jagged; `bundle.py` warns if that happens. On Linux the window uses the desktop's folder picker (kdialog or zenity) when one is installed. The executable needs a glibc at least as new as the build machine's (2.35 on Ubuntu 22.04, which the release builds use), so build on the oldest distro you want to support.

On Windows, with a python.org Python (which includes tkinter), CMake and Visual Studio's C++ tools:

```bat
git clone --recursive https://github.com/Randomno/unityport.git unityport
cd unityport
py tools\bundle.py
```

The Windows executable is built without a console, so double-clicking it opens no terminal window. Run from a terminal with arguments, it attaches to that terminal to print, but the terminal doesn't wait for it: use `start /wait unityport.exe ...` to keep the prompt until it finishes.

A single-file executable unpacks itself to a temp folder on every launch, which takes about 2 seconds on Windows. `--folder` builds `dist/unityport/` instead, a folder holding the executable and its files, which starts in under a second.

The build uses `build/bundle` for its venv, PyInstaller and the translator, which is built with a static C++ runtime.

## Releases

`.github/workflows/release.yml` builds both executables on GitHub's runners: Linux on Ubuntu 22.04, Windows on the latest Windows image. It runs the tests first.

- **Pushing a tag** such as `v1.1` builds both and publishes them as a GitHub release for that tag, with generated release notes. The tag must match the version in `pyproject.toml` and `unityport/__init__.py`, or the build stops.
- **Running it by hand** (Actions > Release > Run workflow) builds both without releasing; the executables are attached to the run as artifacts.

To release:

```bash
# after setting the new version in pyproject.toml and unityport/__init__.py
git commit -am "Version 1.1"
git tag v1.1
git push origin main v1.1
```

## Tools

- `tools/compare_gl.py <ported _Data> <official _Data> [-v]`: compare the GL shaders of a ported build against an official Linux build of the same project (variants, uniform layout, texture bindings, GLSL declarations).
- `tools/dumpshader.py <assets file> --list | "<shader name>" [--code]`: dump a shader's passes, sub-programs and blob entries.
- `tools/tintshaders.py <ported assets file> [name substring ...]`: make shaders draw flat, distinct colours (edits the file in place). Use it to find which shader causes a rendering glitch: screenshot the game, then look up the colour.

Run a ported game with `-logFile <path>` to get the player log somewhere else; otherwise it goes to `~/.config/unity3d/<company>/<product>/Player.log`.

## Tests

```bash
.venv/bin/python -m unittest
```

The tests cover the shader blob format (including 2021.2 segments and parameter entries), the GLSL fix-ups, the class library swap, settings patches, plugin reports and problem reports. Fix-ups for D3D assumptions in translated GLSL go in `unityport/glsl.py`.
