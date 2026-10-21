"""Rebuild the DXBC RDEF (reflection) chunk that Unity strips from its shaders.

Unity keeps a D3D11 program's reflection data outside the DXBC container
(constant buffer layouts, texture/sampler slots) and drops the RDEF chunk.
HLSLcc needs RDEF to name uniforms and textures, and crashes without it, so
we synthesize one.

Where Unity keeps the reflection data depends on the version:
  2017-2019: on each SerializedSubProgram in the parsed form
             (m_ConstantBuffers, m_TextureParams, ... with m_NameIndex
             pointing into the pass's m_NameIndices).
  2020:      shared parts in prog*.m_CommonParameters, the rest per variant
             in the blob entry after the program code (see parse_blob_params).
  2021.2+:   as 2020, but the per-variant part is a separate blob entry
             (see parse_params).
Both are normalized into a Reflection before building RDEF.
"""

import struct
from dataclasses import dataclass, field

# D3D_SHADER_INPUT_TYPE
SIT_CBUFFER, SIT_TEXTURE, SIT_SAMPLER = 0, 2, 3
# D3D_SHADER_VARIABLE_CLASS / TYPE
SVC_SCALAR, SVC_VECTOR, SVC_MATRIX_COLUMNS = 0, 1, 3
SVT_BOOL, SVT_INT, SVT_FLOAT, SVT_UINT = 1, 2, 3, 19
SVF_USED = 2
RETURN_TYPE_FLOAT = 5
# Unity TextureDimension -> D3D_SRV_DIMENSION
TEX_DIM = {2: 4, 3: 8, 4: 9, 5: 5, 6: 10}
TEX_DIM_MS = {2: 6, 5: 7}
# Unity ShaderParamType (Float, Int, Bool, Half, Short, UInt) -> SVT
PARAM_TYPE = {0: SVT_FLOAT, 1: SVT_INT, 2: SVT_BOOL, 3: SVT_FLOAT, 4: SVT_INT, 5: SVT_UINT}

# Binding kinds in blob parameter records (2 = structured buffer, as seen on
# unity_ParticleInstanceData; 4 = inline sampler state, nameless, seen in 2021.2+;
# 3 assumed)
BIND_TEXTURE, BIND_CBUFFER, BIND_BUFFER, BIND_UAV, BIND_SAMPLER = 0, 1, 2, 3, 4


@dataclass
class Var:
    offset: int
    param_type: int  # Unity ShaderParamType
    rows: int
    cols: int
    is_matrix: bool
    elements: int


@dataclass
class CBuffer:
    size: int = 0
    vars: dict[str, Var] = field(default_factory=dict)


@dataclass
class Texture:
    slot: int
    sampler: int
    dim: int  # Unity TextureDimension
    multisampled: bool


@dataclass
class Reflection:
    cbuffers: dict[str, CBuffer] = field(default_factory=dict)
    cb_slots: dict[str, int] = field(default_factory=dict)
    textures: dict[str, Texture] = field(default_factory=dict)
    samplers: dict[int, str] = field(default_factory=dict)
    buffers: dict[str, int] = field(default_factory=dict)  # structured buffers / UAVs (unsupported)

    def merge(self, other: "Reflection") -> "Reflection":
        out = Reflection()
        for src in (self, other):
            for name, cb in src.cbuffers.items():
                dst = out.cbuffers.setdefault(name, CBuffer())
                dst.size = max(dst.size, cb.size)
                dst.vars.update(cb.vars)
            out.cb_slots.update(src.cb_slots)
            out.textures.update(src.textures)
            out.samplers.update(src.samplers)
            out.buffers.update(src.buffers)
        return out


