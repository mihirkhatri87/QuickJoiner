"""Dependency mapping (connectors/deps.py): manifest parsing, the synthesized
dependency-map document, semantic aliasing of dotted package names, and the
end-to-end correlation this enables (repo A references repo B's package;
a loose alias query retrieves both sides)."""

from __future__ import annotations

from pathlib import Path

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.deps import aliases, dependency_document
from quickjoiner.connectors.files import FilesConnector, read_file_document
from quickjoiner.ingest.pipeline import IngestPipeline

CSPROJ_A = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="AppRiver.Nautical.Models" Version="3.2.0" />
    <PackageReference Include="Newtonsoft.Json"><Version>13.0.1</Version></PackageReference>
    <ProjectReference Include="..\\Core\\Core.csproj" />
  </ItemGroup>
</Project>"""

CSPROJ_B = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <PackageId>AppRiver.Nautical.Models</PackageId>
    <AssemblyName>AppRiver.Nautical.Models</AssemblyName>
  </PropertyGroup>
</Project>"""

OLD_CSPROJ = """<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup>
    <Reference Include="AppRiver.Nautical.Core, Version=1.0.0.0, Culture=neutral" />
  </ItemGroup>
</Project>"""

PACKAGES_CONFIG = """<packages>
  <package id="AppRiver.Nautical.Legacy" version="1.9.0" />
</packages>"""


def test_aliases_follow_org_naming_convention():
    assert aliases("AppRiver.Nautical") == ["nautical", "appriver nautical"]
    assert aliases("AppRiver.Nautical.Models") == [
        "nautical models", "nautical.models", "appriver nautical models"]
    assert aliases("single") == []
    assert aliases("proj-a-service", drop_prefix=False) == ["proj a service"]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dependency_document_dotnet(tmp_path):
    _write(tmp_path, "src/Api/Api.csproj", CSPROJ_A)
    _write(tmp_path, "src/Legacy/Legacy.csproj", OLD_CSPROJ)
    _write(tmp_path, "src/Legacy/packages.config", PACKAGES_CONFIG)
    doc = dependency_document(tmp_path, "proj-a", "https://git/proj-a")

    assert doc is not None
    assert doc.uri == "https://git/proj-a::dependency-map"
    text = doc.text
    assert "proj-a depends on AppRiver.Nautical.Models 3.2.0" in text
    assert "also referred to as: nautical models, nautical.models" in text
    assert "proj-a depends on Newtonsoft.Json 13.0.1" in text  # child <Version> parsed
    assert "Newtonsoft.Json 13.0.1 — also referred" not in text  # third-party: no alias
    assert "AppRiver.Nautical.Core" in text  # old-style assembly Reference
    assert "AppRiver.Nautical.Legacy 1.9.0" in text  # packages.config
    assert "src/Api/Api.csproj -> ../Core/Core.csproj" in text
    assert "proj-a is also referred to as: proj a." in text  # repo name alias, no prefix-drop


def test_dependency_document_provides_side(tmp_path):
    _write(tmp_path, "Models/Models.csproj", CSPROJ_B)
    doc = dependency_document(tmp_path, "nautical-models", "https://git/b")
    text = doc.text
    assert "AppRiver.Nautical.Models — NuGet package id" in text
    assert "also referred to as: nautical models" in text  # provided ids always aliased


def test_dependency_document_polyglot(tmp_path):
    _write(tmp_path, "package.json",
           '{"name": "@org/webapp", "dependencies": {"react": "18.2.0"}}')
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "qj-tool"\ndependencies = ["httpx>=0.27"]\n')
    _write(tmp_path, "requirements.txt", "# pinned\nfastapi==0.111\n-r other.txt\n")
    _write(tmp_path, "go.mod",
           "module github.com/org/svc\n\nrequire (\n\tgithub.com/lib/pq v1.10.9\n)\n")
    _write(tmp_path, "pom.xml",
           "<project><groupId>com.org</groupId><artifactId>svc</artifactId><dependencies>"
           "<dependency><groupId>junit</groupId><artifactId>junit</artifactId>"
           "<version>4.13</version></dependency></dependencies></project>")
    text = dependency_document(tmp_path, "poly", "file:///poly").text
    assert "@org/webapp — npm package" in text
    assert "poly depends on react 18.2.0" in text
    assert "qj-tool — Python package" in text
    assert "poly depends on httpx >=0.27" in text
    assert "poly depends on fastapi ==0.111" in text
    assert "github.com/org/svc — Go module" in text
    assert "poly depends on github.com/lib/pq v1.10.9" in text
    assert "pq v1.10.9 — also referred" not in text  # module paths don't spray aliases
    assert "com.org:svc — Maven artifact" in text
    assert "poly depends on junit:junit 4.13" in text


