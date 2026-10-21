import tempfile
import unittest
from pathlib import Path

from unityport import bcl, rdef
from unityport.shaders import gl_params_2021

GLSL = b"""#ifdef VERTEX
uniform \tvec4 hlslcc_mtx4x4unity_MatrixVP[4];
UNITY_BINDING(0) uniform UnityPerDraw {
\tvec4 hlslcc_mtx4x4unity_ObjectToWorld[4];
\tvec4 unityport_pad64[4];
\tvec4 unity_LODFade;
};
#endif
#ifdef FRAGMENT
uniform \tvec2 _GlobalMipBias;
UNITY_LOCATION(0) uniform  sampler2D _MainTex;
#endif
"""


def read_params(data: bytes):
    """[(group, size, {name: offset})], {binding: (kind, index)}"""
    rd = rdef._Reader(data)
    rd.u32()  # version
    groups = []
    for _ in range(rd.u32()):
        name, size = rd.string(), rd.u32()
        params = {}
        for _ in range(rd.u32()):
            pname = rd.string()
            params[pname] = [rd.u32() for _ in range(6)][-1]
        rd.u32()
        groups.append((name, size, params))
    bindings = {}
    for _ in range(rd.u32()):
        name = rd.string()
        kind, index, _ = rd.u32(), rd.u32(), rd.u32()
        if kind == rdef.BIND_TEXTURE:
            rd.u32()
        bindings[name] = (kind, index)
    return groups, bindings


class Params2021Test(unittest.TestCase):
    def setUp(self):
        self.groups, self.bindings = read_params(gl_params_2021(GLSL, 202012090, {"UnityPerDraw": 656}))

    def test_groups_fragment_first_then_blocks_after_their_stage(self):
        self.assertEqual([(g, size) for g, size, _ in self.groups],
                         [("", 0), ("$Globals", 8), ("$Globals", 64), ("UnityPerDraw", 656)])

    def test_block_members_at_std140_offsets_without_fillers(self):
        self.assertEqual(self.groups[3][2], {"unity_LODFade": 128, "unity_ObjectToWorld": 0})

    def test_bindings(self):
        self.assertEqual(self.bindings, {"_MainTex": (rdef.BIND_TEXTURE, 0), "UnityPerDraw": (rdef.BIND_CBUFFER, 0)})


class PaddingTest(unittest.TestCase):
    def test_gaps_filled_to_keep_offsets(self):
        f, v = rdef.SVT_FLOAT, rdef.SVC_VECTOR
        vs = [("a", 4, rdef.SVC_SCALAR, f, 1, 1, 0), ("b", 64, v, f, 1, 4, 0)]
        names = [(n, off, cols, elements) for n, off, _, _, _, cols, elements in rdef._padded(vs)]
        self.assertEqual(names, [("unityport_pad0", 0, 1, 0), ("a", 4, 1, 0), ("unityport_pad8", 8, 1, 0),
                                 ("unityport_pad12", 12, 1, 0), ("unityport_pad16", 16, 4, 3), ("b", 64, 4, 0)])


class ClassLibraryTest(unittest.TestCase):
    def test_detects_win32_profile_and_replaces_matching_assemblies(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed, profile = Path(tmp, "Managed"), Path(tmp, "unityjit-linux")
            (profile / "Facades").mkdir(parents=True)
            managed.mkdir()
            (managed / "mscorlib.dll").write_bytes(b"..SetThreadErrorMode..")
            (managed / "netstandard.dll").write_bytes(b"win")
            (managed / "Assembly-CSharp.dll").write_bytes(b"game")
            (profile / "mscorlib.dll").write_bytes(b"linux")
            (profile / "Facades" / "netstandard.dll").write_bytes(b"linux")
            self.assertTrue(bcl.needs_linux_bcl(managed))
            self.assertEqual(bcl.install(managed, profile), 2)
            self.assertFalse(bcl.needs_linux_bcl(managed))
            self.assertEqual((managed / "netstandard.dll").read_bytes(), b"linux")
            self.assertEqual((managed / "Assembly-CSharp.dll").read_bytes(), b"game")


if __name__ == "__main__":
    unittest.main()
