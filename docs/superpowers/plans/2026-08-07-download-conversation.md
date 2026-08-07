# Download Conversation (with thinking trace) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Download conversation" button to QuickJoiner's chat view that exports the whole open conversation as one markdown file, with each turn's reasoning trace in a collapsible `<details>` block.

**Architecture:** Entirely client-side, entirely inside `frontend/src/components/Chat.tsx`. A pure function walks the `messages: Msg[]` array already passed to `Chat` as a prop and builds a markdown string; a button triggers a Blob download. Citation numbering is harvested by reusing the app's existing `CiteBook` + `renderMarkdown` pass (the returned JSX is discarded), so exported `[n]` markers can never drift from the inline ones. No backend, API, or schema changes.

**Tech Stack:** React 18 + TypeScript + Vite + Tailwind; `lucide-react` icons. No new dependencies.

## Global Constraints

- **Frontend-only.** No changes to `quickjoiner/` Python, no API endpoint changes, no DB schema/migration. If a task seems to need one, stop — the spec is wrong, not the code.
- **No new npm dependencies.** Everything needed is already imported in `Chat.tsx` or `markdown.tsx`.
- **Node for frontend builds is NOT the one on PATH.** PATH has node v16.10.0, which is too old for this toolchain. Use v22.22.3 explicitly:
  - Bash: `cd D:/QuickJoiner/frontend && PATH="$APPDATA/nvm/v22.22.3:$PATH" node node_modules/typescript/bin/tsc --noEmit`
  - PowerShell: `$env:Path = "$env:APPDATA\nvm\v22.22.3;$env:Path"` then `npm run build` in `frontend/`
  - NOTE: `CLAUDE.md` currently documents this as `$env:LOCALAPPDATA\nvm\v22.23.1` — that path does not exist on this machine. Task 5 fixes that doc line.
- **Typecheck baseline is clean** (verified 2026-08-07, exit 0). Any error you see is yours.
- **`npm run build` is slow on this machine (≈2–5 min).** Don't kill it early. `tsc --noEmit` alone is much faster for iterating.
- **No frontend unit-test suite exists in this repo.** Per `CLAUDE.md`, the established practice is typecheck + build + browser verification. Do not scaffold Jest/Vitest — that is out of scope and would be a unilateral toolchain decision.
- **Verification is by typecheck + real browser check**, not by unit tests. Each task states its own check.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `frontend/src/components/Chat.tsx` | Modify | All of it. Add `conversationFilename()`, `messageToMarkdown()`, `conversationToMarkdown()`, `downloadConversation()`, and the toolbar button. |

Everything lands in one file because `Chat.tsx` already owns the `Msg` type, the `messages` prop, the existing `downloadMarkdown`/`answerFilename` precedent, and the `CiteBook`/`renderMarkdown` import. Splitting the export helpers into a new module would separate them from the `Msg` type they're coupled to, for no gain at this size (~120 lines added to a 549-line file).

---

## Reference: existing code these tasks build on

Read this before starting. All of it already exists — do not recreate it.

**`Msg` type** (`Chat.tsx:10-30`) — the shape you are exporting:

```typescript
export interface Msg {
  id: string;
  role: "user" | "agent" | "error";
  text?: string;        // user bubble / error / plain agent note
  answer?: string;      // final grounded answer (markdown + [citations])
  candidates?: CandidateItem[];
  artifact?: Artifact;  // { title: string; markdown: string }
  streaming?: boolean;
  streamText?: string;
  thinking?: string;
  tools?: string[];
  attachments?: ChatAttachment[];
  learning?: { label: string; done: number; total: number; complete?: boolean };
  ts?: string;
}
```

**Supporting types** (already imported at `Chat.tsx:4-5`):
- `CandidateItem` (`types.ts:345`): `{ rank: number; summary: string; confidence: number | null; sources: string[] }`
- `ChatAttachment` (`types.ts:266`): `{ id: string; filename: string; content_type?: string; size?: number; deleted_at?: string | null; extract_error?: string }`
- `Artifact` (`ArtifactModal.tsx:5`): `{ title: string; markdown: string }`

**`CiteBook`** (`markdown.tsx:29`, already imported at `Chat.tsx:7`):

```typescript
export class CiteBook {
  refs: string[] = [];
  number(ref: string): number { /* same ref -> same number, 1-based */ }
}
```

**`renderMarkdown`** (`markdown.tsx:250`, already imported at `Chat.tsx:7`):

```typescript
export function renderMarkdown(text: string, book: CiteBook, opts: MarkdownOpts = {}): ReactNode[]
```

