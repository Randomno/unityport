"""Patch build-wide settings in globalgamemanagers."""

from pathlib import Path

import UnityPy

# GfxDeviceRenderer values stored in BuildSettings.m_GraphicsAPIs
GRAPHICS_APIS = {2: "Direct3D 11", 8: "OpenGL ES 2", 11: "OpenGL ES 3", 16: "Metal",
                 17: "OpenGL Core", 18: "Direct3D 12", 21: "Vulkan"}
OPENGL_CORE = 17

# ShaderCompilerPlatform
PLATFORM_D3D11 = 4
PLATFORM_GLCORE = 15


def patch_for_opengl(data_dir: Path) -> None:
    """Make the player use OpenGL Core.

    BuildSettings.m_GraphicsAPIs lists the renderers to try; without a Linux
    one the player exits with "No supported renderers found".
    GraphicsSettings.m_ShaderDefinesPerShaderCompiler (2019+) holds the
    built-in shader defines per tier for each shader platform; official
    Linux builds carry the same values for GLCore as Windows ones do for D3D11.
    """
    path = data_dir / "globalgamemanagers"
    if not path.exists():  # Unity 5.x and older keep this in mainData
        path = data_dir / "mainData"
    env = UnityPy.load(str(path))
    found = False
    for obj in env.objects:
        if obj.type.name == "BuildSettings":
            found = True
            tree = obj.read_typetree()
            old = tree.get("m_GraphicsAPIs")
            if old is None:
                print("BuildSettings has no m_GraphicsAPIs; leaving renderer selection to the player")
                continue
            print("Graphics APIs: {} -> {}".format(
                [GRAPHICS_APIS.get(a, a) for a in old], [GRAPHICS_APIS.get(OPENGL_CORE)]))
            tree["m_GraphicsAPIs"] = [OPENGL_CORE]
            obj.save_typetree(tree)
        elif obj.type.name == "GraphicsSettings":
            tree = obj.read_typetree()
            defines = tree.get("m_ShaderDefinesPerShaderCompiler")
            if not defines or any(d["shaderPlatform"] == PLATFORM_GLCORE for d in defines):
                continue
            d3d = next((d for d in defines if d["shaderPlatform"] == PLATFORM_D3D11), None)
            if d3d is None:
                continue
            defines.append(dict(d3d, shaderPlatform=PLATFORM_GLCORE))
            print("Shader tier defines: copied D3D11 entry for OpenGL Core")
            obj.save_typetree(tree)
    if not found:
        raise RuntimeError(f"No BuildSettings object in {path}")
    path.write_bytes(env.file.save())
