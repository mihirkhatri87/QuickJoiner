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

If there are no durable facts, leave the FACTS section empty. Output nothing else.\
"""


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


def parse_compression(text: str) -> tuple[str, list[str]]:
    summary, facts = text.strip(), []
    match = re.search(r"^FACTS:\s*$", text, flags=re.MULTILINE)
    if match:
        summary = text[: match.start()]
        facts = [
            line.strip()[2:].strip()
            for line in text[match.end():].splitlines()
            if line.strip().startswith("- ")
        ]
    summary = re.sub(r"^SUMMARY:\s*", "", summary.strip(), flags=re.IGNORECASE).strip()
    return summary, [f for f in facts if f]


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

        summary_add, facts = self._summarize(old, provider)
        summary = (session.get("summary", "") + "\n\n" + summary_add).strip()
        self.ctx.catalog.save_session(
            session_id,
            session.get("project_id"),
            session.get("title", ""),
            summary,
            messages_to_json(recent),
            estimate_tokens(recent, summary),
        )
        if facts and cfg.learn_from_conversations:
            self._ingest_memory(session, summary_add, facts)
        return True

    def distill(self, session_id: str, provider) -> int:
        """On-demand: extract summary+facts from the full session into memory."""
        session = self.ctx.catalog.get_session(session_id)
        if not session:
            raise ValueError(f"No session {session_id!r}")
        messages = self.history(session)
        if not messages:
            return 0
        summary, facts = self._summarize(messages, provider)
        self._ingest_memory(session, summary, facts)
        return len(facts)

    def _summarize(self, messages: list[Message], provider) -> tuple[str, list[str]]:
        transcript = "\n".join(
            f"{m['role']}: {str(m.get('content', ''))[:1000]}"
            for m in messages
            if m.get("content")
        )
        if provider is None:
            try:
                provider = self.ctx.build_provider()
            except Exception:
                return fallback_digest(messages), []
        try:
            result = provider.chat(
                [{"role": "user", "content": f"Compress these conversation turns:\n\n{transcript}"}],
                system=COMPRESS_SYSTEM,
            )
            return parse_compression(result.text)
        except Exception:
            return fallback_digest(messages), []

    def _ingest_memory(self, session: dict, summary: str, facts: list[str]) -> None:
        project = session.get("project_id") or "default"
        text = f"Conversation memory ({session.get('title') or session['id']}):\n{summary}"
        if facts:
            text += "\n\nDurable facts learned:\n" + "\n".join(f"- {f}" for f in facts)
        self.ctx.catalog.upsert_source("conversations:learned", "Conversation memory", "conversations", {})
        self.ctx.pipeline.ingest(
            [
                Document(
                    uri=f"conversation://{project}/{session['id']}",
                    title=f"Conversation: {session.get('title') or session['id'][:8]}",
                    text=text,
                    kind="note",
                )
            ],
            "conversations:learned",
        )
