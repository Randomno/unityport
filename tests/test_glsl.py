import unittest

from unityport.glsl import d3d_clip_space

# Trimmed from HLSLcc's output for Bug Fables' "Outlined/Silhouette Only" outline pass.
OUTLINE = b"""#ifdef VERTEX
#version 150
uniform \tfloat GlobalOutline;
uniform \tvec4 unity_LightShadowBias;
uniform \tvec4 hlslcc_mtx4x4unity_ObjectToWorld[4];
uniform \tvec4 hlslcc_mtx4x4glstate_matrix_projection[4];
uniform \tvec4 hlslcc_mtx4x4unity_MatrixVP[4];
in  vec4 in_POSITION0;
vec4 u_xlat0;
vec4 u_xlat1;
void main()
{
    u_xlat0.xy = hlslcc_mtx4x4glstate_matrix_projection[0].xy * vec2(GlobalOutline);
    u_xlat1 = hlslcc_mtx4x4unity_MatrixVP[0] * in_POSITION0.xxxx;
    u_xlat1.z += unity_LightShadowBias.x;
    if (u_xlat1.w < 0.0) {
        gl_Position = u_xlat1;
        return;
    }
    gl_Position.xy = u_xlat0.xy * u_xlat1.zz + u_xlat1.xy;
    gl_Position.zw = u_xlat1.zw;
    return;
}

#endif
#ifdef FRAGMENT
#version 150
uniform \tvec4 hlslcc_mtx4x4unity_MatrixVP[4];
layout(location = 0) out vec4 SV_Target0;
void main()
{
    SV_Target0 = hlslcc_mtx4x4unity_MatrixVP[0];
    return;
}

#endif
"""


class ClipSpaceTest(unittest.TestCase):
    def setUp(self):
        self.out = d3d_clip_space(OUTLINE).decode()
        self.vs, self.fs = self.out.split("#ifdef FRAGMENT")

    def test_projection_matrices_converted_to_reversed_z(self):
        for name in ("unity_MatrixVP", "glstate_matrix_projection"):
            self.assertIn(f"vec4 d3d_mtx4x4{name}[4];", self.vs)
            self.assertIn(f"d3d_mtx4x4{name}[2] = vec4(hlslcc_mtx4x4{name}[2].xy, "
                          f"(hlslcc_mtx4x4{name}[2].w - hlslcc_mtx4x4{name}[2].z) * 0.5, "
                          f"hlslcc_mtx4x4{name}[2].w);", self.vs)
        body = self.vs.split("void main()")[1]
        self.assertIn("d3d_mtx4x4unity_MatrixVP[0] * in_POSITION0", body)
        # The only remaining references are in the prologue: 4 rows x 4 components read.
        self.assertEqual(body.count("hlslcc_mtx4x4unity_MatrixVP["), 4 * 4)

    def test_uniform_declarations_untouched(self):
        self.assertIn("uniform \tvec4 hlslcc_mtx4x4unity_MatrixVP[4];", self.vs)
        self.assertIn("uniform \tvec4 unity_LightShadowBias;", self.vs)

    def test_shadow_bias_negated(self):
        self.assertIn("vec4 d3d_LightShadowBias = unity_LightShadowBias * vec4(-1.0, 1.0, 1.0, 1.0);", self.vs)
        self.assertIn("u_xlat1.z += d3d_LightShadowBias.x;", self.vs)

    def test_every_return_converts_back_to_gl(self):
        self.assertIn("        gl_Position.z = gl_Position.w - 2.0 * gl_Position.z;\n        return;", self.vs)
        self.assertIn("    gl_Position.z = gl_Position.w - 2.0 * gl_Position.z;\n    return;", self.vs)
        self.assertEqual(self.vs.count("gl_Position.z = gl_Position.w"), self.vs.count("return;"))

    def test_fragment_stage_untouched(self):
        self.assertEqual(self.fs, OUTLINE.decode().split("#ifdef FRAGMENT")[1])

    def test_conversion_round_trips(self):
        # z_d3d = (w - z_gl) / 2 in the matrices, z_gl = w - 2 z_d3d at the end.
        for z, w in ((-1.0, 1.0), (0.3, 2.0), (5.0, 5.0)):
            self.assertAlmostEqual(w - 2.0 * ((w - z) * 0.5), z)

    def test_shader_without_gl_position_unchanged(self):
        src = b"#ifdef VERTEX\nvoid main()\n{\n    return;\n}\n#endif\n"
        self.assertEqual(d3d_clip_space(src), src)

    def test_unexpected_format_fails_loudly(self):
        with self.assertRaises(ValueError):
            d3d_clip_space(b"#ifdef VERTEX\nvoid main() {\n    gl_Position = vec4(0.0);\n}\n#endif\n")
        with self.assertRaises(ValueError):
            d3d_clip_space(b"#ifdef VERTEX\nvoid main()\n{\n    gl_Position = vec4(0.0);\n}\n#endif\n")


if __name__ == "__main__":
    unittest.main()
