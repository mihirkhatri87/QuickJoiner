# Download entire conversation (incl. thinking trace) — design

## Problem

The chat UI only offers a per-answer "download as markdown" button (in the hover
toolbar under each agent message, `Chat.tsx::MessageActions`). There is no way to
download a whole conversation — questions, answers, and the reasoning/thinking
trace — as a single file.

## Constraint discovered during design

Thinking traces are **not persisted anywhere server-side**. They arrive as SSE
`thinking` events during a live turn and are accumulated into `Msg.thinking`
client-side (`App.tsx` line ~360: `patch((m) => ({ ...m, thinking: (m.thinking ||
"") + e.data }))`). `GET /api/sessions/{id}` (`quickjoiner/api/app.py::get_session`)
only ever returns `role` and `content` per stored message:

```python
entry = {k: v for k, v in m.items() if k in ("role", "content")}
```

So a thinking trace only exists in the browser tab's live memory, for turns
generated since the page was last loaded. Reloading the page, or opening a past
conversation from the Rail, loses it — permanently, since it was never written to
`chat_sessions.messages_json` in the first place.

## Decisions (confirmed with user)

1. **Client-side only, for now.** No backend persistence work. The export reflects
   whatever is currently in the browser tab's `messages: Msg[]` state. A future
   iteration could add backend persistence of the thinking trace to make old
   conversations downloadable with full fidelity too — out of scope here.
2. **Scope: the currently-open conversation only.** One button inside the chat
   view. No per-row download action added to the Rail's conversation list (that
   would only ever export Q&A, never thinking, for anything but the live session
   — explicitly declined as a separate, later feature if ever wanted).
3. **Content: everything currently visible per turn** that is actual content —
   not a trimmed-down summary, nothing silently dropped from what's on screen.
   Transient UI state (streaming caret, ingest progress bar) is excluded; see
   "Components & data flow" for the exact list.
4. **Thinking trace renders as a collapsible `<details>`/`<summary>` block** in the
   exported markdown, mirroring the in-app collapsed-by-default treatment. This is
   standard GitHub-Flavored Markdown (renders as a native disclosure widget in
   GitHub, VS Code's preview, Obsidian, etc.); a plain-text viewer shows the literal
   tags, which is an inherent limit of markdown, not something to design around.

## Architecture

Entirely client-side, entirely inside `frontend/src/components/Chat.tsx`. No
backend, API, or schema changes.

## Components & data flow

- **New function `downloadConversation(messages: Msg[])`**, sibling to the existing
  `downloadMarkdown(text: string)` (which is unchanged — still backs the per-answer
  button).
- Walks `messages` in order. Per message, by `role`:
  - `"user"` → `### Q` heading + the question text. If `m.attachments` is set,
    list each attachment's filename (not its content — content may since have
    expired per the 7-day chat-attachment retention sweep).
  - `"agent"` → `### A` heading, then in order:
    - If `m.thinking` is set: `<details><summary>Reasoning trace</summary>` /
      the thinking text / `</details>`.
    - If `m.tools` is non-empty: a `_Tools used: x, y, z_` line.
    - The answer body: `m.answer ?? m.text ?? ""`.
    - If `m.candidates` is set: a short bullet list (rank, summary, confidence).
    - If `m.artifact` is set: the artifact's title, noted as a generated document
      (its body is viewed in `ArtifactModal` and separately downloadable there —
      not inlined here).
    - A `**Sources**` list, derived as described below. Omitted if empty.
  - `"error"` → the error text, prefixed `**Error:**`.

  Two `Msg` fields are deliberately NOT exported, because neither is content:
  `streaming`/`streamText` (in-flight transport state — a mid-stream turn exports
  whatever text has landed so far via the `m.answer ?? m.text` fallback, with no
  partial-turn marker; downloading mid-answer is an edge case, not a supported
  flow) and `learning` (a transient ingest progress bar, meaningless once the
  file is on disk).
- **Sources list derivation reuses existing pure rendering logic**, not a
  reimplementation: `new CiteBook()` + `renderMarkdown(text, book)` (from
  `markdown.tsx`) is called exactly as `AnswerBody` already calls it at render
  time, purely to populate `book.refs` as a side effect — the returned JSX is
  discarded. This guarantees the `[n]` markers already inline in `text` and the
  `[n]` list underneath it always agree, because both come from the same
  numbering pass. Each turn gets its own `CiteBook`, so numbers restart at `[1]`
  per turn (turns are visually separated by `### Q`/`### A` headings, so this
  reads unambiguously — no cross-turn renumbering needed).
- **Button**: rendered once near the top of `Chat`'s scrollable content, labelled
  "Download conversation" (a `Download` icon, consistent with the existing
  per-answer button's icon). Visible whenever `Chat` is mounted at all — `App.tsx`
  already only mounts `Chat` when `messages.length > 0`, so no extra guard needed.
- **Filename**: reuses the existing `answerFilename`-style slug logic, seeded from
  the first user question in the conversation (falls back to a generic name if
  there is none, mirroring the existing fallback in `answerFilename`).

## Error handling

None needed beyond what already exists. This is synchronous string-building over
data already resident in memory — same risk profile as the existing
`downloadMarkdown`, which has no error handling either. Optional fields
(`thinking`/`tools`/`candidates`/`attachments`) are already typed optional on
`Msg`, and are simply skipped when absent.

## Testing

This repo has no frontend unit-test suite (per `CLAUDE.md`: manual/Playwright
verification is the established practice for frontend work). Verification plan:
send a multi-turn conversation that includes at least one turn with a thinking
trace, tool calls, and citations; click "Download conversation"; open the
resulting `.md` file and confirm:
- Turns appear in order with clear Q/A separation.
- The `<details>` block collapses/expands (view in a markdown previewer, e.g. VS
  Code or GitHub).
- Sources list `[n]` numbers match the inline citation markers for that turn.
- A turn with no thinking/tools/candidates/attachments renders cleanly with no
  empty/dangling sections.

## Explicitly out of scope

- Backend persistence of thinking traces (would let old/reloaded conversations
  export with full fidelity — a real follow-up, not built here).
- Downloading a past conversation from the Rail sidebar.
- Any format other than markdown (matches the existing per-answer precedent and
  the rest of the app's export story — md/html/csv/pptx already exists for
  `qj ask`/`qj brief`, but this feature doesn't need to reach for those here).