def from_serialized_params(params: dict, names: dict[int, str]) -> Reflection:
    """Reflection from parsed-form params (a 2017-2019 SerializedSubProgram,
    or a 2020 m_Parameters / m_CommonParameters dict)."""
    r = Reflection()
    for cb in params.get("m_ConstantBuffers", []):
        c = r.cbuffers.setdefault(names[cb["m_NameIndex"]], CBuffer())
        c.size = max(c.size, cb["m_Size"])
        for m in cb["m_MatrixParams"]:
            rows = m["m_RowCount"]
            c.vars[names[m["m_NameIndex"]]] = Var(m["m_Index"], m["m_Type"], rows,
                                                  m.get("m_ColumnCount", 4), True, m["m_ArraySize"])
        for v in cb["m_VectorParams"]:
            c.vars[names[v["m_NameIndex"]]] = Var(v["m_Index"], v["m_Type"], 1, v["m_Dim"], False,
                                                  v["m_ArraySize"])
    for b in params.get("m_ConstantBufferBindings", []):
        r.cb_slots[names[b["m_NameIndex"]]] = b["m_Index"]
    for t in params.get("m_TextureParams", []):
        name = names[t["m_NameIndex"]]
        r.textures[name] = Texture(t["m_Index"], t.get("m_SamplerIndex", -1), t["m_Dim"],
                                   bool(t.get("m_MultiSampled")))
    for b in params.get("m_BufferParams", []) + params.get("m_UAVParams", []):
        r.buffers[names[b["m_NameIndex"]]] = b["m_Index"]
    for s in params.get("m_Samplers", []):
        r.samplers.setdefault(s["bindPoint"], f"inlineSampler{s['bindPoint']}")
    return r


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return v

    def string(self) -> str:
        n = self.u32()
        s = self.data[self.pos:self.pos + n].decode("utf-8")
        self.pos += (n + 3) & ~3
        return s


def parse_blob_params(tail: bytes) -> tuple[int, list[tuple[int, int]], Reflection]:
    """Parse the per-variant parameter block that follows the code in a 2020 blob entry.

    Layout:
      u32 sourceMap; u32 n; n * (u32 source, u32 target)       vertex channels
      parameters (see parse_params)
    """
    rd = _Reader(tail)
    source_map = rd.u32()
    channels = [(rd.u32(), rd.u32()) for _ in range(rd.u32())]
    return source_map, channels, _read_params(rd)


def parse_params(data: bytes) -> Reflection:
    """Parse a parameter record: the rest of a 2020 tail, or a 2021.2+ parameter blob entry
    after its version word.

    Layout:
      u32 groups; groups * {
          string name; u32 usedSize; u32 n;
          n * {string name; u32 type, rows, cols, isMatrix, arraySize, offset}
          u32 structCount }                                     group 0 = globals
      u32 bindings; bindings * {
          string name; u32 kind, index, extra; [u32 dim<<1|ms if kind == texture] }
    """
    return _read_params(_Reader(data))


def _read_params(rd: _Reader) -> Reflection:
    r = Reflection()
    for _ in range(rd.u32()):
        name = rd.string()
        used = rd.u32()
        cb = CBuffer(used)
        for _ in range(rd.u32()):
            pname = rd.string()
            ptype, rows, cols, is_matrix, arr, offset = (rd.u32() for _ in range(6))
            cb.vars[pname] = Var(offset, ptype, rows, cols, bool(is_matrix), arr)
        if rd.u32():
            raise ValueError("struct parameters are not supported")
        if name:
            r.cbuffers[name] = cb
        elif cb.vars:
            raise ValueError("global (non-cbuffer) parameters are not supported")
    for _ in range(rd.u32()):
        name = rd.string()
        kind, index, extra = rd.u32(), rd.u32(), rd.u32()
        if kind == BIND_TEXTURE:
            dim = rd.u32()
            r.textures[name] = Texture(index, extra, dim >> 1, bool(dim & 1))
        elif kind == BIND_CBUFFER:
            r.cb_slots[name] = index
        elif kind == BIND_SAMPLER:
            r.samplers.setdefault(index, name or f"inlineSampler{index}")
        elif kind in (BIND_BUFFER, BIND_UAV):
            r.buffers[name] = index
    return r


