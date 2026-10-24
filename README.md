# unityport

Tool for converting a Windows Unity game to native Linux. Built off the approach of [Unify](https://github.com/0xf4b1/unify) with added shader translation and a GUI. Created by LLM and cleaned up by human.

Only Mono games are supported (no IL2CPP), see [Limitations](#limitations). Various games between Unity 2018 and 6 (2024) were successfully ported.

## Download

Get `unityport` (Linux) or `unityport.exe` (Windows) from the [latest release](https://github.com/Randomno/unityport/releases/latest). On Linux, mark it as executable first (`chmod +x unityport`). Linux needs glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36 or later).

The first port of each Unity version downloads Unity's Linux player from `download.unity3d.com` (about 100–150 MB, more for 2021.2 and newer).

## Usage

Choose the Windows game folder (the one containing `Game.exe` and `Game_Data`), and optionally an output folder, then **Port**. Drag and drop is supported.

Or use the command line:

```bash
unityport "path/to/Game" -o "path/to/Game-linux"
```

`unityport --help` lists the options.

Native Windows plugins (`Plugins/*.dll`) can't be converted. unityport lists any that were found, and which game files use them. Each needs a Linux version of the plugin, or its use removed from the game's code. For example, Steam games using Steamworks.NET need `libsteam_api.so` from the Steamworks SDK, saved under the name the list gives. `lib_burst_generated.dll` is only noted, since Burst normally falls back to the managed code.

unityport keeps its downloads (Linux players and class libraries) and settings in `~/.local/share/unityport` on Linux and `%LOCALAPPDATA%\unityport` on Windows. Set `UNITYPORT_HOME` to use another folder.

Under WSL, **Browse...** offers both Windows and Linux folders, and paths copied from the other side are converted, so `C:\Games\Game` works in the Linux build and `/home/me/Game` in the Windows one.

## Limitations

- IL2CPP games (`GameAssembly.dll`) are not supported.
- Native Windows plugins (`Plugins/*.dll`) are reported but not replaced (see [Usage](#usage)).
- Shader variants that use structured buffers (for example particle procedural instancing) are skipped; they need SSBOs, which GL 3.2 lacks. Unity's own GL builds omit them as well.
- Shaders that Unity adds only to GL builds (such as `Hidden/Core/FallbackError` in 2022) are missing, so the player may log that a fallback shader wasn't found.
- Geometry, hull and domain shader passes are skipped.
- Serialized files keep the Windows target-platform value in their headers; the Linux player doesn't seem to mind.

## Reporting problems

In the window, **Help > Report a problem** fills in an issue with the port's output and its `Player.log`. Check it, then post it with **Copy and open GitHub**. Or [open an issue](https://github.com/Randomno/unityport/issues/new?template=port-problem.md) and fill in the template.

The game's log is in `~/.config/unity3d/<company>/<game>/Player.log`.

## Running from source

Requirements:

- Python 3.10+
- `7z` (p7zip / 7-Zip) to unpack Unity installers. On Windows, a 7-Zip install in its default folder also works.
- CMake and a C++11 compiler to build the translator
- For the window: tkinter (`python3-tk` on Debian/Ubuntu)

```bash
git clone --recursive https://github.com/Randomno/unityport.git unityport
cd unityport
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build -j
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/unityport
```

If you cloned without `--recursive`, run `git submodule update --init` first. CMake applies the fixes in `native/patches` to the HLSLcc checkout when it configures (this needs `git`).