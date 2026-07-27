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
from quickjoiner.sync_control import NOOP_CONTROL, SyncControl


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

    # A running sync attaches a live control (sync_manager); until then this no-op default
    # means `_checkpoint`/`_stage` are safe to call from any connector, sync path, or test.
    # Each job builds a fresh connector instance, so the per-instance control is thread-safe.
    _control: SyncControl = NOOP_CONTROL

    def __init__(self, name: str, options: dict[str, Any], workspace: Path):
        self.name = name
        self.options = options
        self.workspace = workspace

    @property
    def source_id(self) -> str:
        return f"{self.type_name}:{self.name}"

    def _checkpoint(self) -> None:
        """Cooperative pause/stop point — call inside long non-yielding loops (paginating
        an API, walking teams) so a stop/pause is honored within seconds instead of after
        the whole phase. Raises SyncStopped when cancelled; blocks while paused."""
        self._control.check()

    def _stage(self, name: str, done: int | None = None, total: int | None = None) -> None:
        """Report the current phase (+ optional progress for an estimated %) to the UI.
        Also a cancellation point, so stage transitions honor stop/pause too."""
        self._control.stage(name, done, total)

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

    def wants_resync(self, payload: dict[str, Any]) -> bool:
        """True if this push event should trigger a full `sync()` run instead of (or as
        well as) `handle_event`'s yielded documents. Most PUSH connectors don't need
        this — GitHub/GitLab/Jira/etc. can turn the payload directly into documents. A
        connector that only knows how to read its content by re-fetching a whole
        resource (the git-clone connector: a push webhook carries commit metadata, never
        file contents) overrides this instead, and `handle_event` there yields nothing."""
        return False

    def on_deleted(self) -> None:
        """Release anything this connector stored OUTSIDE the catalog when it is deleted.

        Most connectors need nothing: their credentials live in the source's options,
        which `save_config` removes with the source. Override when a connector holds
        workspace-side state that would otherwise outlive it — OneDrive keeps a
        Microsoft 365 refresh token on disk, and a live token left behind after its
        connector is gone is still redeemable. Best-effort: raising here must never
        block a deletion."""
        return None