def _var_size(rows: int, cols: int, elements: int) -> int:
    """Bytes a variable occupies under HLSL cbuffer packing."""
    last = (rows - 1) * 16 + cols * 4
    return (max(elements, 1) - 1) * 16 * rows + last


class _Writer:
    def __init__(self):
        self.parts: list[bytes] = []
        self.size = 0
        self.strings: dict[str, list[int]] = {}  # name -> fixup positions

    def put(self, data: bytes) -> int:
        pos = self.size
        self.parts.append(data)
        self.size += len(data)
        return pos

    def string_ref(self, name: str) -> bytes:
        # Placeholder patched in finish(); remember where it goes.
        self.strings.setdefault(name, []).append(self.size)
        return b"\0\0\0\0"

    def finish(self) -> bytes:
        buf = bytearray(b"".join(self.parts))
        for name, fixups in self.strings.items():
            off = len(buf)
            buf += name.encode() + b"\0"
            for pos in fixups:
                buf[pos:pos + 4] = struct.pack("<I", off)
        while len(buf) % 4:
            buf += b"\0"
        return bytes(buf)


# Prefix of the filler variables build_rdef(pad_blocks=True) adds
PAD_PREFIX = "unityport_pad"


def _padded(vs: list) -> list:
    """Fill the gaps between cbuffer variables (sorted by offset) with filler variables.

    Unity's reflection lists only the variables a program uses. HLSLcc declares
    uniform blocks from RDEF without gaps, so std140 would move every variable
    after a gap; fillers keep each one at its D3D offset.
    """
    out = []
    end = 0
    for var in vs:
        offset = var[1]
        while end < offset:
            if end % 16 or offset - end < 16:
                out.append((f"{PAD_PREFIX}{end}", end, SVC_SCALAR, SVT_FLOAT, 1, 1, 0))
                end += 4
            else:
                n = (offset - end) // 16
                out.append((f"{PAD_PREFIX}{end}", end, SVC_VECTOR, SVT_FLOAT, 1, 4, n if n > 1 else 0))
                end += 16 * n
        out.append(var)
        end = max(end, offset + _var_size(var[4], var[5], var[6]))
    return out


