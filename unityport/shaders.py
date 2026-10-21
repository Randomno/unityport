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
[, u32 segment from 2019.3]), then entries. A large platform blob is split
into segments (one LZ4 block each, at most 16 MiB): segment 0 holds just the
index and the entries are in the following segments. Entries:
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

2021.2+ moves sub-programs to prog*.m_PlayerSubPrograms[tier][] (with
m_ParameterBlobIndices[tier][] alongside) and the parameter block out of the
program entry: it is a blob entry of its own (u32 version, then the 2020
block from the group count on), shared by variants with equal parameters.
The program entry keeps only u32 sourceMap, u32 channel count (0) after the
code. In SRP shaders, GLCore programs declare named constant buffers as
std140 uniform blocks ($Globals stays plain uniforms); shaders using the
built-in pipeline's UnityCG includes get plain uniforms throughout.
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
from .glsl import apply_fixups

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


def split_blob(segments: bytes | list[bytes]) -> tuple[list[bytes], int]:
    """Return the raw entries and the index record size (8, or 12 with segments)."""
    if isinstance(segments, bytes):
        segments = [segments]
    raw = segments[0]
    (count,) = struct.unpack_from("<I", raw, 0)
    if count == 0:
        return [], 8
    (first,) = struct.unpack_from("<I", raw, 4)
    record = 12 if first == 4 + 12 * count or len(segments) > 1 else 8
    entries = []
    for i in range(count):
        off, length = struct.unpack_from("<II", raw, 4 + record * i)
        seg = struct.unpack_from("<I", raw, 4 + record * i + 8)[0] if record == 12 else 0
        entries.append(segments[seg][off:off + length])
    return entries, record


def parse_blob(segments: bytes | list[bytes]) -> tuple[list[BlobEntry], int]:
    """Parse a blob of program entries only (before 2021.2, which adds parameter entries)."""
    entries, record = split_blob(segments)
    return [BlobEntry.parse(e) for e in entries], record


class LazyEntries:
    """Program entries of a 2021.2+ blob, parsed on first use (parameter entries stay raw)."""

    def __init__(self, raw: list[bytes]):
        self.raw = raw
        self._parsed: dict[int, BlobEntry] = {}

    def __getitem__(self, i: int) -> BlobEntry:
        if i not in self._parsed:
            self._parsed[i] = BlobEntry.parse(self.raw[i])
        return self._parsed[i]


SEGMENT_SIZE = 1 << 24


def build_blob(entries: list[BlobEntry | bytes], record: int) -> bytes:
    (segment,) = build_segments(entries, record, max_size=None)
    return segment


def build_segments(entries: list[BlobEntry | bytes], record: int, max_size: int | None = SEGMENT_SIZE) -> list[bytes]:
    datas = [e if isinstance(e, bytes) else e.serialize() for e in entries]
    header_size = 4 + record * len(datas)
    if max_size is None or record == 8 or header_size + sum(map(len, datas)) <= max_size:
        segments = [b""]
        base = header_size
    else:  # index alone in segment 0, entries packed into the following ones
        segments = [b"", b""]
        base = 0
    header = struct.pack("<I", len(datas))
    for d in datas:
        if base == 0 and segments[-1] and len(segments[-1]) + len(d) > max_size:
            segments.append(b"")
        header += struct.pack("<II", base + len(segments[-1]), len(d))
        if record == 12:
            header += struct.pack("<I", len(segments) - 1)
        segments[-1] += d
    segments[0] = header + segments[0]
    return segments


def _segment_records(tree: dict, i: int) -> list[tuple[int, int, int]]:
    """(offset, compressed length, decompressed length) of platform i's segments."""
    fields = [tree[k][i] for k in ("offsets", "compressedLengths", "decompressedLengths")]
    return list(zip(*fields)) if isinstance(fields[0], list) else [tuple(fields)]


def platform_segments(tree: dict, platform: int) -> list[bytes]:
    blob = bytes(tree["compressedBlob"])
    return [lz4.block.decompress(blob[off:off + clen], uncompressed_size=dlen)
            for off, clen, dlen in _segment_records(tree, tree["platforms"].index(platform))]


def platform_blob(tree: dict, platform: int) -> bytes:
    """A platform's blob; only for platforms stored in one segment."""
    (segment,) = platform_segments(tree, platform)
    return segment


