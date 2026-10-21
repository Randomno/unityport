import struct
import unittest

from unityport.shaders import BlobEntry, build_blob, build_segments, parse_blob, split_blob


def entry(version=201802150, local=None, code=b"abc"):
    return BlobEntry(version, 6, bytes(range(16)), ["DIRECTIONAL", "FOG_LINEAR"], local, code, b"\x03\0\0\0tail")


class BlobEntryTest(unittest.TestCase):
    def test_round_trip(self):
        e = entry()
        self.assertEqual(BlobEntry.parse(e.serialize()), e)

    def test_round_trip_with_local_keywords(self):
        # 2018.3 - 2021.1 entries carry a second (local) keyword list.
        e = entry(version=201806140, local=["_EMISSION"])
        self.assertEqual(BlobEntry.parse(e.serialize()), e)

    def test_strings_and_code_are_4_byte_aligned(self):
        data = entry(code=b"12345").serialize()
        # header (24) + count + "DIRECTIONAL" (4 + 12) + "FOG_LINEAR" (4 + 12) + code length + code padded to 8
        self.assertEqual(len(data), 24 + 4 + 16 + 16 + 4 + 8 + len(b"\x03\0\0\0tail"))

    def test_dxbc_extracted_by_container_size(self):
        dxbc = b"DXBC" + b"\0" * 20 + struct.pack("<I", 32) + b"\0" * 4
        e = entry(code=b"prefix" + dxbc + b"trailing")
        self.assertEqual(e.dxbc(), dxbc)


class BlobTest(unittest.TestCase):
    def test_round_trip_both_record_sizes(self):
        entries = [entry(), entry(code=b"")]
        for record in (8, 12):  # 12 = with segment index, 2019.3+
            parsed, rec = parse_blob(build_blob(entries, record))
            self.assertEqual(rec, record)
            self.assertEqual(parsed, entries)

    def test_large_blob_split_into_segments(self):
        # 2021.2+: index alone in segment 0, entries packed into the next segments
        entries = [bytes([i]) * 40 for i in range(5)]
        segments = build_segments(entries, 12, max_size=100)
        self.assertEqual(len(segments[0]), 4 + 12 * 5)
        self.assertEqual([len(s) for s in segments[1:]], [80, 80, 40])
        self.assertEqual(split_blob(segments), (entries, 12))

    def test_small_blob_stays_in_one_segment(self):
        entries = [b"x" * 8, b"y" * 8]
        (segment,) = build_segments(entries, 12, max_size=100)
        self.assertEqual(split_blob(segment), (entries, 12))

    def test_empty_blob(self):
        self.assertEqual(parse_blob(struct.pack("<I", 0)), ([], 8))


if __name__ == "__main__":
    unittest.main()
