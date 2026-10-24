"""Patch build-wide settings in globalgamemanagers."""

import struct
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
        elif obj.type.name == "UnityConnectSettings" and env.file.unity_version.startswith("6000."):
            raw = _without_insights(obj.get_raw_data(), obj.read_typetree())
            if raw is not None:
                obj.set_raw_data(raw)
                print("UnityConnectSettings: removed InsightsSettings, which Linux players don't read")
    if not found:
        raise RuntimeError(f"No BuildSettings object in {path}")
    path.write_bytes(env.file.save())


def _without_insights(raw: bytes, tree: dict) -> bytes | None:
    """UnityConnectSettings without its InsightsSettings block, or None if it has none.

    Unity 6's Windows player writes InsightsSettings (engine diagnostics URLs) after
    m_TestInitMode, but the Linux player (6000.2.6) doesn't read it, so it takes those
    URLs for CrashReportingSettings and runs off the end ("globalgamemanagers is
    corrupted"). The edit is on the raw bytes: UnityPy's layout for the class includes it.
    """
    insights = tree.get("InsightsSettings")
    if insights is None:
        return None
    pos = 4  # m_Enabled, m_TestMode, aligned

    def skip_string(pos: int) -> tuple[int, bytes]:
        (n,) = struct.unpack_from("<I", raw, pos)
        return pos + 4 + n + (-n % 4), raw[pos + 4:pos + 4 + n]

    for _ in ("m_EventOldUrl", "m_EventUrl", "m_ConfigUrl", "m_DashboardUrl"):
        pos, _ = skip_string(pos)
    start = pos + 4  # after m_TestInitMode
    end, event_url = skip_string(start)
    end, stack_url = skip_string(end)
    end += 4  # m_EngineDiagnosticsEnabled, m_Enabled, aligned
    if (event_url.decode("utf-8", "replace"), stack_url.decode("utf-8", "replace")) != \
            (insights.get("m_EventUrl"), insights.get("m_StackTraceUrl")):
        raise ValueError("unexpected UnityConnectSettings layout")
    return raw[:start] + raw[end:]
