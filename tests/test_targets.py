import struct
import unittest

from unityport import rdef, shaders


class GlTargetTest(unittest.TestCase):
    def test_versions_seen_in_official_builds(self):
        # ShaderRequirements -> GLSL version in Unity's 2020.3 and 2022.3 Linux builds
        for req, version in ((0x1, 150), (0x21, 150), (0xe3, 150), (0xeeb, 330), (0xfeb, 330),
                             (0x200feb, 330), (0x1eeb, 410), (0x1feb, 410), (0x4021, 420),
                             (0x40e3, 420), (0x10cfeb, 420)):
            self.assertEqual(shaders.gl_target(req)[0], version, hex(req))
        self.assertEqual(shaders.gl_target(0x10cfeb)[1], shaders.GLCORE43)
        self.assertEqual(shaders.gl_target(0xfeb)[1], shaders.GLCORE32)


class SharedBlocksTest(unittest.TestCase):
    def test_both_stages_get_all_members(self):
        def refl(**vars):
            r = rdef.Reflection()
            r.cbuffers["UnityPerDraw"] = rdef.CBuffer(16 * len(vars), {
                n: rdef.Var(o, 0, 1, 4, False, 0) for n, o in vars.items()})
            r.cbuffers["$Globals"] = rdef.CBuffer(16, {"_Color": rdef.Var(0, 0, 1, 4, False, 0)})
            r.cb_slots = {"UnityPerDraw": 1, "$Globals": 0}
            return r
        v, f = refl(unity_ObjectToWorld=0), refl(unity_LODFade=160)
        v2, f2, key = shaders._share_blocks(v, f)
        for r in (v2, f2):
            self.assertEqual(set(r.cbuffers["UnityPerDraw"].vars), {"unity_ObjectToWorld", "unity_LODFade"})
        self.assertEqual(key, (("UnityPerDraw", ("unity_LODFade", "unity_ObjectToWorld")),))
        self.assertEqual(set(v.cbuffers["UnityPerDraw"].vars), {"unity_ObjectToWorld"})  # inputs untouched


class TypedUavTest(unittest.TestCase):
    def test_finds_rwtexture2d_declaration(self):
        # dcl_uav_typed_texture2d (float,float,float,float) u1, then ret
        dcl = struct.pack("<4I", 0x9C | (3 << 11) | (4 << 24), 0x0010E01E, 1, 0x5555)
        ret = struct.pack("<I", 0x3E | (1 << 24))
        code = struct.pack("<2I", 0x50, 2 + (len(dcl) + len(ret)) // 4) + dcl + ret
        dxbc = b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, 0, 1) + struct.pack("<I", 36) + \
            b"SHEX" + struct.pack("<I", len(code)) + code
        self.assertEqual(rdef.typed_uavs(dxbc), {1: (4, 5)})


if __name__ == "__main__":
    unittest.main()


class RelabelEmptyTest(unittest.TestCase):
    def test_passless_shader_with_leftover_entries(self):
        tree = {"m_ParsedForm": {"m_SubShaders": [{"m_Passes": []}]}, "platforms": [], "offsets": [[]],
                "compressedLengths": [], "decompressedLengths": [], "compressedBlob": [], "stageCounts": [2]}
        # offsets [[]] marks the nested (2019.3+) layout
        shaders.set_platforms(tree, [(shaders.PLATFORM_D3D11, shaders.build_segments([b"x" * 52, b"y" * 8], 12))])
        self.assertTrue(shaders.relabel_empty(tree))
        self.assertEqual(tree["platforms"], [shaders.PLATFORM_GLCORE])
        self.assertEqual(shaders.split_blob(shaders.platform_segments(tree, shaders.PLATFORM_GLCORE))[0], [])


class InsightsTest(unittest.TestCase):
    def test_removes_insights_block(self):
        from unityport import settings

        def s(text):
            b = text.encode()
            return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)
        head = b"\x01\x00\x00\x00" + s("old") + s("event") + s("config") + s("dash") + struct.pack("<i", 0)
        insights = s("https://engine-data") + s("https://stack") + b"\x01\x00\x00\x00"
        tail = s("https://perf") + b"\x00\x00\x00\x00" + struct.pack("<I", 10)
        tree = {"InsightsSettings": {"m_EventUrl": "https://engine-data", "m_StackTraceUrl": "https://stack"}}
        self.assertEqual(settings._without_insights(head + insights + tail, tree), head + tail)
        self.assertIsNone(settings._without_insights(head + tail, {}))
