"""Chat session and project management: persistence, conversation memory, compression.

- **Sessions** persist the neutral message history in the SQLite catalog, so
  conversations survive restarts (CLI `qj chat --resume`, API `session_id`).
- **Projects** are purposeful groups of sessions: a project's name/description
  is injected into the system prompt for every session in it, and facts learned
  in its conversations carry the project in their provenance URI.
- **Conversation memory**: when a session is compressed (or distilled on
  demand), a summary + durable facts are extracted and ingested into the vector
  memory as `conversation://` documents — the agent can later recall what was
  discussed via search_memory, like any other learned knowledge.
- **Token optimization**: histories beyond `chat.compress_after_est_tokens`
  are folded into a rolling summary keeping only the recent turns verbatim
  (LLM summarizer when available; deterministic digest fallback so compression
  never depends on an API key). Persisted tool outputs are truncated to
  `chat.tool_result_max_chars` — old tool results are the biggest token sink.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from quickjoiner.connectors.base import Document
from quickjoiner.llm.base import Message, ToolCall

COMPRESS_SYSTEM = """\
You compress conversation history for an onboarding assistant. Given older turns of
a conversation, produce exactly this output format:

SUMMARY:
<a compact third-person summary of what was discussed and decided, preserving names,
identifiers, and citations like [uri] verbatim>

FACTS:
- <one durable organization fact per line, worth remembering beyond this conversation>

RELATIONSHIPS:
- <type>: <name> | <relation> | <type>: <name>

RELATIONSHIPS lines record concrete links between two NAMED things that this
conversation established (e.g. "team: payments guild | owns | repo: proj-a").
Allowed types: repo, package, project, service, environment, ticket, person, team.
Allowed relations: depends_on, provides, references, part_of, deploys, owns, works_on.
Use names exactly as the conversation gave them. Never invent relationships.