Calling it merely *creates* React elements; it does not render them or run any component, so calling it outside a component purely for its `book.refs` side effect is safe. **Call it without `opts`** (i.e. no `{ mermaid: true }`) when harvesting — you want no mermaid elements built for output you're discarding.

**Existing single-answer download** (`Chat.tsx:41-57`) — the precedent to mirror, and to leave untouched:

```typescript
function answerFilename(text: string): string {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  let title = lines.find((l) => /^#{1,6}\s+/.test(l))?.replace(/^#{1,6}\s+/, "") ?? lines[0] ?? "";
  title = title.replace(/https?:\/\/\S+/g, "").replace(/[*_`~#>[\]()【】]/g, "").trim();
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  return `${slug || "quickjoiner-answer"}.md`;
}

function downloadMarkdown(text: string) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = answerFilename(text);
  a.click();
  URL.revokeObjectURL(a.href);
}
```

**Local `IconButton`** (`Chat.tsx:103`) — takes `{ label, onClick, active?, children }`, renders a 7×7 round button. This is a *different* component from the exported `IconButton` in `ui.tsx`; use the local one.

**`Chat` component signature** (`Chat.tsx:482-492`) — `messages` is already in scope; no prop plumbing needed.

---

### Task 1: Markdown builders, download trigger, and button

Everything in one task, deliberately. `tsconfig.json` has **`noUnusedLocals: true`** (verified 2026-08-07), so adding the builder functions without a call site would fail the typecheck — the builders and the button that calls them cannot be verified apart, so they ship together.

**Files:**
- Modify: `frontend/src/components/Chat.tsx` (insert helpers after `downloadMarkdown`, which ends at line 57; add the button inside the `Chat` component's returned JSX at ~line 517)

**Interfaces:**
- Consumes: `Msg` (same file), `CiteBook` + `renderMarkdown` (already imported line 7), `Download` icon (already imported line 1), `answerFilename`'s slug approach (mirrored, not called)
- Produces:
  - `conversationFilename(messages: Msg[]): string`
  - `messageToMarkdown(m: Msg): string`
  - `conversationToMarkdown(messages: Msg[]): string`
  - `downloadConversation(messages: Msg[]): void`

- [ ] **Step 1: Add the three pure functions**

Insert immediately after the existing `downloadMarkdown` function (after `Chat.tsx:57`), before `function fmtDeleted(...)`:

```typescript
/** Filename for a whole-conversation export, slugged from the first question asked. */
function conversationFilename(messages: Msg[]): string {
  const first = messages.find((m) => m.role === "user")?.text ?? "";
  const cleaned = first.replace(/https?:\/\/\S+/g, "").replace(/[*_`~#>[\]()【】]/g, "").trim();
  const slug = cleaned.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  return `quickjoiner-conversation${slug ? `-${slug}` : ""}.md`;
}

/** One message as a markdown section. Optional fields are skipped when absent, so a
 * plain turn produces no empty headings. Two Msg fields are deliberately NOT exported
 * because neither is content: `streaming`/`streamText` (in-flight transport state — a
 * mid-stream turn simply exports whatever text has landed) and `learning` (a transient
 * ingest progress bar, meaningless in a file). */
function messageToMarkdown(m: Msg): string {
  const out: string[] = [];

  if (m.role === "user") {
    out.push("### Q");
    out.push("");
    out.push(m.text ?? "");
    if (m.attachments?.length) {
      out.push("");
      // Filenames only — the bytes may since have been swept by the retention job.
      out.push(`_Attached: ${m.attachments.map((a) => a.filename).join(", ")}_`);
    }
    return out.join("\n");
  }

  if (m.role === "error") {
    out.push("### Error");
    out.push("");
    out.push(m.text ?? "(unknown error)");
    return out.join("\n");
  }

  out.push("### A");
  out.push("");

  if (m.thinking) {
    // <details>/<summary> is GFM-standard: renders as a native disclosure widget in
    // GitHub, VS Code preview, Obsidian. Mirrors the app's own collapsed-by-default
    // reasoning trace. Blank lines around the body are required for the markdown
    // inside to render rather than being treated as raw HTML content.
    out.push("<details>");
    out.push("<summary>Reasoning trace</summary>");
    out.push("");
    out.push("```");
    out.push(m.thinking);
    out.push("```");
    out.push("");
    out.push("</details>");
    out.push("");
  }

  if (m.tools?.length) {
    out.push(`_Tools used: ${m.tools.join(", ")}_`);
    out.push("");
  }

  const body = m.answer ?? m.text ?? "";
  out.push(body);

  if (m.candidates?.length) {
    out.push("");
    out.push("**Alternative answers considered**");
    out.push("");
    for (const c of m.candidates) {
      const conf = c.confidence == null ? "unscored" : c.confidence.toFixed(2);
      out.push(`- **${c.rank}.** ${c.summary} _(confidence: ${conf})_`);
    }
  }

  if (m.artifact) {
    out.push("");
    out.push(`_Generated document: ${m.artifact.title}_`);
  }

  // Sources: harvested by running the SAME numbering pass the UI runs at render time,
  // so the [n] markers already inline in `body` and this list can never disagree. The
  // returned nodes are discarded — creating React elements runs no component.
  if (body) {
    const book = new CiteBook();
    renderMarkdown(body, book);
    if (book.refs.length) {
      out.push("");
      out.push("**Sources**");
      out.push("");
      book.refs.forEach((ref, i) => out.push(`${i + 1}. ${ref}`));
    }
  }

  return out.join("\n");
}

/** The whole conversation as one markdown document. */
function conversationToMarkdown(messages: Msg[]): string {
  const stamp = new Date().toISOString().replace("T", " ").slice(0, 16);
  const parts = [
    "# QuickJoiner conversation",
    "",
    `_Exported ${stamp}_`,
    "",
    "---",
    "",
  ];
  parts.push(messages.map(messageToMarkdown).join("\n\n---\n\n"));
  parts.push("");
  return parts.join("\n");
}
```

- [ ] **Step 2: Add the download trigger**

Insert immediately after `conversationToMarkdown`:

```typescript
function downloadConversation(messages: Msg[]) {
  const blob = new Blob([conversationToMarkdown(messages)], {
    type: "text/markdown;charset=utf-8",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = conversationFilename(messages);
  a.click();
  URL.revokeObjectURL(a.href);
}
```

- [ ] **Step 3: Add the button to the Chat component's JSX**

In the `Chat` component's `return`, the inner container currently starts like this (`Chat.tsx:517-525`):

```tsx
      <div className="mx-auto flex max-w-[1100px] flex-col gap-9">
        {memo && (
```

Insert a toolbar row as the FIRST child of that container, immediately after the opening `<div ...>` tag and before `{memo && (`:

```tsx
        <div className="flex items-center justify-end">
          <button
            type="button"
            onClick={() => downloadConversation(messages)}
            title="Download this conversation as markdown (includes reasoning traces)"
            className="inline-flex items-center gap-1.5 rounded-full bg-fill px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted transition hover:bg-fill2 hover:text-ink"
          >
            <Download size={12} />
            Download conversation
          </button>
        </div>
```

`messages` is already in scope (it is the component's prop). No new import is needed — `Download` is already imported on line 1.

- [ ] **Step 4: Typecheck**

Run:
```bash
cd D:/QuickJoiner/frontend && PATH="$APPDATA/nvm/v22.22.3:$PATH" node node_modules/typescript/bin/tsc --noEmit
```
Expected: exit 0, no output. (The baseline was verified clean on 2026-08-07, so any error here is from this change.)

- [ ] **Step 5: Build the frontend**

Run (PowerShell — takes 2–5 min, do not kill it early):
```powershell
$env:Path = "$env:APPDATA\nvm\v22.22.3;$env:Path"; cd D:\QuickJoiner\frontend; npm run build
```
Expected: build succeeds, `frontend/dist` updated.

- [ ] **Step 6: Commit**

```bash
cd D:/QuickJoiner && git add frontend/src/components/Chat.tsx frontend/dist && git commit -F - <<'EOF'
feat(chat): download the whole conversation as markdown

A Download conversation button in the chat pane exports every turn to one
markdown file: questions, answers, tool calls, candidates, artifact titles,
and a per-turn sources list whose [n] numbering reuses the app's own
CiteBook pass so it can't drift from the inline markers. Reasoning traces
are emitted as GFM <details> blocks, mirroring the in-app collapsed default.

Client-side only — it reads the messages already in memory, so there is no
API or schema change. Thinking traces are not persisted server-side, so a
reloaded page exports Q&A without them.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 2: Browser verification

The real check. This repo has no frontend unit tests, so this task IS the test.

**Files:** none modified (verification only; fix-ups land here if something is wrong)

**Interfaces:**
- Consumes: everything from Task 1

- [ ] **Step 1: Make sure the server is serving the rebuilt UI**

The FastAPI server serves `frontend/dist`, and a running process must be restarted to pick up a fresh build.

```bash
cd D:/QuickJoiner && curl -s -m 5 http://localhost:8787/api/notifications?hours=1 | head -c 200
```

If that returns JSON with `"active": 0`, no sync is running and it is safe to restart. If `active` is non-zero, WAIT — restarting mid-sync interrupts real work.

Restart:
```bash
cd D:/QuickJoiner && tasklist | grep -i qj    # note the qj.exe PID
# taskkill //PID <pid> //F
# .venv/Scripts/qj.exe serve > /tmp/qj_serve.log 2>&1 &
```
Then confirm: `curl -s http://localhost:8787/api/status`

- [ ] **Step 2: Drive the UI and produce a real export**

Open `http://localhost:8787` in a browser (Playwright or by hand). Ask a question that will produce a *rich* turn — one with a reasoning trace, tool calls, and citations. A known-good one against this workspace:

> list all teams and their members

Wait for the answer to finish streaming. Confirm on screen that the turn shows: a "Reasoning trace" disclosure, tool chips, and a "Grounded · N sources" stamp. (If the model produces no thinking trace, try again with extended thinking enabled in Settings → Language model — the export can only contain a trace if one was streamed.)

- [ ] **Step 3: Click "Download conversation" and open the file**

The button is at the top-right of the chat pane. It should download `quickjoiner-conversation-<slug>.md` to your Downloads folder.

- [ ] **Step 4: Verify the exported file against this checklist**

Open the `.md` in VS Code and use **Ctrl+Shift+V** (markdown preview) — a plain editor pane will show literal `<details>` tags, which is expected and not a failure.

Check all of:
- [ ] Turns appear in order, each with a clear `### Q` / `### A` heading.
- [ ] The **Reasoning trace** block renders as a collapsible widget in the preview, collapsed by default, and expands on click.
- [ ] The reasoning text inside it is intact (not truncated, not escaped into gibberish).
- [ ] `_Tools used: …_` lists the same tools the UI showed as chips.
- [ ] The **Sources** list at the end of the answer has the same count as the "Grounded · N sources" stamp, and its `[n]` numbers match the superscripts inline in the answer text.
- [ ] The answer's own markdown (headings, tables, lists) still renders correctly — the export must not have mangled it.
- [ ] A turn with no thinking/tools/candidates has no empty or dangling sections.

- [ ] **Step 5: Fix anything the checklist caught, then re-verify**

If a check fails, fix it in `Chat.tsx`, re-run the typecheck and build from Task 1 Steps 4–5, restart the server, and repeat Steps 3–4. Commit any fix with a message naming what the browser check caught.

---

### Task 3: Documentation

Per this repo's house rule (`CLAUDE.md` → "Keep the docs current — always"), a user-facing feature must update both `CLAUDE.md` and `README.md` in the same change.

**Files:**
- Modify: `D:/QuickJoiner/CLAUDE.md`
- Modify: `D:/QuickJoiner/README.md`

- [ ] **Step 1: Update the `Chat` bullet in CLAUDE.md**

Find the frontend architecture bullet describing `Chat` (search for `per-answer **hover toolbar**`). Extend that sentence to also mention the conversation-level export. Add, in the same bullet:

> plus a **Download conversation** button at the top of the chat pane exporting the whole open conversation to one markdown file — questions, answers, tool calls, candidates, and each turn's **reasoning trace as a collapsible GFM `<details>` block**, with a per-turn sources list whose `[n]` numbering is harvested by re-running the same `CiteBook`/`renderMarkdown` pass the UI renders with (so exported numbers cannot drift from the inline superscripts). **Client-side only, and honest about one limit:** thinking traces are never persisted server-side (`GET /api/sessions/{id}` returns only `role`/`content`), so a reloaded page or a conversation reopened from the Rail exports its Q&A **without** traces — only turns generated live in the current tab carry them. Backend persistence of the trace is a genuine follow-up, not built.

- [ ] **Step 2: Add a dated status entry in CLAUDE.md**

Add to the end of the status-entry list (immediately before `## Next steps (agreed with user)`):

```markdown
- Whole-conversation markdown export (2026-08-07, user request: "I want the ability to
  download the entire conversation including thinking trace", plus a follow-up asking for
  the trace to be collapsible in the md as well). Shipped as a **Download conversation**
  button in the chat pane — design in the `Chat` frontend bullet above. Worth recording is
  what scoping the feature turned up: **thinking traces are never persisted anywhere
  server-side.** They arrive as SSE `thinking` events and live only in the browser tab's
  `Msg.thinking` state; `get_session` returns only `role`/`content`. So the export is
  deliberately client-side and states its own limit rather than implying a fidelity it
  cannot deliver — a reloaded page exports Q&A with no traces. The collapsible ask is met
  with GFM `<details>`/`<summary>`, which mirrors the in-app disclosure and renders natively
  in GitHub/VS Code/Obsidian (a plain-text viewer shows the literal tags — inherent to
  markdown, not worked around). The sources list re-runs the app's own `CiteBook` pass
  rather than reimplementing citation numbering, so exported `[n]` and inline `[n]` cannot
  disagree. Frontend-only: no API, schema, or Python change, so Swagger/Postman/Bruno are
  untouched. Verified by typecheck + build + a real browser export (no frontend unit-test
  suite exists — manual/Playwright verification is this repo's established practice).
  Spec: `docs/superpowers/specs/2026-08-07-download-conversation-design.md`.
```

- [ ] **Step 3: Fix the stale Node path in CLAUDE.md**

`CLAUDE.md`'s "Dev environment" section says:

> nvm-windows has v22.23.1 but it's NOT on PATH (symlink never activated). Prepend per command: `$env:Path = "$env:LOCALAPPDATA\nvm\v22.23.1;$env:Path"`

Both the version and the parent directory are wrong on this machine (verified 2026-08-07: `nvm list` shows 22.22.3 / 18.20.8 / 16.10.0, installed under `%APPDATA%\nvm`, and PATH resolves to node v16.10.0). Correct it to:

> nvm-windows has v22.22.3 but it's NOT on PATH (PATH resolves to an old v16.10.0). Prepend per command: `$env:Path = "$env:APPDATA\nvm\v22.22.3;$env:Path"`

- [ ] **Step 4: Update README.md**

Find the web-UI feature section and add the export to the user-facing feature list, in the README's own voice (setup + how to use, not architecture):

```markdown
- **Download a conversation** — the **Download conversation** button at the top of the chat
  pane saves the whole exchange as one markdown file: every question and answer, the tools
  each answer used, its cited sources, and the model's reasoning trace in a collapsible
  section. Note that reasoning traces are only captured for turns you watched happen in that
  browser tab — reloading the page or reopening an older conversation exports the questions
  and answers without them.
```

- [ ] **Step 5: Commit**

```bash
cd D:/QuickJoiner && git add CLAUDE.md README.md && git commit -F - <<'EOF'
docs: whole-conversation markdown export

Architecture bullet + dated status entry for the Download conversation
feature, and a README entry for users. Also corrects the stale Node path
in the dev-environment section (%APPDATA%\nvm\v22.22.3, not
%LOCALAPPDATA%\nvm\v22.23.1 — verified against nvm list).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

## Self-review notes

**Spec coverage** — every spec requirement maps to a task:

| Spec requirement | Task |
|---|---|
| `downloadConversation(messages)` sibling to `downloadMarkdown` | 1 (Step 2) |
| User turn → `### Q` + text + attachment filenames | 1 (Step 1) |
| Agent turn → thinking / tools / answer / candidates / artifact / sources | 1 (Step 1) |
| Error turn → error text as its own section | 1 (Step 1) |
| Thinking as collapsible `<details>` | 1 (Step 1) |
| Sources reuse `CiteBook` + `renderMarkdown`, per-turn numbering | 1 (Step 1) |
| `streaming`/`streamText` and `learning` excluded | 1 (documented in the function's comment) |
| Button near top of chat, visible whenever `Chat` is mounted | 1 (Step 3) |
| Filename slugged from the first user question | 1 (`conversationFilename`, Step 1) |
| No backend/API/schema change | Global Constraints |
| Verification by browser, not unit tests | 2 |
| Docs kept in sync (house rule) | 3 |

**Deviations from the spec, deliberate:**
- Spec said error turns get `**Error:**` inline; the plan uses an `### Error` heading instead, for consistency with the `### Q`/`### A` sectioning. Cosmetic.
- The thinking text is wrapped in a ``` fence inside the `<details>`. The spec didn't specify. A raw trace can contain characters markdown would interpret (`#`, `*`, `>`), so fencing preserves it verbatim — matching the app, which renders it in a `<pre>`.

**Task decomposition note:** the builders and the button were originally two tasks. They were merged after verifying `tsconfig.json` sets `noUnusedLocals: true` — builders with no call site fail the typecheck, so a builders-only task would have ended on a knowingly-failing verification step. They are one task because they cannot be verified independently.

**Type consistency:** `conversationFilename` / `messageToMarkdown` / `conversationToMarkdown` / `downloadConversation` are spelled identically in their definitions and call sites (all Task 1). All `Msg` field accesses (`text`, `answer`, `thinking`, `tools`, `candidates`, `artifact`, `attachments`) match the interface at `Chat.tsx:10-30`. `CandidateItem.confidence` is `number | null`, and the code null-checks it before `.toFixed(2)`. `Artifact` exposes `title`, which is the only field the export reads.
