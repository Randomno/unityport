"""Make shaders in a ported assets file draw a flat colour, to find which
shader is responsible for a rendering glitch.

Usage:
  python tools/tintshaders.py <ported assets file> [name substring ...]

Every GLCore fragment program of the matching shaders (all shaders if no
substring is given) outputs one distinct colour; the colour -> shader mapping
is printed. Screenshot the game and look up the colour. With many shaders the
hues get close, so narrow down with substrings. Edits the file in place: copy
it first, or re-run unityport to undo.
"""

import colorsys
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import UnityPy  # noqa: E402

from unityport import shaders as sh  # noqa: E402


def tint(code: bytes, rgb) -> bytes:
    text = code.decode()
    i = text.find("#ifdef FRAGMENT")
    if i < 0 or not re.search(r"out\s+vec4\s+SV_Target0\b", text[i:]):
        return code
    colour = "vec4(%.3f, %.3f, %.3f, 1.0)" % rgb
    frag = re.sub(r"^([ \t]*)return;$", r"\1SV_Target0 = %s;\n\1return;" % colour, text[i:], flags=re.M)
    return (text[:i] + frag).encode()


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path, subs = sys.argv[1], sys.argv[2:]
    env = UnityPy.load(path)
    targets = []
    for obj in env.objects:
        if obj.type.name != "Shader":
            continue
        tree = obj.read_typetree()
        name = tree["m_ParsedForm"]["m_Name"]
        if sh.PLATFORM_GLCORE in tree["platforms"] and (not subs or any(s in name for s in subs)):
            targets.append((obj, tree, name))
    for k, (obj, tree, name) in enumerate(targets):
        rgb = colorsys.hsv_to_rgb(k / len(targets), 1.0, 1.0 if k % 2 else 0.6)
        raw, record = sh.split_blob(sh.platform_segments(tree, sh.PLATFORM_GLCORE))
        programs = {sub.sp["m_BlobIndex"] for ps in sh._passes(tree["m_ParsedForm"])
                    for sub in sh.subprograms(ps["progVertex"]) if sub.sp["m_GpuProgramType"] in sh.GLCORE}
        entries = []
        for i, data in enumerate(raw):
            if i in programs:  # 2021.2+ blobs also hold parameter entries
                data = sh.BlobEntry.parse(data)
                data.code = tint(data.code, rgb)
            entries.append(data)
        sh.set_platforms(tree, [(p, sh.build_segments(entries, record) if p == sh.PLATFORM_GLCORE else None)
                                for p in tree["platforms"]])
        obj.save_typetree(tree)
        print("#%02x%02x%02x  %s" % (*(int(c * 255) for c in rgb), name))
    Path(path).write_bytes(env.file.save())


if __name__ == "__main__":
    main()