def set_platforms(tree: dict, platforms: list[tuple[int, list[bytes] | None]]) -> None:
    """Rewrite compressedBlob with these platforms, in order: (platform, segments),
    where segments None keeps that platform's current data."""
    blob = bytes(tree["compressedBlob"])
    nested = isinstance(tree["offsets"][0], list)
    compressed = []
    for platform, segments in platforms:
        if segments is None:
            records = _segment_records(tree, tree["platforms"].index(platform))
            compressed.append((platform, [(blob[off:off + clen], dlen) for off, clen, dlen in records]))
        else:
            compressed.append((platform, [(lz4.block.compress(seg, mode="high_compression", store_size=False),
                                           len(seg)) for seg in segments]))
    out = b""
    for key in ("platforms", "offsets", "compressedLengths", "decompressedLengths"):
        tree[key] = []
    for platform, segs in compressed:
        if not nested and len(segs) != 1:
            raise ValueError("segmented blob in a pre-2019.3 shader")
        offsets = []
        for comp, _ in segs:
            offsets.append(len(out))
            out += comp
        values = (offsets, [len(c) for c, _ in segs], [d for _, d in segs])
        tree["platforms"].append(platform)
        for key, v in zip(("offsets", "compressedLengths", "decompressedLengths"), values):
            tree[key].append(v if nested else v[0])
    tree["compressedBlob"] = list(out)


def _append_platform(tree: dict, platform: int, segments: list[bytes]) -> None:
    set_platforms(tree, [(p, None) for p in tree["platforms"]] + [(platform, segments)])
    if "stageCounts" in tree:  # 2021.2+, one per platform: 2 for D3D11, 1 in official GLCore builds
        tree["stageCounts"].append(1)


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


_BLOCK_RE = re.compile(r"^(?:UNITY_BINDING\((\d+)\)\s*)?uniform\s+(\w+)\s*\{(.*?)^\};", re.M | re.S)
_MEMBER_RE = re.compile(r"^\s*(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+(\w+)(?:\[(\d+)\])?\s*;", re.M)


