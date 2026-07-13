"""Lightweight, dependency-free code-structure extraction for the knowledge graph.

Per code file we emit two structural relations that regex can extract reliably:

    repo --defines--> symbol:<function/class/type>   (detail = "in <path>")
    repo --imports--> module:<imported module>       (detail = "from <path>")

so "where is PaymentProcessor defined?" and "who imports stripe?" become graph
lookups whose evidence is the code file itself. Call-graph edges need cross-file
symbol resolution (tree-sitter / an LSP) and are intentionally out of scope here —
this is the cheap, robust structural layer that composes with the dependency-map
extractor in connectors/deps.py.

Pure and testable (same shape as deps.py): (text, uri, repo_id) -> (entities, edges).
Entities are (id, name, type) 3-tuples; edges are (src, rel, dst, detail) 4-tuples,
matching what the ingest pipeline persists via catalog.replace_doc_edges.
"""

from __future__ import annotations

import re

# Cap symbols/imports per file so a generated or vendored megafile can't explode
# the graph. Real source files sit well under this.
_MAX = 80

CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".cs", ".go",
}


def looks_like_code(kind: str, uri: str) -> bool:
    """Whether a document should get code-structure extraction — connector-set
    kind=='code' or a recognized source extension (so any connector's code
    documents qualify, not just ones that bothered to set kind)."""
    if (kind or "").lower() == "code":
        return True
    return _ext(uri) in CODE_EXTS


def _ext(uri: str) -> str:
    path = uri.split("?")[0].split("#")[0].rstrip("/")
    dot, slash = path.rfind("."), max(path.rfind("/"), path.rfind("\\"))
    return path[dot:].lower() if dot > slash else ""


def _short_path(uri: str) -> str:
    return uri.split("?")[0].split("#")[0][-80:]


def _uniq(seq) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- per-language

def _python(text: str) -> tuple[list[str], list[str]]:
    defines = [d for d in re.findall(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", text, re.M)
               if not d.startswith("__")]
    defines += re.findall(r"^\s*class\s+([A-Za-z_]\w*)", text, re.M)
    imports: list[str] = []
    for mod in re.findall(r"^\s*import\s+([A-Za-z_][\w.]*)", text, re.M):
        imports.append(mod.split(".")[0])
    for dots, mod in re.findall(r"^\s*from\s+(\.*)([A-Za-z_][\w.]*)?\s+import", text, re.M):
        if not dots and mod:  # skip relative imports (local, not shared entities)
            imports.append(mod.split(".")[0])
    return defines, imports


def _jsts(text: str) -> tuple[list[str], list[str]]:
    defines = re.findall(r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text, re.M)
    defines += re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text, re.M)
    defines += re.findall(r"^\s*export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", text, re.M)
    defines += re.findall(r"^\s*class\s+([A-Za-z_$][\w$]*)", text, re.M)
    imports: list[str] = []
    for spec in re.findall(r"""import\s+[^'"]*?from\s+['"]([^'"]+)['"]""", text):
        imports.append(_js_pkg(spec))
    for spec in re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", text):
        imports.append(_js_pkg(spec))
    for spec in re.findall(r"""^\s*import\s+['"]([^'"]+)['"]""", text, re.M):  # side-effect import
        imports.append(_js_pkg(spec))
    return defines, imports


def _js_pkg(spec: str) -> str:
    if spec.startswith((".", "/")):  # local/relative import
        return ""
    if spec.startswith("@"):  # scoped: @scope/name
        return "/".join(spec.split("/")[:2])
    return spec.split("/")[0]


def _jvm(text: str) -> tuple[list[str], list[str]]:  # Java + Kotlin
    defines = re.findall(
        r"\b(?:public|private|protected|final|abstract|sealed|open|internal|data|static)?\s*"
        r"(?:class|interface|enum|object|record)\s+([A-Za-z_]\w*)", text)
    defines += re.findall(r"^\s*(?:public|private|internal|protected)?\s*fun\s+([A-Za-z_]\w*)", text, re.M)
    imports = [_jvm_pkg(i) for i in re.findall(r"^\s*import\s+(?:static\s+)?([\w.]+)", text, re.M)]
    return defines, imports


def _jvm_pkg(imp: str) -> str:
    imp = imp.rstrip(";").strip().rstrip(".*").strip()  # drop trailing wildcard/dots
    parts = imp.split(".")
    if parts and parts[-1][:1].isupper():  # trailing ClassName -> keep the package
        parts = parts[:-1]
    return ".".join(parts)


def _csharp(text: str) -> tuple[list[str], list[str]]:
    defines = re.findall(
        r"\b(?:public|private|protected|internal|sealed|abstract|static|partial)?\s*"
        r"(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)", text)
    imports = [i.strip() for i in re.findall(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;", text, re.M)]
    return defines, imports


def _go(text: str) -> tuple[list[str], list[str]]:
    defines = re.findall(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", text, re.M)
    defines += re.findall(r"^\s*type\s+([A-Za-z_]\w*)\s", text, re.M)
    imports: list[str] = []
    for block in re.findall(r"import\s*\((.*?)\)", text, re.S):  # grouped imports
        imports += re.findall(r'"([^"]+)"', block)
    imports += re.findall(r'^\s*import\s+"([^"]+)"', text, re.M)  # single import
    return defines, imports


_HANDLERS = {
    ".py": _python, ".pyi": _python,
    ".js": _jsts, ".jsx": _jsts, ".ts": _jsts, ".tsx": _jsts, ".mjs": _jsts, ".cjs": _jsts,
    ".java": _jvm, ".kt": _jvm, ".kts": _jvm,
    ".cs": _csharp,
    ".go": _go,
}


def extract_code_graph(text: str, uri: str, repo_id: str) -> tuple[list[tuple], list[tuple]]:
    """(entities, edges) linking `repo_id` to the symbols it defines and modules it
    imports. Returns empty lists for non-code / unrecognized files. The caller adds
    the repo entity and supplies the evidence document id."""
    handler = _HANDLERS.get(_ext(uri))
    if handler is None:
        return [], []
    try:
        defines, imports = handler(text)
    except Exception:  # a pathological file must never break ingestion
        return [], []
    defines = _uniq(defines)[:_MAX]
    imports = _uniq(m for m in imports if m)[:_MAX]
    if not defines and not imports:
        return [], []

    path = _short_path(uri)
    entities: list[tuple] = []
    edges: list[tuple] = []
    for name in defines:
        sid = f"symbol:{name.lower()}"
        entities.append((sid, name, "symbol"))
        edges.append((repo_id, "defines", sid, f"in {path}"))
    for mod in imports:
        mid = f"module:{mod.lower()}"
        entities.append((mid, mod, "module"))
        edges.append((repo_id, "imports", mid, f"from {path}"))
    return entities, edges
