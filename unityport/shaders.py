"""Add OpenGL Core programs to D3D11-only Shader objects.

Shader layout (Unity 5.5+), as seen in the typetree:
  platforms[i], offsets[i], compressedLengths[i], decompressedLengths[i]
      one LZ4 block (no frame header) per shader compiler platform inside
      compressedBlob. Platform 4 = D3D11, 15 = GLCore. From 2019.3 the
      per-platform values are wrapped in one-element lists (segments).
  m_ParsedForm.m_SubShaders[].m_Passes[].prog{Vertex,Fragment}.m_SubPrograms[]
      one entry per platform per variant; m_BlobIndex indexes into that
      platform's decompressed blob, m_GpuProgramType says which platform.

Decompressed platform blob: u32 count, count * (u32 offset, u32 length
[, u32 segment from 2019.3]), then entries:
  u32 version, u32 programType, 4 * u32 stats,
  keywords (u32 count + aligned strings; 2018.3-2021.1 has a second,
  local keyword list), u32 codeLength, code (aligned), parameter block.

D3D11 code is a small prefix followed by a DXBC container. GLCore code is the
GLSL text of both stages wrapped in #ifdef VERTEX / #ifdef FRAGMENT, held by
the vertex sub-program. Two GLCore layouts exist:
  legacy (2017-2019): an extra empty fragment sub-program per variant, and a
      parameter block with no uniforms; Unity reflects uniforms from GL.
  2020: no fragment sub-programs, and the parameter block lists every plain
      uniform (offsets in GLSL declaration order) and texture binding.
"""

import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import lz4.block
import UnityPy

from . import rdef

PLATFORM_D3D11 = 4
PLATFORM_GLCORE = 15

# ShaderGpuProgramType
GLCORE32 = 6
DX11_VERTEX = {15, 16}       # SM40, SM50
DX11_PIXEL = {17, 18}
DX11_OTHER_STAGES = {19, 20, 21, 22}  # geometry, hull, domain

# Blob entry versions with separate global and local keyword lists
LOCAL_KEYWORDS_VERSIONS = range(201806140, 202012090)

TOOL = Path(__file__).resolve().parent.parent / "native" / "build" / "hlslcc_tool"


def _first(x):
    return x[0] if isinstance(x, list) else x


def _aligned(n: int) -> int:
    return (n + 3) & ~3


def _pack_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b + b"\0" * (_aligned(len(b)) - len(b))


@dataclass
class BlobEntry:
    version: int
    program_type: int
    stats: bytes
    keywords: list[str]
    local_keywords: list[str] | None
    code: bytes
    tail: bytes

    @classmethod
    def parse(cls, data: bytes) -> "BlobEntry":
        version, ptype = struct.unpack_from("<II", data, 0)
        stats = data[8:24]
        pos = 24

        def strings():
            nonlocal pos
            (count,) = struct.unpack_from("<I", data, pos)
            pos += 4
            out = []
            for _ in range(count):
                (n,) = struct.unpack_from("<I", data, pos)
                out.append(data[pos + 4:pos + 4 + n].decode("utf-8"))
                pos += 4 + _aligned(n)
            return out

        keywords = strings()
        local = strings() if version in LOCAL_KEYWORDS_VERSIONS else None
        (n,) = struct.unpack_from("<I", data, pos)
        code = data[pos + 4:pos + 4 + n]
        pos += 4 + _aligned(n)
        return cls(version, ptype, stats, keywords, local, code, data[pos:])

    def serialize(self) -> bytes:
        out = bytearray(struct.pack("<II", self.version, self.program_type))
        out += self.stats
        for kws in (self.keywords, self.local_keywords):
            if kws is None:
                continue
            out += struct.pack("<I", len(kws))
            for k in kws:
                out += _pack_string(k)
        out += struct.pack("<I", len(self.code)) + self.code
        out += b"\0" * (_aligned(len(self.code)) - len(self.code))
        out += self.tail
        return bytes(out)

    def dxbc(self) -> bytes:
        start = self.code.find(b"DXBC")
        if start < 0:
            raise ValueError("no DXBC container in program code")
        (size,) = struct.unpack_from("<I", self.code, start + 24)
        return self.code[start:start + size]


