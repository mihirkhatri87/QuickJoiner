"""Shared scaffolding for connectors' READ-ONLY live agent tools (plan 09).

Two jobs:
- **Consolidation** (Phase 0): a connector *type* with several configured instances (AppRiver
  has three GitLab projects) exposes ONE tool set with a `project`/`repo` selector, instead of
  N near-duplicate tool sets that bloat the model's context. `make_resolver` turns a selector
  string into the owning connector (for its base_url/token); with a single instance the
  selector is ignored, with several an unmatched selector returns a helpful "which one?" hint.
- **Boilerplate + safety**: `read_tool` builds an `AgentTool` from a compact param spec, and
  `bounded` caps any tool's output so a big list/diff/log can't overflow the window.

Everything here is READ-ONLY by construction — callers only ever issue GET requests. Nothing in
this module (or the tools built with it) mutates a remote system.
"""

from __future__ import annotations

from typing import Any, Callable

from quickjoiner.llm.base import AgentTool, ToolSpec

# A single tool result is capped here as a backstop; the agent loop also caps live results to
# chat.live_tool_result_max_chars. Diffs/logs pass an explicit smaller cap.
MAX_TOOL_CHARS = 12000


def cite(label: str, url: str | None) -> str:
    """A citable marker for one live result: `[source: <label> | uri: <url>]`.

    Live tools answer the ENUMERATION questions memory can't ("which MRs are open", "what
    ran on this branch"), and the model cites what they return — but a bare "!123 Fix login"
    is not resolvable to anything, so those citations could never become links the way an
    ingested document's could. This is the same shape `search_memory` emits, so the one
    citation parser reads both and a live result links exactly like a learned one.

    Returns "" when there is no url: a marker with nothing behind it would add noise to the
    model's context and promise a link that cannot exist.
    """
    if not url or not label:
        return ""
    # Neither field may contain the delimiters, or the marker stops parsing where it
    # shouldn't. Titles with brackets/pipes are common in ticket systems.
    clean_label = str(label).replace("|", "/").replace("[", "(").replace("]", ")").strip()
    clean_url = str(url).split()[0].replace("]", "%5D") if str(url).strip() else ""
    if not clean_label or not clean_url:
        return ""
    return f" [source: {clean_label} | uri: {clean_url}]"


def bounded(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if text and len(text) > limit:
        return text[:limit] + "\n…[output truncated]"
    return text


def make_resolver(connectors: list, label: Callable[[Any], str]):
    """`resolve(selector) -> (connector, error)`. One instance ⇒ always that one (selector
    ignored). Several ⇒ case-insensitive substring match of `selector` against `label(conn)`;
    no/unknown selector ⇒ `(None, "specify which …")` so the model asks or retries with a name."""
    def resolve(selector: str | None):
        if len(connectors) == 1:
            return connectors[0], None
        if selector:
            s = selector.strip().lower()
            for c in connectors:
                if s and s in label(c).lower():
                    return c, None
        names = ", ".join(sorted(label(c) for c in connectors))
        return None, f"Specify which project (one of: {names})."
    return resolve


def read_tool(
    name: str,
    description: str,
    fn: Callable[..., str],
    params: dict[str, tuple[str, bool, str]] | None = None,
) -> AgentTool:
    """Build a read-only `AgentTool`. `params`: name -> (json_type, required, description)."""
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, (ptype, req, pdesc) in (params or {}).items():
        props[pname] = {"type": ptype, "description": pdesc}
        if req:
            required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return AgentTool(spec=ToolSpec(name=name, description=description, input_schema=schema), fn=fn)


def clamp(n: Any, default: int, hi: int) -> int:
    try:
        return max(1, min(int(n or default), hi))
    except (TypeError, ValueError):
        return default
