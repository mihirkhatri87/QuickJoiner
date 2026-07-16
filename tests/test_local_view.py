"""Resolving an evidence citation's URI back to a local file on disk — only for
connector types that provably have one (git clones, a local files/ source);
everything else (API-based connectors, wikis, tickets) correctly has no local
file and falls back to the remote link (see quickjoiner/api/app.py's endpoint)."""

from __future__ import annotations

from pathlib import Path

from quickjoiner.connectors.local_view import local_file_path


def test_git_clone_uri_resolves_to_workspace_repo_path(tmp_path):
    repo_dir = tmp_path / "repos" / "Connector" / "AppRiver.Connector.Web"
    repo_dir.mkdir(parents=True)
    (repo_dir / "appsettings.json").write_text('{"a": 1}', encoding="utf-8")

    uri = "https://gitlab.otxlab.net/x/appriver.connector.git::AppRiver.Connector.Web/appsettings.json"
    path = local_file_path(tmp_path, "git:Connector", uri)
    assert path == repo_dir / "appsettings.json"


def test_git_uri_missing_file_returns_none(tmp_path):
    uri = "https://gitlab.otxlab.net/x/appriver.connector.git::does/not/exist.cs"
    assert local_file_path(tmp_path, "git:Connector", uri) is None


def test_git_uri_without_separator_returns_none(tmp_path):
    assert local_file_path(tmp_path, "git:Connector", "https://gitlab.otxlab.net/x/repo.git") is None


def test_files_local_uri_resolves(tmp_path):
    f = tmp_path / "some-file.md"
    f.write_text("# hi", encoding="utf-8")
    assert local_file_path(tmp_path, "files:docs", f.as_uri()) == f


def test_files_remote_url_has_no_local_file(tmp_path):
    assert local_file_path(tmp_path, "files:docs", "https://example.com/page") is None


def test_non_local_connector_types_return_none(tmp_path):
    assert local_file_path(tmp_path, "confluence:eng", "https://x.atlassian.net/wiki/page") is None
    assert local_file_path(tmp_path, "github:org-repo", "https://github.com/org/repo/blob/main/a.py") is None
    assert local_file_path(tmp_path, "jira:eng", "https://x.atlassian.net/browse/PAY-1") is None