def parse_blob(raw: bytes) -> tuple[list[BlobEntry], int]:
    """Return the entries and the index record size (8, or 12 with segments)."""
    (count,) = struct.unpack_from("<I", raw, 0)
    if count == 0:
        return [], 8
    (first,) = struct.unpack_from("<I", raw, 4)
    record = 12 if first == 4 + 12 * count else 8
    entries = []
    for i in range(count):
        off, length = struct.unpack_from("<II", raw, 4 + record * i)
        entries.append(BlobEntry.parse(raw[off:off + length]))
    return entries, record


def build_blob(entries: list[BlobEntry], record: int) -> bytes:
    datas = [e.serialize() for e in entries]
    header = struct.pack("<I", len(datas))
    offset = 4 + record * len(datas)
    body = b""
    for d in datas:
        header += struct.pack("<II", offset + len(body), len(d))
        if record == 12:
            header += struct.pack("<I", 0)  # segment
        body += d
    return header + body


def platform_blob(tree: dict, platform: int) -> bytes:
    i = tree["platforms"].index(platform)
    blob = bytes(tree["compressedBlob"])
    off = _first(tree["offsets"][i])
    clen = _first(tree["compressedLengths"][i])
    dlen = _first(tree["decompressedLengths"][i])
    return lz4.block.decompress(blob[off:off + clen], uncompressed_size=dlen)


def _append_platform(tree: dict, platform: int, raw: bytes) -> None:
    comp = lz4.block.compress(raw, mode="high_compression", store_size=False)
    blob = bytes(tree["compressedBlob"])
    nested = isinstance(tree["offsets"][0], list)
    wrap = (lambda v: [v]) if nested else (lambda v: v)
    tree["platforms"].append(platform)
    tree["offsets"].append(wrap(len(blob)))
    tree["compressedLengths"].append(wrap(len(comp)))
    tree["decompressedLengths"].append(wrap(len(raw)))
    tree["compressedBlob"] = list(blob + comp)


# --- GLCore parameter blocks -------------------------------------------------

def _legacy_gl_tail(source_map: int) -> bytes:
    # Observed on every GLCore entry in Unity 2017.3/2018.4's own resources:
    # source map, channel count (0), then 1 and five zero words.
    return struct.pack("<8I", source_map, 0, 1, 0, 0, 0, 0, 0)


_UNIFORM_RE = re.compile(
    r"^(?:UNITY_LOCATION\((\d+)\)\s*)?uniform\s+(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+(\w+)(?:\[(\d+)\])?\s*;",
    re.M)
_SAMPLER_DIMS = {"2D": 2, "3D": 3, "Cube": 4, "2DArray": 5, "CubeArray": 6,
                 "2DShadow": 2, "2DArrayShadow": 5, "CubeShadow": 4, "CubeArrayShadow": 6,
                 "2DMS": 2, "2DMSArray": 5}
# Parameter type codes in GL parameter blocks; Unity uses 5 for int/ivec (uint/bool assumed)
_SCALAR_TYPES = {"float": 0, "vec": 0, "int": 5, "ivec": 5, "uint": 5, "uvec": 5, "bool": 2, "bvec": 2}


# VertexAttribute (2019+) for the in_<SEMANTIC><n> inputs HLSLcc emits
_VERTEX_ATTRIBUTES = {"POSITION": 0, "NORMAL": 1, "TANGENT": 2, "COLOR": 3, "TEXCOORD": 4,
                      "BLENDWEIGHTS": 12, "BLENDWEIGHT": 12, "BLENDINDICES": 13}
_INPUT_RE = re.compile(r"^in\s+\w+\s+in_([A-Z]+)(\d+)\s*;", re.M)