def build_rdef(refl: Reflection, version_token: int, pad_blocks: bool = False) -> bytes:
    """Build an RDEF chunk body from normalized reflection data.

    pad_blocks: fill gaps in named cbuffers (all but $Globals), which HLSLcc
    turns into std140 uniform blocks when asked to.
    """
    major = (version_token >> 4) & 0xF
    w = _Writer()

    # Resource bindings: constant buffers, textures, samplers.
    # Only cbuffers with a known slot are bound; others are not used by this program.
    cbs = [(name, cb) for name, cb in refl.cbuffers.items() if name in refl.cb_slots]
    bindings = []  # (name, type, return, dim, samples, point)
    for name, _ in cbs:
        bindings.append((name, SIT_CBUFFER, 0, 0, 0, refl.cb_slots[name]))
    samplers = dict(refl.samplers)
    for name, tex in refl.textures.items():
        dim = (TEX_DIM_MS if tex.multisampled else TEX_DIM).get(tex.dim, 4)
        bindings.append((name, SIT_TEXTURE, RETURN_TYPE_FLOAT, dim, 0xFFFFFFFF, tex.slot))
        if 0 <= tex.sampler < 0x10000:
            samplers.setdefault(tex.sampler, "sampler" + name)
    for point, name in sorted(samplers.items()):
        bindings.append((name, SIT_SAMPLER, 0, 0, 0, point))

    header_size = 28
    binding_size = 32
    cb_desc_size = 24
    var_desc_size = 40 if major >= 5 else 24

    bindings_off = header_size
    cbs_off = bindings_off + binding_size * len(bindings)
    vars_off = cbs_off + cb_desc_size * len(cbs)

    cb_vars = []
    for cb_name, cb in cbs:
        vs = []
        for name, v in cb.vars.items():
            if v.is_matrix:
                cls = SVC_MATRIX_COLUMNS
            else:
                cls = SVC_SCALAR if v.cols == 1 else SVC_VECTOR
            vs.append((name, v.offset, cls, PARAM_TYPE.get(v.param_type, SVT_FLOAT),
                       v.rows if v.is_matrix else 1, v.cols, v.elements))
        vs.sort(key=lambda x: x[1])
        cb_vars.append(_padded(vs) if pad_blocks and not cb_name.startswith("$") else vs)
    types_off = vars_off + var_desc_size * sum(len(v) for v in cb_vars)

    # Header
    w.put(struct.pack("<I", len(cbs)))
    w.put(struct.pack("<I", cbs_off if cbs else 0))
    w.put(struct.pack("<I", len(bindings)))
    w.put(struct.pack("<I", bindings_off))
    w.put(struct.pack("<I", (version_token & 0xFFFF0000) | (major << 8) | (version_token & 0xF)))
    w.put(struct.pack("<I", 0))
    w.put(w.string_ref("unityport"))

    for name, typ, ret, dim, samples, point in bindings:
        w.put(w.string_ref(name))
        w.put(struct.pack("<7I", typ, ret, dim, samples, point, 1, 0))

    var_index = 0
    for (name, cb), vs in zip(cbs, cb_vars):
        w.put(w.string_ref(name))
        w.put(struct.pack("<5I", len(vs), vars_off + var_desc_size * var_index, cb.size, 0, 0))
        var_index += len(vs)

    type_index = 0
    for vs in cb_vars:
        for name, offset, cls, typ, rows, cols, elements in vs:
            w.put(w.string_ref(name))
            size = _var_size(rows, cols, elements)
            w.put(struct.pack("<4I", offset, size, SVF_USED, types_off + 16 * type_index))
            w.put(struct.pack("<I", 0))  # default value
            if major >= 5:
                w.put(struct.pack("<4I", 0xFFFFFFFF, 0, 0xFFFFFFFF, 0))
            type_index += 1

    for vs in cb_vars:
        for name, offset, cls, typ, rows, cols, elements in vs:
            w.put(struct.pack("<6HI", cls, typ, rows, cols, elements, 0, 0))

    return w.finish()


def _chunks(dxbc: bytes) -> list[tuple[bytes, bytes]]:
    (count,) = struct.unpack_from("<I", dxbc, 28)
    out = []
    for i in range(count):
        (off,) = struct.unpack_from("<I", dxbc, 32 + 4 * i)
        fourcc = dxbc[off:off + 4]
        (size,) = struct.unpack_from("<I", dxbc, off + 4)
        out.append((fourcc, dxbc[off + 8:off + 8 + size]))
    return out


def version_token(dxbc: bytes) -> int:
    for fourcc, data in _chunks(dxbc):
        if fourcc in (b"SHDR", b"SHEX"):
            return struct.unpack_from("<I", data, 0)[0]
    raise ValueError("DXBC has no shader code chunk")


def with_rdef(dxbc: bytes, rdef: bytes) -> bytes:
    """Return the DXBC container with its RDEF chunk replaced/added."""
    chunks = [(b"RDEF", rdef)] + [c for c in _chunks(dxbc) if c[0] != b"RDEF"]
    header_size = 32 + 4 * len(chunks)
    body = b""
    offsets = []
    for fourcc, data in chunks:
        offsets.append(header_size + len(body))
        body += fourcc + struct.pack("<I", len(data)) + data
    total = header_size + len(body)
    # Checksum left as in the original; HLSLcc does not verify it.
    head = dxbc[:20] + struct.pack("<III", 1, total, len(chunks)) + struct.pack(f"<{len(chunks)}I", *offsets)
    return head + body