If a section has nothing, leave it empty. Output nothing else.\
"""

# Knowledge-graph triples now live in ingest/triples.py (shared with document
# ingestion); re-exported here so `from quickjoiner.sessions import Triple,
# parse_triples` keeps working.
from quickjoiner.ingest.triples import (  # noqa: E402
    TRIPLE_RELS,
    TRIPLE_TYPES,
    Triple,
    parse_triples,
    triples_to_graph,
)


def estimate_tokens(messages: list[Message], summary: str = "") -> int:
    """Rough token estimate (~4 chars/token) — for thresholds, not billing."""
    total = len(summary)
    for m in messages:
        total += len(str(m.get("content", "")))
        for tc in m.get("tool_calls", []) or []:
            total += len(str(getattr(tc, "input", tc)))
    return total // 4


def messages_to_json(messages: list[Message]) -> str:
    out = []
    for m in messages:
        entry = dict(m)
        if m.get("tool_calls"):
            entry["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "input": tc.input} for tc in m["tool_calls"]
            ]
        out.append(entry)
    return json.dumps(out)


def messages_from_json(raw: str, tool_result_max_chars: int | None = None) -> list[Message]:
    messages: list[Message] = []
    for entry in json.loads(raw or "[]"):
        if entry.get("tool_calls"):
            entry["tool_calls"] = [ToolCall(**tc) for tc in entry["tool_calls"]]
        if (
            tool_result_max_chars
            and entry.get("role") == "tool"
            and len(entry.get("content", "")) > tool_result_max_chars
        ):
            entry["content"] = entry["content"][:tool_result_max_chars] + "\n…[truncated]"
        messages.append(entry)
    return messages


def split_for_compression(messages: list[Message], keep_recent: int) -> tuple[list[Message], list[Message]]:
    """Split into (old, recent) at a user-turn boundary so tool-call groups stay
    intact and the recent window is never smaller than keep_recent."""
    cut = max(len(messages) - keep_recent, 0)
    while cut > 0 and messages[cut].get("role") != "user":
        cut -= 1
    return messages[:cut], messages[cut:]


_SECTION = re.compile(r"^(SUMMARY|FACTS|RELATIONSHIPS):\s*$", flags=re.MULTILINE)


def _bullets(block: str) -> list[str]:
    return [line.strip()[2:].strip() for line in block.splitlines()
            if line.strip().startswith("- ") and line.strip()[2:].strip()]


def parse_compression(text: str) -> tuple[str, list[str], list[Triple]]:
    matches = list(_SECTION.finditer(text))
    if not matches:
        return text.strip(), [], []
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1)] = text[m.end():end]
    head = text[: matches[0].start()].strip()  # prose before any header counts as summary
    summary = sections.get("SUMMARY", "").strip() or head
    return summary, _bullets(sections.get("FACTS", "")), parse_triples(_bullets(sections.get("RELATIONSHIPS", "")))


def fallback_digest(messages: list[Message]) -> str:
    """Keyless compression: a deterministic one-line-per-turn digest."""
    lines = []
    for m in messages:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            first = str(m["content"]).strip().splitlines()[0][:160]
            lines.append(f"- {m['role']}: {first}")
    return "\n".join(lines)


class SessionManager:
    def __init__(self, ctx):
        self.ctx = ctx  # AppContext: catalog, pipeline, config

    # -- projects -------------------------------------------------------------
    def create_project(self, name: str, description: str = "") -> dict:
        project_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
        self.ctx.catalog.upsert_project(project_id, name, description)
        return self.ctx.catalog.get_project(project_id)

    def resolve_project(self, id_or_name: str | None) -> dict | None:
        return self.ctx.catalog.get_project(id_or_name) if id_or_name else None

    # -- sessions ---------------------------------------------------------------
    def open_session(self, session_id: str | None = None, project: str | None = None) -> dict:
        """Load an existing session or create a new one (optionally in a project)."""
        if session_id:
            row = self.ctx.catalog.get_session(session_id)
            if row:
                return row
        project_row = self.resolve_project(project)
        row = {
            "id": session_id or str(uuid.uuid4()),
            "project_id": project_row["id"] if project_row else None,
            "title": "",
            "summary": "",
            "messages_json": "[]",
            "est_tokens": 0,
        }
        self.ctx.catalog.save_session(
            row["id"], row["project_id"], "", "", "[]", 0
        )
        return self.ctx.catalog.get_session(row["id"])

    def history(self, session: dict) -> list[Message]:
        return messages_from_json(
            session.get("messages_json", "[]"),
            tool_result_max_chars=self.ctx.config.chat.tool_result_max_chars,
        )

    def system_context(self, session: dict) -> str:
        """Extra system-prompt text: active project framing + rolling summary."""
        parts = []
        project = (
            self.ctx.catalog.get_project(session["project_id"]) if session.get("project_id") else None
        )
        if project:
            parts.append(
                f"Active project: {project['name']}."
                + (f" {project['description']}" if project["description"] else "")
                + " Prefer knowledge and memories related to this project."
            )
        if session.get("summary"):
            parts.append(
                "Summary of this conversation so far (older turns were compressed):\n"
                + session["summary"]
            )
        return "\n\n".join(parts)

    def record_turn(self, session_id: str, messages: list[Message]) -> dict:
        session = self.ctx.catalog.get_session(session_id) or self.open_session(session_id)
        title = session.get("title") or next(
            (str(m["content"])[:80] for m in messages if m.get("role") == "user"), ""
        )
        self.ctx.catalog.save_session(
            session_id,
            session.get("project_id"),
            title,
            session.get("summary", ""),
            messages_to_json(messages),
            estimate_tokens(messages, session.get("summary", "")),
        )
        return self.ctx.catalog.get_session(session_id)

    # -- compression & conversation memory ---------------------------------------
    def maybe_compress(self, session_id: str, provider=None) -> bool:
        """Compress when over the token threshold. Returns True if compression ran."""
        cfg = self.ctx.config.chat
        session = self.ctx.catalog.get_session(session_id)
        if not session or session["est_tokens"] < cfg.compress_after_est_tokens:
            return False
        messages = self.history(session)
        old, recent = split_for_compression(messages, cfg.keep_recent_messages)
        if not old:
            return False

        summary_add, facts, triples = self._summarize(old, provider)
        summary = (session.get("summary", "") + "\n\n" + summary_add).strip()
        self.ctx.catalog.save_session(
            session_id,
            session.get("project_id"),
            session.get("title", ""),
            summary,
            messages_to_json(recent),
            estimate_tokens(recent, summary),
        )
        if (facts or triples) and cfg.learn_from_conversations:
            self._ingest_memory(session, summary_add, facts, triples)
        return True

    def distill(self, session_id: str, provider) -> int:
        """On-demand: extract summary+facts from the full session into memory."""
        session = self.ctx.catalog.get_session(session_id)
        if not session:
            raise ValueError(f"No session {session_id!r}")
        messages = self.history(session)
        if not messages:
            return 0
        summary, facts, triples = self._summarize(messages, provider)
        self._ingest_memory(session, summary, facts, triples)
        return len(facts)

    def _summarize(self, messages: list[Message], provider) -> tuple[str, list[str], list[Triple]]:
        transcript = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:1000]}"
            for m in messages
            if m.get("content")
        )
        if provider is None:
            try:
                provider = self.ctx.build_provider()
            except Exception:
                return fallback_digest(messages), [], []  # keyless: no triples, deterministic graph only
        try:
            result = provider.chat(
                [{"role": "user", "content": f"Compress these conversation turns:\n\n{transcript}"}],
                system=COMPRESS_SYSTEM,
            )
            return parse_compression(result.text)
        except Exception:
            return fallback_digest(messages), [], []

    def _ingest_memory(self, session: dict, summary: str, facts: list[str],
                       triples: list[Triple] = ()) -> None:
        project = session.get("project_id") or "default"
        label = session.get("title") or session["id"][:8]
        text = f"Conversation memory ({session.get('title') or session['id']}):\n{summary}"
        if facts:
            text += "\n\nDurable facts learned:\n" + "\n".join(f"- {f}" for f in facts)
        if triples:
            text += "\n\nRelationships noted:\n" + "\n".join(
                f"- {t.src_name} ({t.src_type}) {t.rel} {t.dst_name} ({t.dst_type})"
                for t in triples
            )
        # Triples ride the document as graph metadata — the pipeline persists them
        # with this conversation doc as the evidence for every edge (Phase C).
        metadata: dict = {}
        if triples:
            metadata = {"graph": triples_to_graph(triples, f"said in conversation: {label[:60]}")}
        self.ctx.catalog.upsert_source("conversations:learned", "Conversation memory", "conversations", {})
        self.ctx.pipeline.ingest(
            [
                Document(
                    uri=f"conversation://{project}/{session['id']}",
                    title=f"Conversation: {label}",
                    text=text,
                    kind="note",
                    metadata=metadata,
                )
            ],
            "conversations:learned",
        )