def gl_source_map(glsl: bytes) -> int:
    """Bitmask of the vertex attributes the GLSL vertex stage actually reads."""
    text = glsl.decode("utf-8", "replace").split("#ifdef FRAGMENT")[0]
    mask = 0
    for semantic, index in _INPUT_RE.findall(text):
        base = _VERTEX_ATTRIBUTES.get(semantic)
        if base is not None:
            mask |= 1 << (base + (int(index) if semantic == "TEXCOORD" else 0))
    return mask


def _stage_uniforms(text: str, textures: dict) -> tuple[list, int]:
    """Plain uniforms of one GLSL stage, laid out the way Unity 2020's GL compiler does.

    Declaration order; scalars/vec2 align to their size, vec3/vec4, arrays and
    matrices to 16 bytes; scalar and vec2 arrays are packed tightly. Verified
    against every uniform of an official 2020.3 Linux build.
    """
    vectors, matrices = [], []
    seen = set()
    offset = 0
    for m in _UNIFORM_RE.finditer(text):
        loc, gtype, name, count = m.group(1), m.group(2), m.group(3), m.group(4)
        n = int(count) if count else 0
        if "sampler" in gtype:
            # hlslcc_zcmp<name> is the depth-compare view of <name>; Unity binds it itself.
            if not name.startswith("hlslcc_zcmp") and name not in textures:
                dim_name = gtype.split("sampler", 1)[1]
                textures[name] = (int(loc) if loc else len(textures), _SAMPLER_DIMS.get(dim_name, 2),
                                  dim_name.startswith("2DMS"))
            continue
        if name in seen:
            continue
        seen.add(name)
        if name.startswith("hlslcc_mtx4x4"):
            elements = n // 4 if n > 4 else 0
            offset = (offset + 15) & ~15
            matrices.append((name[len("hlslcc_mtx4x4"):], 0, 4, 4, 1, elements, offset))
            offset += 64 * max(elements, 1)
            continue
        base = gtype.rstrip("234")
        cols = int(gtype[-1]) if gtype[-1] in "234" else 1
        if n:
            align, size = 16, n * (16 if cols == 3 else 4 * cols)
        else:
            align, size = {1: 4, 2: 8, 3: 16, 4: 16}[cols], 4 * cols
        offset = (offset + align - 1) & ~(align - 1)
        vectors.append((name, _SCALAR_TYPES.get(base, 0), 1, cols, 0, n, offset))
        offset += size
    return vectors + matrices, (offset + 15) & ~15


def gl_tail_2020(glsl: bytes, source_map: int) -> bytes:
    """Parameter block for a 2020 GLCore entry, derived from the GLSL text.

    Matches Unity's own GL compiler: an empty globals group, then one
    "$Globals" group per stage that has plain uniforms (fragment first), each
    laid out from offset 0 in declaration order with 16 bytes per vector
    slot; then the samplers as texture bindings at their UNITY_LOCATION.
    """
    text = glsl.decode("utf-8", "replace")
    if re.search(r"^\s*(?:UNITY_BINDING\(\d+\)\s*)?(?:layout\([^)]*\)\s*)?uniform\s+\w+\s*\{", text, re.M):
        raise ValueError("uniform blocks are not supported")
    vertex, _, fragment = text.partition("#ifdef FRAGMENT")
    textures: dict = {}
    vert_params, vert_size = _stage_uniforms(vertex, textures)
    frag_params, frag_size = _stage_uniforms(fragment, textures)
    groups = [(params, size) for params, size in ((frag_params, frag_size), (vert_params, vert_size)) if params]

    out = bytearray(struct.pack("<3I", source_map, 0, 1 + len(groups)))
    out += _pack_string("") + struct.pack("<3I", 0, 0, 0)
    for params, size in groups:
        out += _pack_string("$Globals") + struct.pack("<2I", size, len(params))
        for name, ptype, rows, cols, is_matrix, arr, off in params:
            out += _pack_string(name) + struct.pack("<6I", ptype, rows, cols, is_matrix, arr, off)
        out += struct.pack("<I", 0)  # struct count
    out += struct.pack("<I", len(textures))
    for name, (slot, dim, ms) in textures.items():
        out += _pack_string(name) + struct.pack("<4I", rdef.BIND_TEXTURE, slot, slot, (dim << 1) | ms)
    return bytes(out)


