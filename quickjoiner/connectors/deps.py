"""Dependency mapping: turn package manifests in an ingested tree into one
synthesized, retrieval-friendly document per repo.

Manifests encode the strongest evidence of how repos relate — "A references B's
package" — but as sparse XML/JSON attributes that neither embeddings nor an LLM
reliably surface from raw chunks. Covered ecosystems:
- .NET: `.csproj`/`.vbproj`/`.fsproj` (PackageReference, old-style Reference,
  ProjectReference, PackageId), `packages.config`, `.nuspec`
- Java: `pom.xml` (properties interpolation, parent, modules, dependencyManagement),
  `build.gradle`(.kts) (string/map/project notations), `settings.gradle`(.kts),
  `gradle/libs.versions.toml` version catalogs
- Node/React/Angular: `package.json` (dependencies/dev/peer/optional + workspaces),
  `angular.json` projects
- Python: `pyproject.toml` (PEP 621 + optional-dependencies + Poetry incl. groups),
  `requirements*.txt`, `Pipfile`, `setup.cfg`, `setup.py`
- Go: `go.mod` `dependency_document()`
rewrites that structure as dense natural-language statements ("<repo> depends on
AppRiver.Nautical.Models 3.2.0") so the link is retrievable from either end and
citable like any other document.

Semantic aliasing: orgs speak about dotted packages by their suffix — the org
prefix is dropped and the rest is spoken with spaces (`AppRiver.Nautical` -> "nautical",
`AppRiver.Nautical.Models` -> "nautical models"). `aliases()` generates those forms
and the document embeds them ("also referred to as: ..."), so loose questions still
retrieve the right sources via the sparse leg and embeddings. Third-party packages
are only aliased when they look org-internal (>= 3 segments, or first segment matches
an org prefix seen in the repo) to keep alias noise out of citable text.

All functions are module-level and pure (path in, Documents out) per the connector
convention, so tests exercise them on temp trees without git or HTTP.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from quickjoiner.connectors.base import Document

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".idea", ".vs"}
_MAX_MANIFEST_BYTES = 512_000
_SPLIT = re.compile(r"[.\-_]")
_SPLIT_FULL = re.compile(r"[.\-_/@:]")  # also breaks maven coords, npm scopes, go paths
_REQ_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*")

# Segments too generic to count as org vocabulary when deciding whether a
# dependency looks org-internal (and so deserves alias text).
_GENERIC_TOKENS = {
    "com", "org", "net", "io", "www", "github", "gitlab", "bitbucket",
    "core", "common", "api", "app", "apps", "web", "ui", "client", "server",
    "service", "services", "lib", "libs", "src", "main", "test", "tests",
    "tools", "utils", "shared", "models", "model", "docs", "data", "base",
}


def _alias_base(name: str) -> str:
    """The part of an identifier people actually say: strip npm scope /
    Go module path (keep the last path segment) and Maven groupId."""
    base = name.lstrip("@")
    if "/" in base:
        base = base.rsplit("/", 1)[-1]
    if ":" in base:
        base = base.rsplit(":", 1)[-1]
    return base


def _name_tokens(name: str) -> set[str]:
    """Non-generic vocabulary of a full identifier (scope/group included)."""
    return {p for p in _SPLIT_FULL.split(name.lower()) if p and p not in _GENERIC_TOKENS}


def aliases(identifier: str, drop_prefix: bool = True) -> list[str]:
    """Colloquial forms of a dotted/hyphenated identifier, lowercased.

    With drop_prefix (the org convention): AppRiver.Nautical.Models ->
    ["nautical models", "nautical.models", "appriver nautical models"];
    AppRiver.Nautical -> ["nautical", "appriver nautical"].
    Without it (plain repo names): proj-a-service -> ["proj a service"].
    """
    parts = [p.lower() for p in _SPLIT.split(identifier) if p]
    if len(parts) < 2:
        return []
    forms: list[str] = []
    if drop_prefix:
        rest = parts[1:]
        forms += [" ".join(rest), ".".join(rest)]
    forms.append(" ".join(parts))
    out: list[str] = []
    for f in forms:
        if f and f != identifier.lower() and f not in out:
            out.append(f)
    return out


@dataclass
class Dep:
    ecosystem: str  # NuGet | assembly | npm | Python | Go | Maven
    name: str
    version: str = ""
    referenced_by: list[str] = field(default_factory=list)


@dataclass
class Provided:
    name: str
    kind: str  # e.g. "NuGet package id", "npm package", "project/assembly"
    declared_in: str


@dataclass
class _Scan:
    provides: list[Provided] = field(default_factory=list)
    deps: dict[tuple[str, str, str], Dep] = field(default_factory=dict)  # (eco, name, ver)
    project_refs: list[tuple[str, str]] = field(default_factory=list)  # (manifest, target)

    def add_dep(self, ecosystem: str, name: str, version: str, referenced_by: str) -> None:
        name, version = name.strip(), (version or "").strip()
        if not name:
            return
        key = (ecosystem, name.lower(), version)
        dep = self.deps.setdefault(key, Dep(ecosystem, name, version))
        if referenced_by not in dep.referenced_by:
            dep.referenced_by.append(referenced_by)


def _local(tag: str) -> str:
    """Element tag without XML namespace (old-style csproj declares one)."""
    return tag.rsplit("}", 1)[-1]


def _xml_root(path: Path) -> ET.Element | None:
    try:
        return ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except (ET.ParseError, OSError):
        return None


# ------------------------------------------------------------- .NET manifests

def _parse_msbuild(path: Path, rel: str, scan: _Scan) -> None:
    root = _xml_root(path)
    if root is None:
        return
    package_id = assembly = ""
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "PackageReference":
            name = el.get("Include") or el.get("Update") or ""
            version = el.get("Version") or ""
            if not version:
                for child in el:
                    if _local(child.tag) == "Version":
                        version = (child.text or "").strip()
            scan.add_dep("NuGet", name, version, rel)
        elif tag == "ProjectReference":
            target = (el.get("Include") or "").replace("\\", "/")
            if target:
                scan.project_refs.append((rel, target))
        elif tag == "Reference":  # packages.config-era assembly reference
            name = (el.get("Include") or "").split(",")[0].strip()
            if name:
                scan.add_dep("assembly", name, "", rel)
        elif tag == "PackageId":
            package_id = (el.text or "").strip()
        elif tag in ("AssemblyName", "RootNamespace") and not assembly:
            assembly = (el.text or "").strip()
    if package_id:
        scan.provides.append(Provided(package_id, "NuGet package id", rel))
    scan.provides.append(Provided(assembly or path.stem, "project/assembly", rel))


def _parse_packages_config(path: Path, rel: str, scan: _Scan) -> None:
    root = _xml_root(path)
    if root is None:
        return
    for el in root.iter():
        if _local(el.tag) == "package":
            scan.add_dep("NuGet", el.get("id") or "", el.get("version") or "", rel)


def _parse_nuspec(path: Path, rel: str, scan: _Scan) -> None:
    root = _xml_root(path)
    if root is None:
        return
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "id" and (el.text or "").strip():
            scan.provides.append(Provided(el.text.strip(), "NuGet package id", rel))
        elif tag == "dependency":
            scan.add_dep("NuGet", el.get("id") or "", el.get("version") or "", rel)


# ------------------------------------------------------------ other ecosystems

def _parse_package_json(path: Path, rel: str, scan: _Scan) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("name"), str) and data["name"]:
        scan.provides.append(Provided(data["name"], "npm package", rel))
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            for name, version in block.items():
                scan.add_dep("npm", name, str(version), rel)
    ws = data.get("workspaces")
    patterns = ws if isinstance(ws, list) else ws.get("packages") if isinstance(ws, dict) else []
    for pattern in patterns or []:
        if isinstance(pattern, str):
            scan.project_refs.append((rel, f"workspace {pattern}"))


def _parse_angular_json(path: Path, rel: str, scan: _Scan) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return
    projects = data.get("projects") if isinstance(data, dict) else None
    if isinstance(projects, dict):
        for name in projects:
            scan.provides.append(Provided(name, "project/assembly", rel))


def _req_name(spec: str) -> str:
    m = _REQ_NAME.match(spec.strip())
    return m.group(0) if m else ""


def _add_pep508(scan: _Scan, spec, rel: str) -> None:
    if isinstance(spec, str) and _req_name(spec):
        scan.add_dep("Python", _req_name(spec), spec[len(_req_name(spec)):].strip(), rel)


def _parse_pyproject(path: Path, rel: str, scan: _Scan) -> None:
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return
    project = data.get("project", {})
    if isinstance(project.get("name"), str):
        scan.provides.append(Provided(project["name"], "Python package", rel))
    for spec in project.get("dependencies", []) or []:
        _add_pep508(scan, spec, rel)
    for specs in (project.get("optional-dependencies") or {}).values():
        for spec in specs or []:
            _add_pep508(scan, spec, rel)

    poetry = data.get("tool", {}).get("poetry", {})
    if isinstance(poetry.get("name"), str):
        scan.provides.append(Provided(poetry["name"], "Python package", rel))

    def _poetry_block(block) -> None:
        if not isinstance(block, dict):
            return
        for name, v in block.items():
            if name.lower() == "python":
                continue
            version = v if isinstance(v, str) else v.get("version", "") if isinstance(v, dict) else ""
            scan.add_dep("Python", name, "" if version == "*" else str(version), rel)

    _poetry_block(poetry.get("dependencies"))
    _poetry_block(poetry.get("dev-dependencies"))
    for group in (poetry.get("group") or {}).values():
        if isinstance(group, dict):
            _poetry_block(group.get("dependencies"))


def _parse_pipfile(path: Path, rel: str, scan: _Scan) -> None:
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return
    for section in ("packages", "dev-packages"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, v in block.items():
            version = v if isinstance(v, str) else v.get("version", "") if isinstance(v, dict) else ""
            scan.add_dep("Python", name, "" if version == "*" else str(version), rel)


def _parse_setup_cfg(path: Path, rel: str, scan: _Scan) -> None:
    import configparser

    cp = configparser.ConfigParser()
    try:
        cp.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except (configparser.Error, OSError):
        return
    name = cp.get("metadata", "name", fallback="").strip()
    if name:
        scan.provides.append(Provided(name, "Python package", rel))
    for line in cp.get("options", "install_requires", fallback="").splitlines():
        _add_pep508(scan, line.strip(), rel)


def _parse_setup_py(path: Path, rel: str, scan: _Scan) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = re.search(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", text)
    if m:
        scan.provides.append(Provided(m.group(1), "Python package", rel))
    block = re.search(r"install_requires\s*=\s*\[(.*?)\]", text, flags=re.DOTALL)
    if block:
        for spec in re.findall(r"['\"]([^'\"]+)['\"]", block.group(1)):
            _add_pep508(scan, spec, rel)


def _parse_requirements(path: Path, rel: str, scan: _Scan) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = _req_name(line)
        if name:
            scan.add_dep("Python", name, line[len(name):].strip(), rel)


def _parse_go_mod(path: Path, rel: str, scan: _Scan) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = re.search(r"^module\s+(\S+)", text, flags=re.MULTILINE)
    if m:
        scan.provides.append(Provided(m.group(1), "Go module", rel))
    block = re.search(r"require\s*\(([^)]*)\)", text, flags=re.DOTALL)
    entries = block.group(1).splitlines() if block else re.findall(r"^require\s+(.+)$", text, flags=re.MULTILINE)
    for entry in entries:
        parts = entry.split("//")[0].split()
        if len(parts) >= 2:
            scan.add_dep("Go", parts[0], parts[1], rel)


def _parse_pom(path: Path, rel: str, scan: _Scan) -> None:
    root = _xml_root(path)
    if root is None:
        return
    def _child(el, name):
        for c in el:
            if _local(c.tag) == name:
                return (c.text or "").strip()
        return ""

    props: dict[str, str] = {}
    for el in root:
        if _local(el.tag) == "properties":
            for p in el:
                props[_local(p.tag)] = (p.text or "").strip()

    parent = next((c for c in root if _local(c.tag) == "parent"), None)
    parent_group = parent_version = ""
    if parent is not None:
        parent_group, parent_version = _child(parent, "groupId"), _child(parent, "version")
        pa = _child(parent, "artifactId")
        if pa:
            scan.project_refs.append((rel, f"parent {parent_group}:{pa}".replace(" :", " ")))

    group = _child(root, "groupId") or parent_group
    version = _child(root, "version") or parent_version
    props.setdefault("project.version", version)
    props.setdefault("project.groupId", group)

    def _resolve(v: str) -> str:
        return re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), v or "")

    artifact = _child(root, "artifactId")
    if artifact:
        scan.provides.append(Provided(f"{group}:{artifact}".lstrip(":"), "Maven artifact", rel))
    for modules in (c for c in root if _local(c.tag) == "modules"):
        for module in modules:
            if _local(module.tag) == "module" and (module.text or "").strip():
                scan.project_refs.append((rel, f"module {module.text.strip()}"))
    for el in root.iter():
        if _local(el.tag) == "dependency":
            g, a, v = _child(el, "groupId"), _child(el, "artifactId"), _child(el, "version")
            if a:
                scan.add_dep("Maven", f"{_resolve(g)}:{a}".lstrip(":"), _resolve(v), rel)


# ------------------------------------------------------------------- Gradle

_GRADLE_CONF = (
    r"(?:implementation|api|compileOnly|compile|runtimeOnly|runtime|testImplementation|"
    r"testCompile|testRuntimeOnly|annotationProcessor|kapt|ksp|developmentOnly|providedCompile)"
)
_GRADLE_STR = re.compile(
    _GRADLE_CONF + r"""\s*\(?\s*['"]([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+)(?::([^'"]+))?['"]"""
)
_GRADLE_MAP = re.compile(
    _GRADLE_CONF + r"""\s*\(?\s*group\s*[:=]\s*['"]([^'"]+)['"]\s*,\s*name\s*[:=]\s*['"]([^'"]+)['"]"""
    r"""(?:\s*,\s*version\s*[:=]\s*['"]([^'"]+)['"])?"""
)
_GRADLE_PROJECT = re.compile(_GRADLE_CONF + r"""\s*\(?\s*project\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_GRADLE_ROOTNAME = re.compile(r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""")
_GRADLE_INCLUDE_LINE = re.compile(r"""^\s*include\b[^\n]*""", flags=re.MULTILINE)
_GRADLE_QUOTED = re.compile(r"""['"]:?([A-Za-z0-9_.\-:]+)['"]""")


def _parse_gradle_build(path: Path, rel: str, scan: _Scan) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for m in _GRADLE_STR.finditer(text):
        scan.add_dep("Gradle", f"{m.group(1)}:{m.group(2)}", m.group(3) or "", rel)
    for m in _GRADLE_MAP.finditer(text):
        scan.add_dep("Gradle", f"{m.group(1)}:{m.group(2)}", m.group(3) or "", rel)
    for m in _GRADLE_PROJECT.finditer(text):
        scan.project_refs.append((rel, m.group(1)))


def _parse_gradle_settings(path: Path, rel: str, scan: _Scan) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = _GRADLE_ROOTNAME.search(text)
    if m:
        scan.provides.append(Provided(m.group(1), "project/assembly", rel))
    for line in _GRADLE_INCLUDE_LINE.finditer(text):
        for q in _GRADLE_QUOTED.finditer(line.group(0)):
            scan.provides.append(Provided(q.group(1).lstrip(":"), "project/assembly", rel))


def _parse_version_catalog(path: Path, rel: str, scan: _Scan) -> None:
    """gradle/libs.versions.toml — modern Gradle's central dependency list."""
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return
    versions = {k: v for k, v in (data.get("versions") or {}).items() if isinstance(v, str)}
    for lib in (data.get("libraries") or {}).values():
        group = name = version = ""
        if isinstance(lib, str):  # "group:artifact:version"
            parts = lib.split(":")
            if len(parts) >= 2:
                group, name = parts[0], parts[1]
                version = parts[2] if len(parts) > 2 else ""
        elif isinstance(lib, dict):
            module = lib.get("module", "")
            if isinstance(module, str) and ":" in module:
                group, name = module.split(":", 1)
            else:
                group, name = str(lib.get("group", "")), str(lib.get("name", ""))
            v = lib.get("version", "")
            version = versions.get(v.get("ref", ""), "") if isinstance(v, dict) else str(v)
        if name:
            scan.add_dep("Gradle", f"{group}:{name}".lstrip(":"), version, rel)


_PARSERS = {
    ".csproj": _parse_msbuild, ".vbproj": _parse_msbuild, ".fsproj": _parse_msbuild,
    ".nuspec": _parse_nuspec,
}
_NAMED_PARSERS = {
    "packages.config": _parse_packages_config,
    "package.json": _parse_package_json,
    "angular.json": _parse_angular_json,
    "pyproject.toml": _parse_pyproject,
    "pipfile": _parse_pipfile,
    "setup.cfg": _parse_setup_cfg,
    "setup.py": _parse_setup_py,
    "go.mod": _parse_go_mod,
    "pom.xml": _parse_pom,
    "build.gradle": _parse_gradle_build,
    "build.gradle.kts": _parse_gradle_build,
    "settings.gradle": _parse_gradle_settings,
    "settings.gradle.kts": _parse_gradle_settings,
}


def is_manifest_path(uri: str) -> bool:
    """Whether `scan_tree` already deterministically parses this file for
    dependency edges — so it's redundant (and, for generated lockfiles, wasteful)
    to also spend an LLM triple-extraction call on it (see pipeline.py's gate).

    Uses suffix checks rather than isolating a clean basename: connector URIs mix
    "/"-separated paths with a "repo_url::relative/path" convention (git_repo.py),
    so a naive rsplit("/") can grab the wrong segment for a repo-root manifest —
    endswith() sidesteps that ambiguity entirely."""
    path = uri.split("?")[0].split("#")[0].rstrip("/").lower()
    if any(path.endswith(ext) for ext in _PARSERS):
        return True
    if any(path.endswith(name) for name in _NAMED_PARSERS):
        return True
    if re.search(r"(?:^|[/:])requirements[^/:]*\.txt$", path):
        return True
    if path.endswith(".versions.toml"):
        return True
    return False


def scan_tree(root: Path) -> _Scan:
    scan = _Scan()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        name = path.name.lower()
        parser = _PARSERS.get(path.suffix.lower()) or _NAMED_PARSERS.get(name)
        if parser is None and name.startswith("requirements") and name.endswith(".txt"):
            parser = _parse_requirements
        if parser is None and name.endswith(".versions.toml"):
            parser = _parse_version_catalog
        if parser is None:
            continue
        try:
            if path.stat().st_size > _MAX_MANIFEST_BYTES:
                continue
            parser(path, path.relative_to(root).as_posix(), scan)
        except Exception:
            continue  # one bad manifest shouldn't lose the rest
    return scan


def alias_forms(name: str, org_tokens: set[str], force: bool = False) -> list[str]:
    """Spoken forms for `name`, only when it looks org-internal: a dotted id with
    >= 3 segments, or sharing non-generic vocabulary with what this repo is/provides.
    Forms come from the spoken base (scope/group/path stripped), so
    "com.appriver:nautical-models" and "@appriver/nautical-models" both alias to
    "nautical models" while "junit:junit" and "github.com/lib/pq" stay silent."""
    base = _alias_base(name)
    base_parts = [p for p in _SPLIT.split(base.lower()) if p]
    internal = len(base_parts) >= 3 or bool(_name_tokens(name) & org_tokens)
    if not (force or internal):
        return []
    return aliases(base, drop_prefix="." in base)


def _alias_note(name: str, org_tokens: set[str], force: bool = False) -> str:
    forms = alias_forms(name, org_tokens, force)
    return f' — also referred to as: {", ".join(forms)}' if forms else ""


def _refs(dep: Dep) -> str:
    shown = dep.referenced_by[:3]
    extra = len(dep.referenced_by) - len(shown)
    return ", ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")


def entity_id(type_: str, name: str) -> str:
    return f"{type_}:{name.lower()}"


def _graph_meta(scan: _Scan, repo_name: str, org_tokens: set[str]) -> dict:
    """Structured entities/aliases/edges mirroring the document text, persisted
    into the knowledge graph by the ingest pipeline (evidence = this document)."""
    repo = entity_id("repo", repo_name)
    entities: dict[str, tuple[str, str, str]] = {repo: (repo, repo_name, "repo")}
    alias_rows: set[tuple[str, str]] = {
        (a, repo) for a in aliases(repo_name, drop_prefix="." in repo_name)
    }
    edges: list[tuple[str, str, str, str]] = []

    for prov in scan.provides:
        type_ = "project" if prov.kind == "project/assembly" else "package"
        pid = entity_id(type_, prov.name)
        entities.setdefault(pid, (pid, prov.name, type_))
        for form in alias_forms(prov.name, org_tokens, force=True):
            alias_rows.add((form, pid))
        edges.append((repo, "provides", pid, f"{prov.kind} ({prov.declared_in})"))

    for dep in scan.deps.values():
        pid = entity_id("package", dep.name)
        entities.setdefault(pid, (pid, dep.name, "package"))
        for form in alias_forms(dep.name, org_tokens):
            alias_rows.add((form, pid))
        version = f" {dep.version}" if dep.version else ""
        edges.append((repo, "depends_on", pid, f"{dep.ecosystem}{version} via {_refs(dep)}"))

    return {
        "entities": sorted(entities.values()),
        "aliases": sorted(alias_rows),
        "edges": edges,
    }


def dependency_document(root: Path, repo_name: str, uri_prefix: str) -> Document | None:
    """One markdown map of what `root` provides and depends on, or None if the
    tree has no recognizable manifests. `uri_prefix` follows the connector's
    document convention (e.g. the repo URL); the doc lands at `::dependency-map`."""
    scan = scan_tree(root)
    if not scan.provides and not scan.deps and not scan.project_refs:
        return None

    org_tokens = _name_tokens(repo_name)
    for prov in scan.provides:
        org_tokens |= _name_tokens(prov.name)

    lines = [f"# {repo_name} — dependency & package map", ""]
    repo_aliases = aliases(repo_name, drop_prefix="." in repo_name)
    if repo_aliases:
        lines += [f'{repo_name} is also referred to as: {", ".join(repo_aliases)}.', ""]

    packages = [p for p in scan.provides if p.kind != "project/assembly"]
    projects = [p for p in scan.provides if p.kind == "project/assembly"]
    if packages:
        lines.append("## Packages and modules this repo provides")
        for prov in packages:
            lines.append(f"- {prov.name} — {prov.kind} ({prov.declared_in})"
                         f"{_alias_note(prov.name, org_tokens, force=True)}")
        lines.append("")
    if projects:
        lines.append("## Projects and assemblies built here")
        for prov in projects:
            lines.append(f"- {prov.name} ({prov.declared_in})"
                         f"{_alias_note(prov.name, org_tokens, force=True)}")
        lines.append("")

    if scan.deps:
        lines.append("## Dependencies")
        by_eco: dict[str, list[Dep]] = {}
        for dep in scan.deps.values():
            by_eco.setdefault(dep.ecosystem, []).append(dep)
        for eco in sorted(by_eco):
            lines.append(f"### {eco}")
            for dep in sorted(by_eco[eco], key=lambda d: d.name.lower()):
                version = f" {dep.version}" if dep.version else ""
                lines.append(f"- {repo_name} depends on {dep.name}{version}"
                             f"{_alias_note(dep.name, org_tokens)} — referenced by {_refs(dep)}")
            lines.append("")

    if scan.project_refs:
        lines.append("## Project references (within this repo)")
        for manifest, target in scan.project_refs:
            lines.append(f"- {manifest} -> {target}")
        lines.append("")

    return Document(
        uri=f"{uri_prefix}::dependency-map",
        title=f"{repo_name}: dependencies & packages",
        text="\n".join(lines).strip() + "\n",
        kind="doc",
        metadata={"graph": _graph_meta(scan, repo_name, org_tokens)},
    )
