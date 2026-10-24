// Batch DXBC -> GLSL translator built on Unity's HLSLcc.
//
// Usage: hlslcc_tool [330] [--explicit-locations] < jobs
//
// Reads jobs from stdin, one per line, tab separated:
//   <vertex.dxbc>\t<fragment.dxbc>\t<output.glsl>[\tuniform-blocks]
// and writes each vertex+fragment pair as one Unity GLCore program:
//   #ifdef VERTEX ... #endif
//   #ifdef FRAGMENT ... #endif
// Both stages are translated with shared cross-dependency data so varyings
// line up. "uniform-blocks" declares named constant buffers as std140
// uniform blocks ($Globals stays plain uniforms), as Unity 2021.2+ does for
// SRP shaders. Prints "OK <output>" or "ERR <output> <message>" per job.

#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "hlslcc.h"

namespace {

struct Diagnostics : HLSLccReflection
{
    std::string errors;
    void OnDiagnostics(const std::string &error, int line, bool isError) override
    {
        if (isError)
            errors += error + "; ";
    }
};

bool ReadFile(const std::string &path, std::string &out)
{
    std::ifstream f(path, std::ios::binary);
    if (!f)
        return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    out = ss.str();
    return true;
}

// Flags matching the GLSL that Unity 2017's own GLCore compiler emits:
// in_POSITION0 / vs_TEXCOORD0 names, hlslcc_mtx4x4 matrix prefixes,
// sampler uniforms named after the texture, and plain
// (non-UBO) uniforms for constant buffers.
const unsigned int kFlags =
    HLSLCC_FLAG_INOUT_SEMANTIC_NAMES |
    HLSLCC_FLAG_TRANSLATE_MATRICES |
    HLSLCC_FLAG_DISABLE_GLOBALS_STRUCT |
    HLSLCC_FLAG_GLOBAL_CONSTS_NEVER_IN_UBO;

bool Translate(const std::string &dxbc, GLSLCrossDependencyData &deps, GLLang lang, unsigned int flags,
               std::string &glsl, std::string &error)
{
    GlExtensions ext = {};
    ext.ARB_explicit_attrib_location = 1;
    HLSLccSamplerPrecisionInfo precisions;
    Diagnostics diag;
    GLSLShader result;
    int ok = TranslateHLSLFromMem(dxbc.data(), flags, lang, &ext, &deps, precisions, diag, &result);
    if (!ok)
    {
        error = diag.errors.empty() ? "translation failed" : diag.errors;
        return false;
    }
    glsl = result.sourceCode;
    return true;
}

} // namespace

int main(int argc, char **argv)
{
    // Options: "330" for GLSL 330; "--explicit-locations" for UNITY_LOCATION(n)
    // on samplers, as Unity 2020's GLCore compiler emits.
    GLLang lang = LANG_150;
    unsigned int flags = kFlags;
    for (int i = 1; i < argc; i++)
    {
        std::string arg = argv[i];
        if (arg == "330")
            lang = LANG_330;
        else if (arg == "--explicit-locations")
            flags |= HLSLCC_FLAG_FORCE_EXPLICIT_LOCATIONS;
    }

    std::string line;
    while (std::getline(std::cin, line))
    {
        if (!line.empty() && line.back() == '\r')
            line.pop_back();
        std::vector<std::string> parts;
        std::stringstream ss(line);
        std::string part;
        while (std::getline(ss, part, '\t'))
            parts.push_back(part);
        // Per-job options after the three paths: "uniform-blocks", and a GLSL version
        // ("330", "410", "420") overriding the default
        GLLang jobLang = lang;
        unsigned int jobFlags = flags;
        bool bad = parts.size() < 3;
        for (size_t i = 3; i < parts.size(); i++)
        {
            if (parts[i] == "uniform-blocks")
                jobFlags |= HLSLCC_FLAG_UNIFORM_BUFFER_OBJECT;
            else if (parts[i] == "330")
                jobLang = LANG_330;
            else if (parts[i] == "410")
                jobLang = LANG_410;
            else if (parts[i] == "420")
                jobLang = LANG_420;
            else
                bad = true;
        }
        if (bad)
        {
            std::cout << "ERR ? bad job line" << std::endl;
            continue;
        }
        const std::string &out = parts[2];

        std::string vs, fs;
        if (!ReadFile(parts[0], vs) || !ReadFile(parts[1], fs))
        {
            std::cout << "ERR " << out << " cannot read input" << std::endl;
            continue;
        }

        GLSLCrossDependencyData deps;
        std::string vsGlsl, fsGlsl, error;
        // Fragment shader first, as HLSLcc expects: starting it clears deps, and the vertex
        // shader then takes from it the interpolation of its outputs ("flat" for
        // nointerpolation) and, from GLSL 4.1 on, their locations. Both must match the
        // fragment inputs; D3D11 links the stages by semantic, so a vertex shader may write
        // more or differently ordered outputs than the fragment shader reads.
        if (!Translate(fs, deps, jobLang, jobFlags, fsGlsl, error) || !Translate(vs, deps, jobLang, jobFlags, vsGlsl, error))
        {
            std::cout << "ERR " << out << " " << error << std::endl;
            continue;
        }

        std::ofstream o(out, std::ios::binary);
        o << "#ifdef VERTEX\n" << vsGlsl << "\n#endif\n#ifdef FRAGMENT\n" << fsGlsl << "\n#endif\n";
        std::cout << "OK " << out << std::endl;
    }
    return 0;
}