# --- Translation -------------------------------------------------------------

class Translator:
    """Runs hlslcc_tool over many vertex/fragment pairs in one process."""

    def __init__(self, workdir: Path, explicit_locations: bool):
        self.workdir = workdir
        self.explicit_locations = explicit_locations
        self.jobs: list[tuple[Path, Path, Path]] = []
        self.cache: dict[tuple[bytes, bytes], int] = {}

    def add(self, vs: bytes, ps: bytes) -> int:
        key = (vs, ps)
        if key in self.cache:
            return self.cache[key]
        n = len(self.jobs)
        paths = (self.workdir / f"{n}.vs", self.workdir / f"{n}.ps", self.workdir / f"{n}.glsl")
        paths[0].write_bytes(vs)
        paths[1].write_bytes(ps)
        self.jobs.append(paths)
        self.cache[key] = n
        return n

    def run(self) -> dict[int, bytes | str]:
        if not TOOL.exists():
            raise RuntimeError(f"{TOOL} not built; see native/CMakeLists.txt")
        args = [str(TOOL)] + (["--explicit-locations"] if self.explicit_locations else [])
        status: dict[str, str] = {}
        start = 0
        while start < len(self.jobs):
            stdin = "".join(f"{v}\t{p}\t{o}\n" for v, p, o in self.jobs[start:])
            out = subprocess.run(args, input=stdin, capture_output=True, text=True)
            for line in out.stdout.splitlines():
                kind, path, *msg = line.split(" ", 2)
                status[path] = " ".join(msg)
            done = start + len(out.stdout.splitlines())
            if out.returncode == 0 or done >= len(self.jobs):
                break
            # HLSLcc crashed on the job after the last one reported; skip it and resume.
            status[str(self.jobs[done][2])] = "translator crashed"
            start = done + 1
        results: dict[int, bytes | str] = {}
        for i, (_, _, o) in enumerate(self.jobs):
            if str(o) in status and o.exists() and not status[str(o)]:
                results[i] = o.read_bytes()
            else:
                results[i] = status.get(str(o)) or "translator crashed"
        return results


def _keywords(sp: dict) -> frozenset:
    """A sub-program's keyword indices, tagged by namespace (global/local)."""
    if "m_KeywordIndices" in sp:
        return frozenset(("g", i) for i in sp["m_KeywordIndices"])
    return frozenset([("g", i) for i in sp["m_GlobalKeywordIndices"]] +
                     [("l", i) for i in sp["m_LocalKeywordIndices"]])


def _combine(v: dict, f: dict, ventry: "BlobEntry", fentry: "BlobEntry") -> tuple[dict, list, list | None]:
    """Parsed-form keyword fields and blob keyword strings for a vertex+fragment combination."""
    sp = dict(v)
    if "m_KeywordIndices" in v:
        sp["m_KeywordIndices"] = sorted(set(v["m_KeywordIndices"]) | set(f["m_KeywordIndices"]))
    else:
        for k in ("m_GlobalKeywordIndices", "m_LocalKeywordIndices"):
            sp[k] = sorted(set(v[k]) | set(f[k]))
    keywords = sorted(set(ventry.keywords) | set(fentry.keywords))
    local = None
    if ventry.local_keywords is not None:
        local = sorted(set(ventry.local_keywords) | set(fentry.local_keywords or []))
    return sp, keywords, local


def _reflection(entry: BlobEntry, sp: dict, prog: dict, names: dict[int, str]) -> rdef.Reflection:
    if "m_Parameters" not in sp:  # 2017-2019: everything on the sub-program
        return rdef.from_serialized_params(sp, names)
    common = rdef.from_serialized_params(prog.get("m_CommonParameters", {}), names)
    own = rdef.from_serialized_params(sp["m_Parameters"], names)
    _, _, blob = rdef.parse_blob_params(entry.tail)
    # A slot the variant binds itself takes precedence over a common binding.
    taken = {slot: name for name, slot in {**own.cb_slots, **blob.cb_slots}.items()}
    common.cb_slots = {n: s for n, s in common.cb_slots.items() if taken.get(s, n) == n}
    return common.merge(own).merge(blob)


