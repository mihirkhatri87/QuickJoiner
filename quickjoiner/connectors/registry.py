from __future__ import annotations

from pathlib import Path

from quickjoiner.config import SourceConfig
from quickjoiner.connectors.base import Connector

CONNECTOR_TYPES: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    CONNECTOR_TYPES[cls.type_name] = cls
    return cls


def create_connector(source: SourceConfig, workspace: Path) -> Connector:
    _load_builtin_connectors()
    cls = CONNECTOR_TYPES.get(source.type)
    if cls is None:
        known = ", ".join(sorted(CONNECTOR_TYPES)) or "(none)"
        raise ValueError(f"Unknown connector type {source.type!r}. Known types: {known}")
    return cls(name=source.name, options=source.options, workspace=workspace)


def _load_builtin_connectors() -> None:
    # Import for the @register side effect.
    from quickjoiner.connectors import (  # noqa: F401
        azure_devops,
        confluence,
        files,
        git_repo,
        github,
        gitlab,
        jira,
        octopus,
        onedrive,
        self_connector,
        uploads,
    )
    from quickjoiner.connectors.browser import scraper  # noqa: F401
    from quickjoiner.connectors.logsearch import (  # noqa: F401
        datadog,
        dynatrace,
        elastic,
        grafana,
    )
