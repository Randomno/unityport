"""Fix-ups applied to HLSLcc's GLSL output before it goes into the Shader.

HLSLcc translates the D3D11 bytecode faithfully, including D3D-only
assumptions that Unity's compiler had baked in. Corrections for those live
here, as text transforms of the combined "#ifdef VERTEX ... #ifdef FRAGMENT"
source that hlslcc_tool writes.

They depend on the shape of HLSLcc's output (`void main()` followed by `{` on
its own line, `return;` statements). If that ever changes they raise
ValueError, so the variant shows up as failed instead of rendering wrongly.
"""

import re

# --- Clip-space conventions --------------------------------------------------
#
# The D3D11 bytecode was compiled with UNITY_REVERSED_Z and a 0..1 clip depth
# baked in (shadow caster bias, fog distance, outline shaders that scale by
# clip z...), but the GL player supplies GL projection matrices (-1..1, not
# reversed). So each vertex shader gets D3D-style copies of the projection
# matrices, z_d3d = (w - z_gl) / 2, and converts gl_Position back to GL at
# every return, z_gl = w - 2 z_d3d. Fragment shaders and depth textures stay
# GL; _ZBufferParams etc. come from the engine and match them.

PROJECTION_MATRICES = ("unity_MatrixVP", "glstate_matrix_projection", "glstate_matrix_mvp", "unity_MatrixMVP")
_MAIN = "void main()\n{\n"
_RETURN = re.compile(r"^([ \t]*)return;$", re.M)


def d3d_clip_space(glsl: bytes) -> bytes:
    text = glsl.decode()
    split = text.find("#ifdef FRAGMENT")
    vs, rest = (text[:split], text[split:]) if split >= 0 else (text, "")
    if "gl_Position" not in vs:
        return glsl
    start = vs.find(_MAIN)
    if start < 0:
        raise ValueError("clip-space fix: no 'void main()' in vertex shader")
    body = vs[start + len(_MAIN):]
    if not _RETURN.search(body):
        raise ValueError("clip-space fix: no return statement in vertex shader")

    prologue = []
    for name in PROJECTION_MATRICES:
        src = f"hlslcc_mtx4x4{name}"
        if src + "[" not in body:
            continue
        dst = f"d3d_mtx4x4{name}"
        prologue.append(f"    vec4 {dst}[4];\n")
        prologue += [f"    {dst}[{i}] = vec4({src}[{i}].xy, ({src}[{i}].w - {src}[{i}].z) * 0.5, {src}[{i}].w);\n"
                     for i in range(4)]
        body = body.replace(src + "[", dst + "[")
    if re.search(r"\bunity_LightShadowBias\b", body):
        # The engine passes the depth bias with GL's sign; reversed-Z code expects it negated.
        prologue.append("    vec4 d3d_LightShadowBias = unity_LightShadowBias * vec4(-1.0, 1.0, 1.0, 1.0);\n")
        body = re.sub(r"\bunity_LightShadowBias\b", "d3d_LightShadowBias", body)
    body = _RETURN.sub(r"\1gl_Position.z = gl_Position.w - 2.0 * gl_Position.z;\n\1return;", body)
    return (vs[:start] + _MAIN + "".join(prologue) + body + rest).encode()


def apply_fixups(glsl: bytes) -> bytes:
    """All fix-ups, in order. Raises ValueError if one can't be applied."""
    return d3d_clip_space(glsl)
