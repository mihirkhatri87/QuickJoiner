"""Per-question chat file attachments — context for ONE question, not long-term memory.

A file attached to a chat message is extracted to text and handed to the agent as context for
that turn, so the user can "ask a question about this file". It is deliberately kept **separate
from the Uploads connector and the vector memory**: nothing here is embedded, indexed, graphed,
or citable as learned knowledge, and QuickJoiner never folds an attachment into the Uploads
connector unless the user explicitly asks. Attachments live under `<workspace>/context/<id>/`
(the original file + `text.txt`) with a catalog row, and a scheduler sweep deletes the bytes
after `chat.context_retention_days` (default 7) while keeping the row so history can still show
the filename and when it was removed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quickjoiner.ingest.extract import ExtractionError, extract_text


def context_root(workspace: Path | str) -> Path:
    d = Path(workspace) / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(filename: str) -> str:
    base = Path(filename.replace("\\", "/")).name
    return (re.sub(r"[^A-Za-z0-9._ +()\-]", "_", base).strip(" .") or "attachment")[:180]


def _dir_for(workspace: Path | str, att_id: str) -> Path:
    return context_root(workspace) / att_id


def store_attachment(ctx, filename: str, data: bytes, content_type: str = "") -> dict:
    """Save an uploaded context file and register it. Extracts text now (so the chat turn need
    not re-parse) into `text.txt` beside the original; a file we can't parse is still stored and
    downloadable, it just contributes no context text. Returns the attachment's metadata dict."""
    att_id = uuid.uuid4().hex[:16]
    folder = _dir_for(ctx.workspace, att_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = _safe_name(filename)
    (folder / name).write_bytes(data)
    try:
        text = extract_text(data, name)
    except ExtractionError:
        text = ""
    if text.strip():
        (folder / "text.txt").write_text(text, encoding="utf-8")
    uploaded_at = datetime.now(timezone.utc).isoformat()
    ctx.catalog.add_context_attachment(
        att_id, name, content_type, len(data), len(text), uploaded_at)
    return {"id": att_id, "filename": name, "content_type": content_type,
            "size": len(data), "char_count": len(text), "uploaded_at": uploaded_at}


def attachment_original_path(ctx, att_id: str) -> Path | None:
    """Path to the stored original file for download, or None if the row/bytes are gone
    (deleted by the retention sweep, or never existed)."""
    row = ctx.catalog.get_context_attachment(att_id)
    if row is None or row.get("deleted_at"):
        return None
    path = _dir_for(ctx.workspace, att_id) / row["filename"]
    return path if path.is_file() else None


def _attachment_text(ctx, att_id: str) -> str:
    row = ctx.catalog.get_context_attachment(att_id)
    if row is None or row.get("deleted_at"):
        return ""
    p = _dir_for(ctx.workspace, att_id) / "text.txt"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except OSError:
        return ""


def build_context_block(ctx, attachment_ids: list[str]) -> tuple[str, list[dict]]:
    """Return (system_prompt_block, resolved_metadata). The block instructs the agent to treat
    the attached files as valid, citable context for THIS question (overriding the search-only
    grounding rule for that content); the metadata is what gets stamped on the user's message so
    the UI can render the attachment chips. Total injected text is capped by
    `chat.attachment_context_max_chars`. Unknown/deleted ids are skipped for text but still
    reported in metadata (so a chip renders) — though at send time they'll normally be fresh."""
    if not attachment_ids:
        return "", []
    cap = ctx.config.chat.attachment_context_max_chars
    meta: list[dict] = []
    parts: list[str] = []
    used = 0
    for att_id in attachment_ids:
        row = ctx.catalog.get_context_attachment(att_id)
        if row is None:
            continue
        meta.append({"id": att_id, "filename": row["filename"],
                     "content_type": row.get("content_type") or "",
                     "size": row.get("size_bytes") or 0})
        if used >= cap:
            continue
        text = _attachment_text(ctx, att_id)
        if not text.strip():
            continue
        take = text[: max(0, cap - used)]
        used += len(take)
        parts.append(f"--- FILE: {row['filename']} ---\n{take}"
                     + ("\n…[truncated]" if len(take) < len(text) else ""))
    if not parts:
        block = ""
    else:
        block = (
            "ATTACHED FILES — the user attached the following file(s) as context for THIS "
            "question. Treat their content as valid evidence you may quote and reason over to "
            "answer, exactly as if retrieved from memory; you do NOT need search_memory to use "
            "them. Cite them as [file: <name>]. They are per-question context, NOT long-term "
            "memory — never claim they were 'learned' or suggest connecting/ingesting them, and "
            "do not save them anywhere unless the user explicitly asks. If the question needs org "
            "knowledge beyond these files, still search_memory and combine.\n\n"
            + "\n\n".join(parts)
        )
    return block, meta


def resolve_message_attachments(ctx, atts: list[dict]) -> list[dict]:
    """Overlay each stored message-attachment snapshot with its CURRENT state from the catalog
    (mainly `deleted_at`), so reloaded history shows a working download link or a
    deleted-with-tooltip warning without the snapshot going stale."""
    if not atts:
        return []
    rows = {r["id"]: r for r in ctx.catalog.get_context_attachments([a.get("id") for a in atts if a.get("id")])}
    out = []
    for a in atts:
        row = rows.get(a.get("id"))
        out.append({
            "id": a.get("id"),
            "filename": (row or a).get("filename", a.get("filename", "attachment")),
            "content_type": (row or a).get("content_type", a.get("content_type", "")),
            "size": (row or {}).get("size_bytes", a.get("size", 0)),
            # A row that vanished entirely is treated as deleted too (belt-and-braces).
            "deleted_at": row.get("deleted_at") if row else a.get("deleted_at") or "gone",
        })
    return out


def cleanup_expired(ctx) -> int:
    """Delete the bytes of attachments older than the retention window and mark their rows
    deleted (keeping the row so history still shows the name + deletion time). Returns the count.
    Best-effort per attachment — one failure never stops the sweep."""
    days = ctx.config.chat.context_retention_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    deleted = 0
    for row in ctx.catalog.list_expired_context_attachments(cutoff):
        folder = _dir_for(ctx.workspace, row["id"])
        try:
            for p in folder.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
            folder.rmdir()
        except OSError:
            pass  # folder missing/non-empty — the row is still marked deleted below
        ctx.catalog.mark_context_attachment_deleted(row["id"], now)
        deleted += 1
    return deleted