def _block_members(body: str) -> tuple[list, int]:
    """std140 layout of a uniform block's members as Unity parameters, minus rdef's fillers."""
    vectors, matrices = [], []
    offset = 0
    for gtype, name, count in _MEMBER_RE.findall(body):
        n = int(count) if count else 0
        cols = int(gtype[-1]) if gtype[-1] in "234" else 1
        align = 16 if n or cols >= 3 else 4 * cols
        offset = (offset + align - 1) & ~(align - 1)
        if name.startswith("hlslcc_mtx4x4"):
            matrices.append((name[len("hlslcc_mtx4x4"):], 0, 4, 4, 1, n // 4 if n > 4 else 0, offset))
        elif not name.startswith(rdef.PAD_PREFIX):
            vectors.append((name, _SCALAR_TYPES.get(gtype.rstrip("234"), 0), 1, cols, 0, n, offset))
        offset += 16 * n if n else 4 * cols
    return vectors + matrices, offset


def _stage_uniforms(text: str, textures: dict) -> tuple[list, int]:
    """Plain uniforms of one GLSL stage, laid out the way Unity 2020's GL compiler does.

    Declaration order; scalars/vec2 align to their size, vec3/vec4, arrays and
    matrices to 16 bytes; scalar and vec2 arrays are packed tightly. Verified
    against every uniform of an official 2020.3 Linux build. Returns the
    parameters and the end offset.
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
    return vectors + matrices, offset


def _gl_parameters(glsl: bytes, round_sizes: bool, block_sizes: dict[str, int] | None) -> bytes:
    """Parameter groups and bindings for a GLCore program, derived from the GLSL text.

    Matches Unity's own GL compiler: an empty globals group, then per stage
    (fragment first) a "$Globals" group if it has plain uniforms, laid out
    from offset 0 in declaration order with 16 bytes per vector slot (sizes
    rounded up to 16 in 2020), and a group for each uniform block not listed
    yet. Bindings: the samplers as textures at their UNITY_LOCATION, then the
    uniform blocks at their UNITY_BINDING. block_sizes gives the D3D11 size of
    each block, as only the variables up to the last used one are declared;
    None means uniform blocks are not expected.
    """
    text = glsl.decode("utf-8", "replace")
    textures: dict = {}
    stages = []  # (plain uniforms, end offset, [(block, binding, members, size)])
    for stage in text.partition("#ifdef FRAGMENT")[::2]:
        stage_blocks = []
        for m in _BLOCK_RE.finditer(stage):
            if block_sizes is None:
                raise ValueError("uniform blocks are not supported")
            members, size = _block_members(m.group(3))
            stage_blocks.append((m.group(2), int(m.group(1) or 0), members, max(size, block_sizes.get(m.group(2), 0))))
        stages.append((*_stage_uniforms(_BLOCK_RE.sub("", stage), textures), stage_blocks))
    groups = []
    blocks: dict = {}  # name -> binding
    for params, size, stage_blocks in reversed(stages):
        if params:
            groups.append(("$Globals", params, (size + 15) & ~15 if round_sizes else size))
        for name, binding, members, block_size in stage_blocks:
            if name not in blocks:
                blocks[name] = binding
                groups.append((name, members, block_size))

    out = bytearray(struct.pack("<I", 1 + len(groups)))
    out += _pack_string("") + struct.pack("<3I", 0, 0, 0)
    for group, params, size in groups:
        out += _pack_string(group) + struct.pack("<2I", size, len(params))
        for name, ptype, rows, cols, is_matrix, arr, off in params:
            out += _pack_string(name) + struct.pack("<6I", ptype, rows, cols, is_matrix, arr, off)
        out += struct.pack("<I", 0)  # struct count
    out += struct.pack("<I", len(textures) + len(blocks))
    for name, (slot, dim, ms) in textures.items():
        out += _pack_string(name) + struct.pack("<4I", rdef.BIND_TEXTURE, slot, slot, (dim << 1) | ms)
    for name, binding in blocks.items():
        out += _pack_string(name) + struct.pack("<3I", rdef.BIND_CBUFFER, binding, 0)
    return bytes(out)


def gl_tail_2020(glsl: bytes, source_map: int) -> bytes:
    """Parameter block that follows the code of a 2020 GLCore entry."""
    return struct.pack("<2I", source_map, 0) + _gl_parameters(glsl, True, None)


def gl_params_2021(glsl: bytes, version: int, block_sizes: dict[str, int]) -> bytes:
    """2021.2+ parameter blob entry for a GLCore program."""
    return struct.pack("<I", version) + _gl_parameters(glsl, False, block_sizes)


# --- Translation -------------------------------------------------------------

class Translator:
    """Runs hlslcc_tool over many vertex/fragment pairs in one process."""

    def __init__(self, workdir: Path, explicit_locations: bool):
        self.workdir = workdir
        self.explicit_locations = explicit_locations
        self.jobs: list[tuple[Path, Path, Path, bool]] = []
        self.cache: dict[tuple[bytes, bytes, bool], int] = {}

    def add(self, vs: bytes, ps: bytes, uniform_blocks: bool = False) -> int:
        key = (vs, ps, uniform_blocks)
        if key in self.cache:
            return self.cache[key]
        n = len(self.jobs)
        paths = (self.workdir / f"{n}.vs", self.workdir / f"{n}.ps", self.workdir / f"{n}.glsl")
        paths[0].write_bytes(vs)
        paths[1].write_bytes(ps)
        self.jobs.append((*paths, uniform_blocks))
        self.cache[key] = n
        return n

    def run(self) -> dict[int, bytes | str]:
        if not TOOL.exists():
            raise RuntimeError(f"{TOOL} not built; see native/CMakeLists.txt")
        args = [str(TOOL)] + (["--explicit-locations"] if self.explicit_locations else [])
        status: dict[str, str] = {}
        start = 0
        while start < len(self.jobs):
            stdin = "".join("\t".join(map(str, (v, p, o))) + ("\tuniform-blocks" if ub else "") + "\n"
                            for v, p, o, ub in self.jobs[start:])
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
        for i, (_, _, o, _) in enumerate(self.jobs):
            if str(o) in status and o.exists() and not status[str(o)]:
                try:
                    results[i] = apply_fixups(o.read_bytes())
                except ValueError as e:
                    results[i] = str(e)
            else:
                results[i] = status.get(str(o)) or "translator crashed"
        return results


@dataclass
class Sub:
    """One variant of a program: the parsed-form sub-program and where it sits."""
    sp: dict
    tier: int
    params: int | None  # 2021.2+: index of its parameter blob entry


def subprograms(prog: dict) -> list[Sub]:
    if "m_PlayerSubPrograms" in prog:
        return [Sub(sp, tier, p)
                for tier, (sps, params) in enumerate(zip(prog["m_PlayerSubPrograms"], prog["m_ParameterBlobIndices"]))
                for sp, p in zip(sps, params)]
    return [Sub(sp, sp["m_ShaderHardwareTier"], None) for sp in prog.get("m_SubPrograms", [])]


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


def _reflection(entry: BlobEntry, sp: dict, prog: dict, names: dict[int, str],
                params: bytes | None) -> rdef.Reflection:
    if "m_CommonParameters" not in prog:  # 2017-2019: everything on the sub-program
        return rdef.from_serialized_params(sp, names)
    common = rdef.from_serialized_params(prog["m_CommonParameters"], names)
    own = rdef.from_serialized_params(sp.get("m_Parameters", {}), names)
    if params is None:
        _, _, blob = rdef.parse_blob_params(entry.tail)
    else:
        blob = rdef.parse_params(params[4:])  # after the version word
    # A slot the variant binds itself takes precedence over a common binding.
    taken = {slot: name for name, slot in {**own.cb_slots, **blob.cb_slots}.items()}
    common.cb_slots = {n: s for n, s in common.cb_slots.items() if taken.get(s, n) == n}
    return common.merge(own).merge(blob)


class Unsupported(Exception):
    pass


def program_dxbc(entry: BlobEntry, refl: rdef.Reflection, pad_blocks: bool) -> bytes:
    """DXBC for a D3D11 subprogram with its reflection chunk restored."""
    dxbc = entry.dxbc()
    if refl.buffers:
        # Structured buffers/UAVs need SSBOs (GL 4.3); GLCore 3.2 has none.
        raise Unsupported("structured buffers: " + ", ".join(refl.buffers))
    return rdef.with_rdef(dxbc, rdef.build_rdef(refl, rdef.version_token(dxbc), pad_blocks))


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


LEGACY, LAYOUT_2020, LAYOUT_2021 = "legacy", "2020", "2021.2"

# Constant buffers of the built-in pipeline's UnityShaderVariables.cginc (and
# UIElements). Unity's GL compiler flattens a shader's cbuffers into plain
# uniforms when it uses these includes; SRP includes keep cbuffers (in
# practice UnityPerDraw, which they share with the built-in includes).
BUILTIN_CBUFFERS = {"UnityPerCamera", "UnityPerCameraRare", "UnityPerFrame", "UnityLighting", "UnityShadows",
                    "UnityLightmaps", "UnityReflectionProbes", "UnityProbeVolume", "UnityFog", "UnityTerrain",
                    "UnityPerDrawSprite", "UnityStereoGlobals", "UnityStereoEyeIndex", "UnityStereoEyeIndices",
                    "UIClipRects", "UITransforms"}


def _uses_uniform_blocks(tree: dict) -> bool:
    """2021.2+: whether Unity's GL compiler would keep this shader's cbuffers (an SRP shader)."""
    for ps in _passes(tree["m_ParsedForm"]):
        names = {i: n for n, i in ps["m_NameIndices"]}
        for key, prog in ps.items():
            if key.startswith("prog") and isinstance(prog, dict) and any(
                    names.get(cb["m_NameIndex"]) in BUILTIN_CBUFFERS
                    for cb in prog.get("m_CommonParameters", {}).get("m_ConstantBuffers", [])):
                return False
    return True


def shader_layout(tree: dict) -> str:
    progs = [ps["progVertex"] for ps in _passes(tree["m_ParsedForm"])]
    if any("m_PlayerSubPrograms" in p for p in progs):
        return LAYOUT_2021
    if any("m_CommonParameters" in p for p in progs):
        return LAYOUT_2020
    return LEGACY


@dataclass
class Job:
    ps: dict
    vert: Sub
    frag: Sub
    index: int  # translator job
    block_sizes: dict[str, int]  # D3D11 size of each named cbuffer either stage binds


@dataclass
class ShaderPlan:
    tree: dict
    d3d: list[bytes]
    entries: LazyEntries
    record: int
    layout: str
    jobs: list[Job] = field(default_factory=list)
    skipped: set = field(default_factory=set)


def plan_shader(tree: dict, translator: Translator) -> ShaderPlan | None:
    platforms = tree.get("platforms") or []
    if PLATFORM_GLCORE in platforms or PLATFORM_D3D11 not in platforms:
        return None
    d3d, record = split_blob(platform_segments(tree, PLATFORM_D3D11))
    plan = ShaderPlan(tree, d3d, LazyEntries(d3d), record, shader_layout(tree))
    uniform_blocks = plan.layout == LAYOUT_2021 and _uses_uniform_blocks(tree)

    for ps in _passes(tree["m_ParsedForm"]):
        if any(sub.sp["m_GpuProgramType"] in DX11_OTHER_STAGES
               for prog in ("progGeometry", "progHull", "progDomain")
               for sub in subprograms(ps.get(prog, {}))):
            continue
        names = {i: n for n, i in ps["m_NameIndices"]}
        verts = [s for s in subprograms(ps["progVertex"]) if s.sp["m_GpuProgramType"] in DX11_VERTEX]
        frags = [s for s in subprograms(ps["progFragment"]) if s.sp["m_GpuProgramType"] in DX11_PIXEL]
        # D3D11 compiles each stage separately, dropping keywords the stage does
        # not use, so the stages have different variant sets. GL needs one
        # program per combination: pair every vertex and fragment variant that
        # agree on the keywords both stages know about. This reproduces the
        # variant set of Unity's own GLCore builds exactly.
        vert_kws = frozenset().union(*(_keywords(s.sp) for s in verts))
        frag_kws = frozenset().union(*(_keywords(s.sp) for s in frags))
        cache: dict[tuple, tuple[bytes, dict] | Unsupported] = {}

        def dxbc_for(sub: Sub, prog: str) -> tuple[bytes, dict]:
            key = (prog, sub.sp["m_BlobIndex"], sub.params)
            if key not in cache:
                entry = plan.entries[sub.sp["m_BlobIndex"]]
                params = None if sub.params is None else d3d[sub.params]
                refl = _reflection(entry, sub.sp, ps[prog], names, params)
                sizes = {n: cb.size for n, cb in refl.cbuffers.items() if n in refl.cb_slots}
                try:
                    cache[key] = (program_dxbc(entry, refl, uniform_blocks), sizes)
                except Unsupported as e:
                    cache[key] = e
            if isinstance(cache[key], Unsupported):
                raise cache[key]
            return cache[key]

        for v in verts:
            vk = _keywords(v.sp)
            for f in frags:
                fk = _keywords(f.sp)
                if f.tier != v.tier or vk & frag_kws != fk & vert_kws:
                    continue
                try:
                    (vs, vsizes), (fs, fsizes) = dxbc_for(v, "progVertex"), dxbc_for(f, "progFragment")
                except Unsupported as e:
                    plan.skipped.add(str(e))
                    continue
                plan.jobs.append(Job(ps, v, f, translator.add(vs, fs, uniform_blocks), {**vsizes, **fsizes}))
    return plan if plan.jobs else None


def apply_plan(plan: ShaderPlan, results: dict) -> tuple[int, int, set[str]]:
    """Write translated programs into the Shader typetree. Returns (ok, failed, errors)."""
    entries: list[BlobEntry | bytes] = []
    param_entries: dict[bytes, int] = {}
    ok = failed = 0
    errors = set()
    new_subprograms = []  # (prog, tier, sub-program, parameter entry index)
    gl_passes = []
    for job in plan.jobs:
        ps, v, f = job.ps, job.vert.sp, job.frag.sp
        glsl = results[job.index]
        vsrc = plan.entries[v["m_BlobIndex"]]
        source_map = v.get("m_Channels", {}).get("m_SourceMap", 0)
        if isinstance(glsl, bytes) and plan.layout != LEGACY:
            source_map = gl_source_map(glsl)
            try:
                if plan.layout == LAYOUT_2020:
                    tail = gl_tail_2020(glsl, source_map)
                else:
                    params = gl_params_2021(glsl, vsrc.version, job.block_sizes)
                    tail = struct.pack("<2I", source_map, 0)
            except ValueError as e:
                glsl = str(e)
        if not isinstance(glsl, bytes):
            failed += 1
            errors.add(glsl)
            continue
        ok += 1
        if plan.layout == LEGACY:
            tail = _legacy_gl_tail(source_map)
        fsrc = plan.entries[f["m_BlobIndex"]]
        combined, keywords, local = _combine(v, f, vsrc, fsrc)
        param_index = None
        if plan.layout == LAYOUT_2021:
            if params not in param_entries:
                param_entries[params] = len(entries)
                entries.append(params)
            param_index = param_entries[params]
        vi = len(entries)
        entries.append(BlobEntry(vsrc.version, GLCORE32, b"\0" * 16, keywords, local, glsl, tail))
        if plan.layout == LAYOUT_2021:
            sp = dict(combined, m_BlobIndex=vi, m_GpuProgramType=GLCORE32,
                      m_ShaderRequirements=v["m_ShaderRequirements"] | f["m_ShaderRequirements"])
        else:
            sp = _gl_subprogram(combined, vi, source_map)
        new_subprograms.append((ps["progVertex"], job.vert.tier, sp, param_index))
        if plan.layout == LEGACY:
            fi = len(entries)
            entries.append(BlobEntry(fsrc.version, GLCORE32, b"\0" * 16, keywords, local,
                                     b"", _legacy_gl_tail(0)))
            new_subprograms.append((ps["progFragment"], job.frag.tier, _gl_subprogram(dict(f, **{
                k: combined[k] for k in ("m_KeywordIndices", "m_GlobalKeywordIndices", "m_LocalKeywordIndices")
                if k in combined}), fi, 0), None))
        gl_passes.append(ps)
    if not entries:
        return ok, failed, errors
    for prog, tier, sp, param_index in new_subprograms:
        if param_index is None:
            prog["m_SubPrograms"].append(sp)
        else:
            prog["m_PlayerSubPrograms"][tier].append(sp)
            prog["m_ParameterBlobIndices"][tier].append(param_index)
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
    _append_platform(plan.tree, PLATFORM_GLCORE, build_segments(entries, plan.record))
    if plan.layout != LEGACY:
        _drop_d3d11(plan.tree)
    return ok, failed, errors


def _drop_d3d11(tree: dict) -> None:
    """Remove the D3D11 programs, leaving a GLCore-only shader.

    From 2020 the GLCore program covers both stages and has no fragment
    sub-program; if D3D11 fragment sub-programs remain, the player rejects
    the pass ("not supported on this GPU"). Official Linux builds also have
    empty m_CommonParameters (it holds D3D11 reflection data), and in 2021.2+
    an empty m_PlayerSubPrograms for programs without sub-programs.
    """
    for ps in _passes(tree["m_ParsedForm"]):
        for key, prog in ps.items():
            if not key.startswith("prog") or not isinstance(prog, dict):
                continue
            if "m_PlayerSubPrograms" in prog:
                tiers = [[(sp, p) for sp, p in zip(sps, params) if sp["m_GpuProgramType"] == GLCORE32]
                         for sps, params in zip(prog["m_PlayerSubPrograms"], prog["m_ParameterBlobIndices"])]
                if not any(tiers):
                    tiers = []
                prog["m_PlayerSubPrograms"] = [[sp for sp, _ in t] for t in tiers]
                prog["m_ParameterBlobIndices"] = [[p for _, p in t] for t in tiers]
            else:
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
    if "stageCounts" in tree:
        del tree["stageCounts"][tree["platforms"].index(PLATFORM_D3D11)]
    set_platforms(tree, [(p, None) for p in tree["platforms"] if p != PLATFORM_D3D11])


def convert_file(path: Path) -> tuple[int, int, int]:
    """Translate every D3D11-only shader in an assets file in place.

    Returns (shaders changed, programs translated, programs failed).
    """
    env = UnityPy.load(str(path))
    shaders = [(obj, obj.read_typetree()) for obj in env.objects if obj.type.name == "Shader"]
    if not shaders:
        return 0, 0, 0
    layouts = {shader_layout(t) for _, t in shaders}
    plans = []
    with tempfile.TemporaryDirectory() as tmp:
        translator = Translator(Path(tmp), explicit_locations=layouts != {LEGACY})
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
