"""Connector contract: every external system integration implements this.

A connector declares the connection modes it supports, in preference order:
PULL (API sync), PUSH (webhooks), LIVE (agent tools reading the source directly),
BROWSER (authenticated user-credential browser session), SCRAPE (last resort).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Flag, auto
from pathlib import Path
from typing import Any, Iterator

from quickjoiner.llm.base import AgentTool


class Mode(Flag):
    PULL = auto()
    PUSH = auto()
    LIVE = auto()
    BROWSER = auto()
    SCRAPE = auto()


@dataclass
class Document:
    uri: str
    title: str
    text: str
    kind: str = "doc"  # code | doc | ticket | pipeline | deployment | dashboard | note
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionStatus:
    ok: bool
    message: str = ""


class Connector(ABC):
    type_name: str = "base"
    modes: Mode = Mode.PULL

    def __init__(self, name: str, options: dict[str, Any], workspace: Path):
        self.name = name
        self.options = options
        self.workspace = workspace

    @property
    def source_id(self) -> str:
        return f"{self.type_name}:{self.name}"

    @abstractmethod
    def test(self) -> ConnectionStatus:
        """Validate credentials/reachability without side effects."""

    @abstractmethod
    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        """Yield documents changed since the last sync (incremental via state)."""

    def tools(self) -> list[AgentTool]:
        """Live agent tools that read directly from the source at question time."""
        return []

    def handle_event(self, payload: dict[str, Any]) -> Iterator[Document]:
        """Turn a pushed webhook event into documents. Override for PUSH mode."""
        return iter(())