class Unsupported(Exception):
    pass


def program_dxbc(entry: BlobEntry, sp: dict, prog: dict, names: dict[int, str]) -> bytes:
    """DXBC for a D3D11 subprogram with its reflection chunk restored."""
    dxbc = entry.dxbc()
    refl = _reflection(entry, sp, prog, names)
    if refl.buffers:
        # Structured buffers/UAVs need SSBOs (GL 4.3); GLCore 3.2 has none.
        raise Unsupported("structured buffers: " + ", ".join(refl.buffers))
    return rdef.with_rdef(dxbc, rdef.build_rdef(refl, rdef.version_token(dxbc)))


def _passes(parsed: dict):
    for ss in parsed["m_SubShaders"]:
        for ps in ss["m_Passes"]:
            yield ps


def _empty_params(params: dict) -> dict:
    return {k: [] if isinstance(v, list) else v for k, v in params.items()}


def _gl_subprogram(template: dict, blob_index: int, source_map: int) -> dict:
    sp = dict(template)
    sp["m_BlobIndex"] = blob_index
    sp["m_GpuProgramType"] = GLCORE32
    sp["m_Channels"] = {"m_Channels": [], "m_SourceMap": source_map}
    if "m_Parameters" in sp:
        sp["m_Parameters"] = _empty_params(sp["m_Parameters"])
    for k, v in template.items():
        if k.endswith("Params") or k in ("m_ConstantBuffers", "m_ConstantBufferBindings", "m_Samplers"):
            sp[k] = []
    return sp


@dataclass
class ShaderPlan:
    tree: dict
    d3d: list[BlobEntry]
    record: int
    new_layout: bool  # 2020-style GLCore programs
    jobs: list = field(default_factory=list)  # (pass, vertex_sp, fragment_sp, job)
    skipped: set = field(default_factory=set)


def is_new_layout(tree: dict) -> bool:
    return any("m_CommonParameters" in ps["progVertex"] for ps in _passes(tree["m_ParsedForm"]))


def plan_shader(tree: dict, translator: Translator) -> ShaderPlan | None:
    platforms = tree.get("platforms") or []
    if PLATFORM_GLCORE in platforms or PLATFORM_D3D11 not in platforms:
        return None
    d3d, record = parse_blob(platform_blob(tree, PLATFORM_D3D11))
    plan = ShaderPlan(tree, d3d, record, is_new_layout(tree))

    for ps in _passes(tree["m_ParsedForm"]):
        if any(sp["m_GpuProgramType"] in DX11_OTHER_STAGES
               for prog in ("progGeometry", "progHull", "progDomain")
               for sp in ps.get(prog, {}).get("m_SubPrograms", [])):
            continue
        names = {i: n for n, i in ps["m_NameIndices"]}
        verts = [sp for sp in ps["progVertex"]["m_SubPrograms"] if sp["m_GpuProgramType"] in DX11_VERTEX]
        frags = [sp for sp in ps["progFragment"]["m_SubPrograms"] if sp["m_GpuProgramType"] in DX11_PIXEL]
        # D3D11 compiles each stage separately, dropping keywords the stage does
        # not use, so the stages have different variant sets. GL needs one
        # program per combination: pair every vertex and fragment variant that
        # agree on the keywords both stages know about. This reproduces the
        # variant set of Unity's own GLCore builds exactly.
        vert_kws = frozenset().union(*map(_keywords, verts)) if verts else frozenset()
        frag_kws = frozenset().union(*map(_keywords, frags)) if frags else frozenset()
        dxbc_cache: dict[tuple[str, int], bytes | Unsupported] = {}

        def dxbc_for(sp, prog):
            key = (prog, sp["m_BlobIndex"])
            if key not in dxbc_cache:
                try:
                    dxbc_cache[key] = program_dxbc(d3d[sp["m_BlobIndex"]], sp, ps[prog], names)
                except Unsupported as e:
                    dxbc_cache[key] = e
            if isinstance(dxbc_cache[key], Unsupported):
                raise dxbc_cache[key]
            return dxbc_cache[key]

        for v in verts:
            vk = _keywords(v)
            for f in frags:
                fk = _keywords(f)
                if f["m_ShaderHardwareTier"] != v["m_ShaderHardwareTier"] or vk & frag_kws != fk & vert_kws:
                    continue
                try:
                    job = translator.add(dxbc_for(v, "progVertex"), dxbc_for(f, "progFragment"))
                except Unsupported as e:
                    plan.skipped.add(str(e))
                    continue
                plan.jobs.append((ps, v, f, job))
    return plan if plan.jobs else None