def test_dependency_document_none_without_manifests(tmp_path):
    _write(tmp_path, "README.md", "# hello")
    assert dependency_document(tmp_path, "plain", "file:///plain") is None


BUILD_GRADLE = """plugins { id 'java' }
dependencies {
    implementation 'com.appriver:nautical-core:2.1.0'
    implementation group: 'org.slf4j', name: 'slf4j-api', version: '2.0.13'
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.0")
    implementation project(':shared')
}
"""

SETTINGS_GRADLE = """rootProject.name = 'nautical-svc'
include ':app', ':shared'
"""

LIBS_VERSIONS_TOML = """[versions]
jackson = "2.17.1"
[libraries]
jackson-databind = { module = "com.fasterxml.jackson.core:jackson-databind", version.ref = "jackson" }
guava = "com.google.guava:guava:33.0.0-jre"
"""


def test_dependency_document_gradle(tmp_path):
    _write(tmp_path, "build.gradle", BUILD_GRADLE)
    _write(tmp_path, "settings.gradle", SETTINGS_GRADLE)
    _write(tmp_path, "gradle/libs.versions.toml", LIBS_VERSIONS_TOML)
    text = dependency_document(tmp_path, "nautical-svc", "https://git/n").text

    # org-internal coordinate gets spoken-form aliases (groupId stripped)
    assert "nautical-svc depends on com.appriver:nautical-core 2.1.0" in text
    assert "also referred to as: nautical core" in text
    # map notation, third-party: parsed, not aliased
    assert "nautical-svc depends on org.slf4j:slf4j-api 2.0.13" in text
    assert "slf4j-api 2.0.13 — also referred" not in text
    assert "nautical-svc depends on org.junit.jupiter:junit-jupiter 5.10.0" in text
    # version catalog resolves version.ref and plain strings
    assert "com.fasterxml.jackson.core:jackson-databind 2.17.1" in text
    assert "com.google.guava:guava 33.0.0-jre" in text
    # settings.gradle: root project + included modules; project() as a project ref
    assert "- nautical-svc (settings.gradle)" in text
    assert "- app (settings.gradle)" in text
    assert "build.gradle -> :shared" in text


POM_XML = """<project>
  <parent><groupId>com.appriver</groupId><artifactId>nautical-parent</artifactId>
    <version>7.1.0</version></parent>
  <artifactId>nautical-billing</artifactId>
  <properties><jackson.version>2.17.1</jackson.version></properties>
  <modules><module>billing-core</module></modules>
  <dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId><version>${jackson.version}</version></dependency>
    <dependency><groupId>com.appriver</groupId>
      <artifactId>nautical-models</artifactId><version>${project.version}</version></dependency>
  </dependencies>
</project>"""


def test_dependency_document_maven_properties_parent_modules(tmp_path):
    _write(tmp_path, "pom.xml", POM_XML)
    text = dependency_document(tmp_path, "nautical-billing", "https://git/b").text

    assert "com.appriver:nautical-billing — Maven artifact" in text  # groupId from parent
    assert "jackson-databind 2.17.1" in text  # ${jackson.version} resolved
    assert "nautical-models 7.1.0" in text  # ${project.version} from parent version
    assert "also referred to as: nautical models" in text  # org-internal artifact aliased
    assert "pom.xml -> parent com.appriver:nautical-parent" in text
    assert "pom.xml -> module billing-core" in text


PACKAGE_JSON_WS = """{
  "name": "@appriver/nautical-portal",
  "workspaces": ["packages/*"],
  "dependencies": {"react": "18.2.0", "@appriver/nautical-ui": "4.0.1"},
  "peerDependencies": {"react-dom": "18.2.0"}
}"""


