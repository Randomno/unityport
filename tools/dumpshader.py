"""Dump Shader objects from a Unity assets file, for reverse engineering.

Usage:
  python tools/dumpshader.py <assets file> --list
  python tools/dumpshader.py <assets file> "<shader name>" [--code]

Prints each pass's sub-programs (parsed form) and, per platform blob, the
entries' headers and parameter blocks; --code also prints GLSL source.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import UnityPy  # noqa: E402

from unityport import shaders as sh  # noqa: E402


def main():
    path, name = sys.argv[1], sys.argv[2]
    show_code = "--code" in sys.argv
    env = UnityPy.load(path)
    for obj in env.objects:
        if obj.type.name != "Shader":
            continue
        tree = obj.read_typetree()
        parsed = tree["m_ParsedForm"]
        if name == "--list":
            print(parsed["m_Name"], tree.get("platforms"))
            continue
        if parsed["m_Name"] != name:
            continue
        for key, value in tree.items():
            if key not in ("m_ParsedForm", "compressedBlob"):
                print(f"{key}: {str(value)[:300]}")
        for si, ss in enumerate(parsed["m_SubShaders"]):
            for pi, ps in enumerate(ss["m_Passes"]):
                print(f"== subshader {si} pass {pi}: platforms {ps.get('m_Platforms')}, "
                      f"program mask {ps.get('m_ProgramMask')}")
                print(f"   names {ps['m_NameIndices']}")
                for prog in ("progVertex", "progFragment"):
                    if ps[prog].get("m_CommonParameters"):
                        print(f"   {prog} common: {json.dumps(ps[prog]['m_CommonParameters'])[:1000]}")
                    for sub in sh.subprograms(ps[prog]):
                        params = "" if sub.params is None else f" tier {sub.tier} parameters {sub.params}"
                        print(f"   {prog}: {json.dumps(sub.sp)[:1000]}{params}")
        for platform in tree.get("platforms") or []:
            raw, record = sh.split_blob(sh.platform_segments(tree, platform))
            print(f"== platform {platform} blob: {len(raw)} entries, {record}-byte index records")
            programs = {sub.sp["m_BlobIndex"] for ps in sh._passes(tree["m_ParsedForm"])
                        for prog in ("progVertex", "progFragment") for sub in sh.subprograms(ps[prog])
                        if sub.params is not None}
            for i, data in enumerate(raw):
                if programs and i not in programs:  # 2021.2+ parameter entry
                    print(f"-- entry {i}: parameters {data.hex(' ', 4)[:1200]}")
                    continue
                e = sh.BlobEntry.parse(data)
                print(f"-- entry {i}: version {e.version} type {e.program_type} keywords {e.keywords} "
                      f"local {e.local_keywords} code {len(e.code)} bytes")
                print(f"   parameter block: {e.tail.hex(' ', 4)[:1200]}")
                if show_code and platform == sh.PLATFORM_GLCORE:
                    print(e.code.decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
