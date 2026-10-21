# unityport

Tool for converting a Windows Unity game to native Linux. Built off the approach of [Unify](https://github.com/0xf4b1/unify) with added shader translation. LLM generated.

Mono only (no IL2CPP). Basic games from 2017.3.0f3, 2018.4.23f1, 2020.3.34f1 and 2021.3.45f1 were ported.

## Requirements

- Python 3.10+
- `7z` (p7zip / 7-Zip) to unpack Unity installers
- CMake and a C++11 compiler to build the translator
- Network access to `download.unity3d.com` for the first port of each Unity version

## Setup

```bash
git clone --recursive https://github.com/Randomno/unityport.git unityport
cd unityport
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build -j
python3 -m venv .venv
.venv/bin/pip install -e .
```

If you cloned without `--recursive`, run `git submodule update --init` first.

## Usage

```bash
.venv/bin/unityport "path/to/Game" -o "path/to/Game-linux"
"path/to/Game-linux/Game.x86_64"
```

`path/to/Game` is the Windows game folder containing `Game.exe` and `Game_Data`. Options:

- `--info`: print the detected Unity version and build revision, then exit
- `--arch x86`: use the 32-bit Linux player (only for Unity versions that still ship one)
- `--editor <path>`: a Unity editor install of the game's version, for 2021.2+ games (see below). Without it, the needed files are streamed from the Linux editor archive (about 600 MB of its 3 GB).

Downloaded Linux players and class libraries are cached in `~/.cache/unityport` (override with `UNITYPORT_CACHE`).

## How it works

1. **Version detection**: the Unity version comes from the serialized file headers (`globalgamemanagers`, `level0`, ...). The build revision hash comes from the `<version>_<hash>` string embedded in `UnityPlayer.dll` (or the game exe on older versions), with Unity's release API as a fallback.
2. **Linux player**: Unity publishes per-build manifests at `download_unity/<hash>/unity-<version>-{osx,win}.ini`. The "Linux Build Support (Mono)" macOS `.pkg` (or, as a fallback, the Windows NSIS `.exe`) is downloaded and unpacked with 7z. This is about 100–150 MB instead of a multi-GB editor download.
3. **Assembly**: the game data is copied, and the Windows runtime pieces (Mono, plugins, engine assemblies, `unity default resources`) are replaced with the player's Linux equivalents. The window icon is extracted from the exe into `Resources/UnityPlayer.png`. Compressed builds (`data.unity3d`) are unpacked into loose files, which makes the output larger than the original.
   From 2021.2 Mono's class libraries (`mscorlib`, `System`, ...) are platform-specific, and the Windows ones call kernel32 for file access. They are replaced with the editor's `unityjit-linux` profile, which official Linux builds ship unchanged; player packages don't include it.
4. **Settings**: `BuildSettings.m_GraphicsAPIs` is set to OpenGL Core, and `GraphicsSettings.m_ShaderDefinesPerShaderCompiler` gets an OpenGL Core entry.
5. **Shaders**: each `Shader` object's D3D11 blob is decompressed (raw LZ4 blocks), and every vertex/fragment program pair is translated to GLSL. A GLCore platform is then added in the layout that Unity's own GL compiler produces for that version.

Shader details that took some digging:

- **Unity strips the `RDEF` (reflection) chunk from its DXBC**, which made HLSLcc crash. unityport rebuilds `RDEF` from the reflection data Unity keeps elsewhere: per sub-program in the parsed form (2017–2019), or split between `m_CommonParameters` and a per-variant block after the code (2020).
- D3D11 compiles each stage separately and strips keywords the stage doesn't use, so vertex and fragment variant sets differ. A GL program is generated for each vertex × fragment pair that agrees on their shared keywords. This reproduces Unity's own GL variant set exactly.
- D3D11 bytecode has D3D clip-space conventions baked in (reversed Z, 0..1 depth), while the GL player supplies GL projection matrices. Code that reads clip-space z (shadow-caster bias, fog distance, outline shaders that scale by `pos.z`) breaks. Each translated vertex shader therefore gets D3D-style copies of the projection matrices, and `gl_Position.z` is converted back to GL at the end.
- 2021.2 moves the per-variant parameters into separate blob entries and splits large blobs into 16 MiB segments. GLCore programs of SRP shaders declare named cbuffers as std140 uniform blocks, while shaders using the built-in pipeline's includes (recognized by cbuffers such as `UnityPerFrame`) get plain uniforms, as in Unity's own GL builds. Unity's reflection lists only the cbuffer members a program uses, so the rebuilt RDEF fills the gaps to keep std140 offsets right.
- In 2020, GLCore programs carry a reflection block (per-stage uniform layout and texture bindings), and D3D11 programs must be removed or the player rejects the pass. unityport generates the block from the GLSL using Unity's packing rules.

## Limitations

- IL2CPP games (`GameAssembly.dll`) are not supported.
- Native Windows plugins (`Plugins/*.dll`) are reported but not replaced.
- Shader variants that use structured buffers or UAVs (for example particle procedural instancing) are skipped; they need SSBOs, which GL 3.2 lacks. Unity's own GL builds omit them as well.
- Geometry, hull and domain shader passes are skipped.
- Serialized files keep the Windows target-platform value in their headers; the Linux player doesn't seem to mind.

## Tools

- `tools/compare_gl.py <ported _Data> <official _Data> [-v]`: compare the GL shaders of a ported build against an official Linux build of the same project (variants, uniform layout, texture bindings, GLSL declarations).
- `tools/dumpshader.py <assets file> --list | "<shader name>" [--code]`: dump a shader's passes, sub-programs and blob entries.
- `tools/tintshaders.py <ported assets file> [name substring ...]`: make shaders draw flat, distinct colours (edits the file in place). Use it to find which shader causes a rendering glitch: screenshot the game, then look up the colour.

Run the port with `-logFile <path>` to get the player log; otherwise it goes to `~/.config/unity3d/<company>/<product>/Player.log`.

## Tests

```bash
.venv/bin/python -m unittest
```

The tests cover the shader blob format (including 2021.2 segments and parameter entries), the GLSL fix-ups and the class library swap. Fix-ups for D3D assumptions in translated GLSL go in `unityport/glsl.py`.