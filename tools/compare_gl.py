"""Compare GLCore shaders in a ported build against an official Linux build.

Usage: python tools/compare_gl.py <ported _Data dir> <official _Data dir> [-v]

Matches shaders by name and variants by (pass, keyword names, tier), then
reports variants missing on either side and differences in the parameter
block (uniform layout, texture bindings) and in the uniforms/samplers the
GLSL declares. Code bodies are not compared: temp-variable naming and HLSLcc
version differences make them differ textually.
"""

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import UnityPy  # noqa: E402

from unityport import rdef, shaders as sh  # noqa: E402

DECL_RE = re.compile(r"^(?:UNITY_LOCATION\(\d+\)\s*)?uniform\s+[^;{]*;", re.M)


def gl_variants(path: Path) -> dict:
    out = {}
    env = UnityPy.load(str(path))
    for obj in env.objects:
        if obj.type.name != "Shader":
            continue
        tree = obj.read_typetree()
        if sh.PLATFORM_GLCORE not in (tree.get("platforms") or []):
            continue
        raw, _ = sh.split_blob(sh.platform_segments(tree, sh.PLATFORM_GLCORE))
        name = tree["m_ParsedForm"]["m_Name"]
        variants = out.setdefault(name, {})
        # 2021.2+ keyword names are per shader; before, per pass
        shader_names = tree["m_ParsedForm"].get("m_KeywordNames")
        for pi, ps in enumerate(sh._passes(tree["m_ParsedForm"])):
            names = dict(enumerate(shader_names)) if shader_names else {i: n for n, i in ps["m_NameIndices"]}
            for sub in sh.subprograms(ps["progVertex"]):
                sp = sub.sp
                if sp["m_GpuProgramType"] not in sh.GLCORE:
                    continue
                kw = sp.get("m_KeywordIndices", []) + sp.get("m_GlobalKeywordIndices", []) + \
                    sp.get("m_LocalKeywordIndices", [])
                key = (pi, tuple(sorted(names.get(i, f"#{i}") for i in kw)), sub.tier)
                entry = sh.BlobEntry.parse(raw[sp["m_BlobIndex"]])
                if sub.params is None:
                    params = entry.tail
                else:  # 2021.2+: source map from the program, the rest from the parameter entry
                    params = entry.tail[:8] + raw[sub.params][4:]
                variants[key] = (entry, params, sp.get("m_ShaderRequirements"))
    return out


def describe_params(params: bytes) -> tuple:
    """(sourceMap, {uniform: (group#, layout...)}, {texture: binding}, {group: size})."""
    rd = rdef._Reader(params)
    source_map = rd.u32()
    for _ in range(rd.u32()):
        rd.u32(), rd.u32()
    uniforms = {}
    sizes = {}
    for g in range(rd.u32()):
        group = f"{g}:{rd.string()}"
        sizes[group] = rd.u32()
        for _ in range(rd.u32()):
            name = rd.string()
            uniforms[name] = (group,) + tuple(rd.u32() for _ in range(6))
        rd.u32()
    textures = {}
    for _ in range(rd.u32()):
        name = rd.string()
        kind, index, extra = rd.u32(), rd.u32(), rd.u32()
        textures[name] = (kind, index, extra, rd.u32() if kind == rdef.BIND_TEXTURE else None)
    return source_map, uniforms, textures, sizes


def main():
    ported, official = Path(sys.argv[1]), Path(sys.argv[2])
    verbose = "-v" in sys.argv
    problems = Counter()
    for of in sorted(list(official.iterdir()) + list((official / "Resources").glob("*"))):
        pf = ported / of.relative_to(official)
        if not of.is_file() or not pf.exists() or of.suffix not in ("", ".assets"):
            continue
        try:
            theirs = gl_variants(of)
        except Exception:
            continue
        if not theirs:
            continue
        ours = gl_variants(pf)
        print(f"== {of.relative_to(official)}: {len(theirs)} shaders")
        for name in sorted(set(theirs) | set(ours)):
            t, o = theirs.get(name, {}), ours.get(name, {})
            missing, extra = set(t) - set(o), set(o) - set(t)
            if missing:
                problems["variants missing"] += len(missing)
                print(f"  {name}: {len(missing)} variants missing", sorted(missing)[:3] if verbose else "")
            if extra:
                problems["extra variants"] += len(extra)
                if verbose:
                    print(f"  {name}: {len(extra)} extra variants", sorted(extra)[:3])
            for key in sorted(set(t) & set(o)):
                (te, tparams, treq), (oe, oparams, oreq) = t[key], o[key]
                tdesc, odesc = describe_params(tparams), describe_params(oparams)
                issues = []
                if treq != oreq:
                    issues.append(f"requirements {treq} vs {oreq}")
                if tdesc[3] != odesc[3]:
                    issues.append(f"group sizes {tdesc[3]} vs {odesc[3]}")
                if tdesc[0] != odesc[0]:
                    issues.append(f"sourceMap {tdesc[0]} vs {odesc[0]}")
                if {n: v[:6] for n, v in tdesc[1].items()} != {n: v[:6] for n, v in odesc[1].items()}:
                    issues.append("uniform set")  # which uniforms, in which stage group, of which type
                elif tdesc[1] != odesc[1]:
                    issues.append("uniform offsets")
                if tdesc[2] != odesc[2]:
                    issues.append(f"textures {tdesc[2]} vs {odesc[2]}")
                if (te.keywords, te.local_keywords) != (oe.keywords, oe.local_keywords):
                    issues.append("keyword lists")
                tdecl = sorted(DECL_RE.findall(te.code.decode("utf-8", "replace")))
                odecl = sorted(DECL_RE.findall(oe.code.decode("utf-8", "replace")))
                if tdecl != odecl:
                    issues.append("GLSL declarations")
                for i in issues:
                    problems[i.split(" ")[0]] += 1
                if issues and verbose:
                    print(f"  {name} {key}: {', '.join(issues)}")
                    if "uniform set" in issues or "uniform offsets" in issues:
                        for n in sorted(set(tdesc[1]) | set(odesc[1])):
                            if tdesc[1].get(n) != odesc[1].get(n):
                                print(f"      {n}: unity {tdesc[1].get(n)} ours {odesc[1].get(n)}")
                    if "GLSL declarations" in issues:
                        for d in sorted(set(tdecl) ^ set(odecl)):
                            print(f"      {'unity' if d in tdecl else 'ours '}: {d}")
    print("Summary:", dict(problems) or "no differences")


if __name__ == "__main__":
    main()