def test_dependency_document_npm_and_angular(tmp_path):
    _write(tmp_path, "package.json", PACKAGE_JSON_WS)
    _write(tmp_path, "angular.json", '{"projects": {"portal-admin": {}, "portal-e2e": {}}}')
    text = dependency_document(tmp_path, "nautical-portal", "https://git/p").text

    assert "@appriver/nautical-portal — npm package" in text
    assert "also referred to as: nautical portal" in text  # scope stripped in alias
    assert "depends on @appriver/nautical-ui 4.0.1" in text
    assert "also referred to as: nautical ui" in text  # org scope makes it internal
    assert "depends on react-dom 18.2.0" in text  # peerDependencies included
    assert "react 18.2.0" in text
    assert "react 18.2.0 — also referred" not in text  # third-party stays silent
    assert "package.json -> workspace packages/*" in text
    assert "- portal-admin (angular.json)" in text  # Angular projects listed


PYPROJECT_POETRY = """[tool.poetry]
name = "nautical-etl"
[tool.poetry.dependencies]
python = "^3.11"
httpx = "^0.27"
nautical-sdk = {version = "1.4.0"}
[tool.poetry.group.dev.dependencies]
pytest = "^8.0"
"""

SETUP_CFG = """[metadata]
name = legacy-tool
[options]
install_requires =
    flask>=2.0
    nautical-sdk==1.4.0
"""


def test_dependency_document_python_variants(tmp_path):
    _write(tmp_path, "pyproject.toml", PYPROJECT_POETRY)
    _write(tmp_path, "Pipfile", '[packages]\nrequests = "==2.32.0"\n[dev-packages]\nblack = "*"\n')
    _write(tmp_path, "setup.cfg", SETUP_CFG)
    _write(tmp_path, "setup.py",
           'from setuptools import setup\nsetup(name="oldest-tool", install_requires=["click>=8.0"])\n')
    text = dependency_document(tmp_path, "nautical-etl", "https://git/e").text

    assert "nautical-etl — Python package" in text  # poetry name
    assert "legacy-tool — Python package" in text  # setup.cfg metadata
    assert "oldest-tool — Python package" in text  # setup.py regex
    assert "depends on httpx ^0.27" in text  # poetry table
    assert "depends on nautical-sdk 1.4.0" in text  # dict form + setup.cfg dedupe by version
    assert "also referred to as: nautical sdk" in text
    assert "depends on pytest ^8.0" in text  # poetry dev group
    assert "depends on requests ==2.32.0" in text  # Pipfile
    assert "depends on black — referenced by Pipfile" in text  # "*" version elided
    assert "depends on flask >=2.0" in text  # setup.cfg
    assert "depends on click >=8.0" in text  # setup.py
    assert "python" not in {ln.split(" depends on ")[-1].split(" ")[0]
                            for ln in text.splitlines() if " depends on " in ln}


def test_manifest_files_are_now_ingested(tmp_path):
    _write(tmp_path, "Api.csproj", CSPROJ_A)
    _write(tmp_path, "app.sln", "Microsoft Visual Studio Solution File")
    assert read_file_document(tmp_path / "Api.csproj", tmp_path) is not None
    assert read_file_document(tmp_path / "app.sln", tmp_path) is not None


def test_files_connector_emits_dependency_map(tmp_path, workspace):
    _write(tmp_path / "repo", "src/Api/Api.csproj", CSPROJ_A)
    connector = FilesConnector(name="proj-a", options={"path": str(tmp_path / "repo")},
                               workspace=workspace)
    docs = list(connector.sync({}))
    assert any(d.uri.endswith("::dependency-map") for d in docs)
    assert any(d.uri.endswith("Api.csproj") for d in docs)  # raw manifest ingested too


def test_loose_alias_query_correlates_both_repos(tmp_path, workspace, catalog, store):
    """The user scenario: repo A consumes repo B's NuGet package. A loose question
    ("nautical models") must retrieve the dependency maps of BOTH repos, so the
    agent can correlate consumer and provider with taught facts."""
    _write(tmp_path / "a", "src/Api/Api.csproj", CSPROJ_A)
    _write(tmp_path / "b", "Models/Models.csproj", CSPROJ_B)
    pipe = IngestPipeline(store, catalog)
    for name, folder in (("proj-a", "a"), ("nautical-models", "b")):
        connector = FilesConnector(name=name, options={"path": str(tmp_path / folder)},
                                   workspace=workspace)
        pipe.ingest(connector.sync({}), f"files:{name}")

    hits = store.search("nautical models", top_k=8, min_score=0.0)
    map_sources = {h.source_id for h in hits if h.uri.endswith("::dependency-map")}
    assert map_sources == {"files:proj-a", "files:nautical-models"}
