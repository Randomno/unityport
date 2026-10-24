# How unityport works

What a port does, step by step, and the format details behind it.

## Steps

1. **Version detection**: the Unity version comes from the serialized file headers (`globalgamemanagers`, `level0`, ...). The build revision hash comes from the `<version>_<hash>` string embedded in `UnityPlayer.dll` (or the game exe on older versions), with Unity's release API as a fallback.
2. **Linux player**: Unity publishes per-build manifests at `download_unity/<hash>/unity-<version>-{osx,win}.ini`. The "Linux Build Support (Mono)" macOS `.pkg` (or, as a fallback, the Windows NSIS `.exe`) is downloaded and unpacked with 7z. This is about 100–150 MB instead of a multi-GB editor download.
3. **Assembly**: the game data is copied, and the Windows runtime pieces (Mono, plugins, engine assemblies, `unity default resources`) are replaced with the player's Linux equivalents. The window icon is extracted from the exe into `Resources/UnityPlayer.png`. Compressed builds (`data.unity3d`) are unpacked into loose files, which makes the output larger than the original.
   From 2021.2 Mono's class libraries (`mscorlib`, `System`, ...) are platform-specific, and the Windows ones call kernel32 for file access. They are replaced with the editor's `unityjit-linux` profile, which official Linux builds ship unchanged; player packages don't include it.
4. **Settings**: `BuildSettings.m_GraphicsAPIs` is set to OpenGL Core, and `GraphicsSettings.m_ShaderDefinesPerShaderCompiler` gets an OpenGL Core entry. In Unity 6, `UnityConnectSettings` loses its `InsightsSettings` block: the Windows player writes it but the Linux player (6000.2) doesn't read it, and reports `globalgamemanagers` as corrupted. The Linux player's own field list came from its typetree generator, read from the disassembly with the player package's debug symbols.
5. **Shaders**: each `Shader` object's D3D11 blob is decompressed (raw LZ4 blocks), and every vertex/fragment program pair is translated to GLSL. A GLCore platform is then added in the layout that Unity's own GL compiler produces for that version.

## Shader details

Things that took some digging:

- **Unity strips the `RDEF` (reflection) chunk from its DXBC**, which made HLSLcc crash. unityport rebuilds `RDEF` from the reflection data Unity keeps elsewhere: per sub-program in the parsed form (2017–2019), or split between `m_CommonParameters` and a per-variant block after the code (2020).
- D3D11 compiles each stage separately and strips keywords the stage doesn't use, so vertex and fragment variant sets differ. A GL program is generated for each vertex × fragment pair that agrees on their shared keywords. This reproduces Unity's own GL variant set exactly.
- D3D11 bytecode has D3D clip-space conventions baked in (reversed Z, 0..1 depth), while the GL player supplies GL projection matrices. Code that reads clip-space z (shadow-caster bias, fog distance, outline shaders that scale by `pos.z`) breaks. Each translated vertex shader therefore gets D3D-style copies of the projection matrices, and `gl_Position.z` is converted back to GL at the end.
- 2021.2 moves the per-variant parameters into separate blob entries and splits large blobs into 16 MiB segments. GLCore programs of SRP shaders declare named cbuffers as std140 uniform blocks, while shaders using the built-in pipeline's includes (recognized by cbuffers such as `UnityPerFrame`) get plain uniforms, as in Unity's own GL builds. Unity's reflection lists only the cbuffer members a program uses, so the rebuilt RDEF fills the gaps to keep std140 offsets right.
- In 2020, GLCore programs carry a reflection block (per-stage uniform layout and texture bindings), and D3D11 programs must be removed or the player rejects the pass. unityport generates the block from the GLSL using Unity's packing rules.
- Each GL program targets the GLSL version Unity's own compiler picks from the program's shader requirements (150, 330, 410 or 420, which carry over unchanged from D3D11). D3D11 bytecode compiled for a high target uses functions such as `bitfieldInsert` that `#version 150` lacks.
- GLSL before 4.4 needs vertex outputs and fragment inputs to agree on `flat`, and both stages to declare a uniform block identically. The fragment shader is translated first, as HLSLcc expects, so the vertex shader takes its interpolation modes and, from GLSL 4.1 on, its varying locations from it (D3D11 links the stages by semantic, so the vertex shader may write outputs in another order, or more of them). Both stages get every member either stage uses from a shared block.
- Unity strips unused shaders to an empty D3D11 blob (or, in Unity 6, to no passes over leftover entries); those are relabelled GLCore, as in official builds, or the player warns about them. A pixel shader's random-write targets (`RWTexture2D`) aren't in Unity's reflection data at all, so they are read from the bytecode's declarations.
