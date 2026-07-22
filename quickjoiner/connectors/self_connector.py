"""The QuickJoiner control connector — a permanent, non-ingested singleton.

Unlike every other connector, this one produces NO documents (`sync()` yields nothing) and
contributes NO live source tools of its own — its capability is QuickJoiner's *own* API,
exposed to chat through the ctx-backed control tools in `agent/control.py` (`qj_api` /
`qj_api_reference`). It exists as a source purely so the app can present "QuickJoiner control"
as a connector plate that is always there and cannot be deleted (plan 08).

Because it yields no documents it can never enter memory or the knowledge graph, and because
its type is not in `connectors/specs.FORM_SPECS` it is not user-creatable from the connector
catalog. The reserved name/type below are what the API guards check to refuse
delete/rename/sync of the control connector.
"""

from __future__ import annotations

from typing import Any, Iterator

from quickjoiner.connectors.base import ConnectionStatus, Connector, Document, Mode
from quickjoiner.connectors.registry import register

CONTROL_TYPE = "quickjoiner"
CONTROL_NAME = "quickjoiner"


@register
class QuickJoinerConnector(Connector):
    type_name = CONTROL_TYPE
    modes = Mode.LIVE

    def test(self) -> ConnectionStatus:
        return ConnectionStatus(True, "QuickJoiner control connector (always available)")

    def sync(self, state: dict[str, str]) -> Iterator[Document]:
        # Never ingested — the control connector carries no documents by design.
        return iter(())

    def tools(self) -> list[Any]:
        # Its capability is the ctx-backed control tools (agent/control.py), not per-source tools.
        return []


def is_control_source(name: str | None = None, type_: str | None = None) -> bool:
    """True if a source name/type refers to the reserved control connector."""
    return name == CONTROL_NAME or type_ == CONTROL_TYPE