def apply_plan(plan: ShaderPlan, results: dict) -> tuple[int, int, set[str]]:
    """Write translated programs into the Shader typetree. Returns (ok, failed, errors)."""
    entries: list[BlobEntry] = []
    ok = failed = 0
    errors = set()
    new_subprograms = []
    gl_passes = []
    for ps, v, f, job in plan.jobs:
        glsl = results[job]
        vsrc = plan.d3d[v["m_BlobIndex"]]
        source_map = v["m_Channels"]["m_SourceMap"]
        if isinstance(glsl, bytes) and plan.new_layout:
            source_map = gl_source_map(glsl)
            try:
                tail = gl_tail_2020(glsl, source_map)
            except ValueError as e:
                glsl = str(e)
        if not isinstance(glsl, bytes):
            failed += 1
            errors.add(glsl)
            continue
        ok += 1
        if not plan.new_layout:
            tail = _legacy_gl_tail(source_map)
        fsrc = plan.d3d[f["m_BlobIndex"]]
        combined, keywords, local = _combine(v, f, vsrc, fsrc)
        vi = len(entries)
        entries.append(BlobEntry(vsrc.version, GLCORE32, b"\0" * 16, keywords, local, glsl, tail))
        new_subprograms.append((ps["progVertex"], _gl_subprogram(combined, vi, source_map)))
        if not plan.new_layout:
            fi = len(entries)
            entries.append(BlobEntry(fsrc.version, GLCORE32, b"\0" * 16, keywords, local,
                                     b"", _legacy_gl_tail(0)))
            new_subprograms.append((ps["progFragment"], _gl_subprogram(dict(f, **{
                k: combined[k] for k in ("m_KeywordIndices", "m_GlobalKeywordIndices", "m_LocalKeywordIndices")
                if k in combined}), fi, 0)))
        gl_passes.append(ps)
    if not entries:
        return ok, failed, errors
    for prog, sp in new_subprograms:
        prog["m_SubPrograms"].append(sp)
    for ps in gl_passes:
        platforms = ps.get("m_Platforms")
        if platforms is not None and PLATFORM_GLCORE not in platforms:
            # m_EditorDataHash is parallel to m_Platforms; Unity rejects the
            # pass ("not supported on this GPU") if they do not line up.
            hashes = ps.get("m_EditorDataHash")
            if hashes is not None and len(hashes) == len(platforms):
                src = platforms.index(PLATFORM_D3D11) if PLATFORM_D3D11 in platforms else 0
                hashes.append(dict(hashes[src]))
            platforms.append(PLATFORM_GLCORE)
    _append_platform(plan.tree, PLATFORM_GLCORE, build_blob(entries, plan.record))
    if plan.new_layout:
        _drop_d3d11(plan.tree)
    return ok, failed, errors


def _drop_d3d11(tree: dict) -> None:
    """Remove the D3D11 programs, leaving a GLCore-only shader.

    In the 2020 layout the GLCore program covers both stages and has no
    fragment sub-program; if D3D11 fragment sub-programs remain, the player
    rejects the pass ("not supported on this GPU"). Official Linux builds
    also have empty m_CommonParameters (it holds D3D11 reflection data).
    """
    for ps in _passes(tree["m_ParsedForm"]):
        for key, prog in ps.items():
            if not key.startswith("prog") or not isinstance(prog, dict):
                continue
            prog["m_SubPrograms"] = [sp for sp in prog["m_SubPrograms"]
                                     if sp["m_GpuProgramType"] == GLCORE32]
            if "m_CommonParameters" in prog:
                prog["m_CommonParameters"] = _empty_params(prog["m_CommonParameters"])
        platforms = ps.get("m_Platforms")
        if platforms and PLATFORM_D3D11 in platforms:
            i = platforms.index(PLATFORM_D3D11)
            del platforms[i]
            hashes = ps.get("m_EditorDataHash")
            if hashes is not None and len(hashes) > i:
                del hashes[i]
    i = tree["platforms"].index(PLATFORM_D3D11)
    for key in ("platforms", "offsets", "compressedLengths", "decompressedLengths"):
        del tree[key][i]
    # Offsets still index into compressedBlob, which keeps the unused D3D11
    # bytes; rebuild it with only the remaining platforms.
    blob = bytes(tree["compressedBlob"])
    nested = isinstance(tree["offsets"][0], list)
    new_blob = b""
    for j in range(len(tree["platforms"])):
        off, length = _first(tree["offsets"][j]), _first(tree["compressedLengths"][j])
        tree["offsets"][j] = [len(new_blob)] if nested else len(new_blob)
        new_blob += blob[off:off + length]
    tree["compressedBlob"] = list(new_blob)


def convert_file(path: Path) -> tuple[int, int, int]:
    """Translate every D3D11-only shader in an assets file in place.

    Returns (shaders changed, programs translated, programs failed).
    """
    env = UnityPy.load(str(path))
    shaders = [(obj, obj.read_typetree()) for obj in env.objects if obj.type.name == "Shader"]
    if not shaders:
        return 0, 0, 0
    new_layout = any(is_new_layout(t) for _, t in shaders)
    plans = []
    with tempfile.TemporaryDirectory() as tmp:
        translator = Translator(Path(tmp), explicit_locations=new_layout)
        for obj, tree in shaders:
            name = tree["m_ParsedForm"]["m_Name"]
            try:
                plan = plan_shader(tree, translator)
            except (ValueError, KeyError, struct.error) as e:
                print(f"  {name}: skipped ({e})")
                continue
            if plan:
                plans.append((obj, plan))
                for reason in plan.skipped:
                    print(f"  {name}: some variants skipped ({reason})")
        if not plans:
            return 0, 0, 0
        results = translator.run()

    changed = total_ok = total_failed = 0
    for obj, plan in plans:
        ok, failed, errors = apply_plan(plan, results)
        total_ok += ok
        total_failed += failed
        if failed:
            name = plan.tree["m_ParsedForm"]["m_Name"]
            print(f"  {name}: {failed} of {ok + failed} programs failed: {'; '.join(errors)[:300]}")
        if ok:
            obj.save_typetree(plan.tree)
            changed += 1
    if changed:
        path.write_bytes(env.file.save())
    return changed, total_ok, total_failed


def convert_data_dir(data_dir: Path) -> None:
    files = [p for p in sorted(data_dir.rglob("*"))
             if p.is_file() and p.suffix in ("", ".assets")
             and p.name != "unity default resources" and not p.name.endswith(".resS")
             and p.parent.name not in ("Managed", "Mono", "MonoBleedingEdge", "Plugins", "etc")]
    for p in files:
        try:
            changed, ok, failed = convert_file(p)
        except Exception as e:  # not a SerializedFile, or unsupported layout
            if p.suffix == ".assets" or p.name.startswith(("level", "unity_builtin")):
                print(f"{p.relative_to(data_dir)}: skipped ({type(e).__name__}: {e})")
            continue
        if changed:
            print(f"{p.relative_to(data_dir)}: {changed} shaders, {ok} programs translated, {failed} failed")
