import { Check, CircleAlert, Download, Loader2, Paperclip, Quote, ScrollText, ThumbsDown, ThumbsUp, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CandidateItem, ChatAttachment } from "../types";
import type { Artifact } from "./ArtifactModal";
import { CandidateCarousel, type CandidateCard } from "./CandidateCarousel";
import { CiteBook, renderInline, renderMarkdown } from "./markdown";
import { cn } from "./ui";

export interface Msg {
  id: string;
  role: "user" | "agent" | "error";
  text?: string; // user bubble / error / plain agent note
  answer?: string; // final grounded answer (markdown + [citations])
  candidates?: CandidateItem[]; // validated multi-angle options (plan 06 §C)
  artifact?: Artifact; // generated document (e.g. scrape report) viewable in the modal
  streaming?: boolean;
  streamText?: string; // live delta accumulation before the final answer arrives
  thinking?: string;
  tools?: string[];
  attachments?: ChatAttachment[]; // per-question context files shown beneath the question
  /** Real progress for an in-flight "learn this permanently" ingest (chunks embedded / total)
   * — a large document can take minutes to embed on a CPU-only machine, so this replaces a
   * silent wait with an honest, live number. Renders above thinking/tools/answer in the same
   * bubble; `complete` marks it done and settled (kept visible, not removed, as a record of
   * what happened this turn). `total` of 0 means "still extracting/chunking — count not
   * known yet", shown as an indeterminate bar rather than a fake 0%. */
  learning?: { label: string; done: number; total: number; complete?: boolean };
  ts?: string;
}

type Reaction = "like" | "dislike";

const REFUSAL = /haven'?t learned|not (yet )?learned|don'?t (yet )?have|no.*(learned|in memory)/i;

/* Answer rendering lives in markdown.tsx (the shared <Markdown> engine); here we
 * add the grounded/not-learned stamp, per-message hover actions (download,
 * view sources, like/dislike → learn from feedback), and the citation superscripts
 * carry native hover tooltips (title=source) — no dedicated side panel. */

/** A fence guaranteed to survive its own content: CommonMark closes a fenced block
 * only on a run of backticks at least as long as the opener, so a trace that itself
 * contains ``` needs a longer fence. Without this, an inner run closes the block early
 * and the trailing fence opens a new one — swallowing the </details> and every later
 * turn into one unterminated code block. */
function safeFence(content: string): string {
  const longest = (content.match(/`+/g) ?? []).reduce((max, run) => Math.max(max, run.length), 0);
  return "`".repeat(Math.max(3, longest + 1));
}

/** Derive a context-appropriate .md filename from the answer's first heading/line. */
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
    const fence = safeFence(m.thinking);
    out.push("<details>");
    out.push("<summary>Reasoning trace</summary>");
    out.push("");
    out.push(fence);
    out.push(m.thinking);
    out.push(fence);
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

function fmtDeleted(deletedAt?: string | null): string {
  if (!deletedAt || deletedAt === "gone") return "This file was removed and can no longer be downloaded.";
  const d = new Date(deletedAt);
  const when = isNaN(d.getTime())
    ? deletedAt
    : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric" });
  return `Deleted on ${when} (7-day retention) — can no longer be downloaded.`;
}

/** Per-question context files shown beneath a question: a download button, or — once the
 * retention sweep deleted the file — a struck-through name with a warning icon + when-tooltip. */
function AttachmentChips({ attachments }: { attachments: ChatAttachment[] }) {
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="mt-1.5 flex flex-col items-end gap-1">
      {attachments.map((a) =>
        a.deleted_at ? (
          <span
            key={a.id}
            title={fmtDeleted(a.deleted_at)}
            className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-fill px-3 py-1 text-[12px] text-faint"
          >
            <TriangleAlert size={13} className="flex-shrink-0 text-gold" />
            <span className="truncate line-through decoration-faint/60">{a.filename}</span>
          </span>
        ) : (
          <button
            key={a.id}
            type="button"
            title={`Download ${a.filename}`}
            onClick={() => api.downloadChatAttachment(a.id, a.filename).catch((e) => setErr(String(e?.message || e)))}
            className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-fill2 px-3 py-1 text-[12px] text-muted transition hover:text-ink"
          >
            <Paperclip size={13} className="flex-shrink-0" />
            <span className="truncate">{a.filename}</span>
            <Download size={12} className="flex-shrink-0 opacity-60" />
          </button>
        ),
      )}
      {err && <span className="text-[11px] text-danger">{err}</span>}
    </div>
  );
}

function IconButton({
  label,
  onClick,
  active,
  children,
}: {
  label: string;
  onClick: () => void;
  active?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "flex h-7 w-7 items-center justify-center rounded-full transition",
        active ? "bg-accent-soft text-accent" : "text-faint hover:bg-fill hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

/** Modal listing every source a single answer cited (replaces the side panel). */
function SourcesModal({ refs, onClose }: { refs: string[]; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-[2px]" onClick={onClose}>
      <div
        className="flex max-h-full w-full max-w-[560px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 border-b border-fill2 px-5 py-3.5">
          <Quote size={14} className="text-gold" />
          <div className="flex-1 text-[13px] font-semibold text-ink">
            Cited sources · {refs.length}
          </div>
          <button onClick={onClose} aria-label="Close" className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted transition hover:text-ink">
            <X size={15} />
          </button>
        </div>
        <ol className="scroll-thin flex flex-col gap-2 overflow-y-auto px-5 py-4">
          {refs.map((ref, i) => {
            const isUrl = /^https?:\/\//.test(ref);
            return (
              <li key={i} className="flex items-baseline gap-3">
                <span className="font-mono text-[11px] font-semibold tabular-nums text-gold">{i + 1}</span>
                {isUrl ? (
                  <a href={ref} target="_blank" rel="noopener noreferrer" className="min-w-0 break-words font-mono text-[11.5px] leading-snug text-accent underline decoration-hair underline-offset-2 hover:decoration-accent [overflow-wrap:anywhere]">
                    {ref}
                  </a>
                ) : (
                  <span className="min-w-0 break-words font-mono text-[11.5px] leading-snug text-muted [overflow-wrap:anywhere]">{ref}</span>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

/** After a thumbs-down: collect what was wrong and teach it into memory. */
function FeedbackModal({
  question,
  onClose,
  onLearned,
}: {
  question?: string;
  onClose: () => void;
  onLearned?: () => void;
}) {
  const [text, setText] = useState("");
  // Private by default (knowledge scopes): a correction typed in the moment is one person's
  // view until they say otherwise, and a wrong one shared becomes citable org truth for
  // everyone. In open mode / signed out this changes nothing — there is no owner.
  const [share, setShare] = useState(false);
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const submit = async () => {
    const fact = text.trim();
    if (!fact || state === "busy") return;
    setState("busy");
    try {
      await api.learn(fact, question || "Answer feedback", share);
      setState("done");
      onLearned?.();
      setTimeout(onClose, 900);
    } catch {
      setState("error");
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-[2px]" onClick={onClose}>
      <div className="flex w-full max-w-[520px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2.5 border-b border-fill2 px-5 py-3.5">
          <ThumbsDown size={14} className="text-muted" />
          <div className="flex-1 text-[13px] font-semibold text-ink">Help QuickJoiner learn</div>
          <button onClick={onClose} aria-label="Close" className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted transition hover:text-ink">
            <X size={15} />
          </button>
        </div>
        <div className="px-5 py-4">
          <p className="mb-2.5 text-[12.5px] leading-relaxed text-muted">
            What was wrong, or what’s the correct answer? We’ll learn it into memory so the next answer is better.
          </p>
          <textarea
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
            }}
            rows={4}
            placeholder="e.g. The correct owner of the release calendar is Priya, not Sam…"
            className="w-full resize-none rounded-sm bg-fill px-3.5 py-2.5 font-sans text-[13.5px] leading-normal text-ink outline-none placeholder:text-faint focus:shadow-[0_0_0_1.5px_var(--accent-soft)]"
          />
          <label className="mt-3 flex cursor-pointer items-start gap-2 text-[12px] leading-relaxed text-muted">
            <input
              type="checkbox"
              checked={share}
              onChange={(e) => setShare(e.target.checked)}
              className="mt-0.5 accent-[var(--accent)]"
            />
            <span>
              Share with everyone in this workspace
              <span className="block text-[11.5px] text-faint">
                Off by default — kept to you, and only you can cite it.
              </span>
            </span>
          </label>
          <div className="mt-3 flex items-center justify-end gap-2">
            {state === "error" && <span className="mr-auto text-[11.5px] text-danger">Could not save — try again.</span>}
            {state === "done" && (
              <span className="mr-auto text-[11.5px] text-gold">
                Learned {share ? "and shared" : "privately"} — thank you.
              </span>
            )}
            <button onClick={onClose} className="rounded-full px-3.5 py-1.5 text-[12px] text-muted transition hover:text-ink">
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={!text.trim() || state === "busy" || state === "done"}
              className="flex items-center gap-1.5 rounded-full bg-accent px-4 py-1.5 text-[12px] font-semibold text-accent-ink transition hover:bg-accent-hi disabled:opacity-50"
            >
              {state === "busy" && <Loader2 size={13} className="animate-spin" />}
              Learn from this
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function MessageActions({
  text,
  refs,
  reaction,
  onReact,
  onViewSources,
}: {
  text: string;
  refs: string[];
  reaction?: Reaction;
  onReact: (r: Reaction) => void;
  onViewSources: () => void;
}) {
  return (
    <div className="mt-2 flex items-center gap-0.5 opacity-100 transition-opacity duration-150 focus-within:opacity-100 md:opacity-0 md:group-hover:opacity-100">
      <IconButton label="Download as markdown" onClick={() => downloadMarkdown(text)}>
        <Download size={14} />
      </IconButton>
      {refs.length > 0 && (
        <IconButton label={`View cited sources (${refs.length})`} onClick={onViewSources}>
          <Quote size={14} />
        </IconButton>
      )}
      <IconButton label="Good response" onClick={() => onReact("like")} active={reaction === "like"}>
        <ThumbsUp size={14} />
      </IconButton>
      <IconButton label="Bad response — tell us why" onClick={() => onReact("dislike")} active={reaction === "dislike"}>
        <ThumbsDown size={14} />
      </IconButton>
    </div>
  );
}

function AnswerBody({
  text,
  candidates,
  reaction,
  onReact,
  onViewSources,
}: {
  text: string;
  candidates?: CandidateItem[];
  reaction?: Reaction;
  onReact: (r: Reaction) => void;
  onViewSources: (refs: string[]) => void;
}) {
  const refused = REFUSAL.test(text.slice(0, 140));
  const book = new CiteBook();
  const blocks = renderMarkdown(text, book, { mermaid: true });
  // Candidate cards render against the SAME book, eagerly — before refs is read —
  // so their citation chips continue the answer's numbering and land in the same
  // sources modal (never a second citation renderer).
  const cards: CandidateCard[] = (candidates ?? []).map((c, i) => ({
    rank: c.rank,
    confidence: c.confidence,
    summaryNodes: renderInline(c.summary, book, `cand${i}`),
    sourceNodes: renderInline(c.sources.map((s) => `[${s}]`).join(" "), book, `candsrc${i}`),
  }));
  const refs = book.refs;

  return (
    <div>
      <button
        type="button"
        disabled={refs.length === 0}
        onClick={() => onViewSources(refs)}
        className={cn(
          "mb-3 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-mono text-[9.5px] uppercase tracking-[0.16em] transition",
          refused ? "bg-unknown-soft text-unknown" : "bg-gold-soft text-gold",
          refs.length > 0 && "hover:brightness-110",
        )}
      >
        {refused ? <CircleAlert size={12} /> : <Check size={12} />}
        {refused ? "Not learned yet" : refs.length ? `Grounded · ${refs.length} source${refs.length > 1 ? "s" : ""}` : "Grounded"}
      </button>
      <div className="break-words text-[15.5px] leading-[1.75]">{blocks}</div>
      {cards.length > 0 && <CandidateCarousel cards={cards} />}
      {!refused && (
        <MessageActions
          text={text}
          refs={refs}
          reaction={reaction}
          onReact={onReact}
          onViewSources={() => onViewSources(refs)}
        />
      )}
    </div>
  );
}

function Message({
  m,
  reaction,
  onReact,
  onViewSources,
  onOpenArtifact,
}: {
  m: Msg;
  reaction?: Reaction;
  onReact: (r: Reaction) => void;
  onViewSources: (refs: string[]) => void;
  onOpenArtifact?: (a: Artifact) => void;
}) {
  if (m.role === "user") {
    return (
      <div className="flex animate-rise justify-end">
        <div className="max-w-[78%]">
          <div className="whitespace-pre-wrap rounded-lg rounded-br-xs bg-raised px-4 py-3 text-[14.5px] leading-relaxed shadow-soft">
            {m.text}
          </div>
          {/* Attachments render directly under the question so they read as part of it. */}
          {m.attachments && m.attachments.length > 0 && <AttachmentChips attachments={m.attachments} />}
          {m.ts && <div className="mt-1.5 pr-1 text-right font-mono text-[10px] tabular-nums text-faint">{m.ts}</div>}
        </div>
      </div>
    );
  }

  if (m.role === "error") {
    return (
      <div className="flex animate-rise">
        <div className="rounded-lg bg-danger-soft px-4 py-3 font-mono text-[12.5px] text-danger">{m.text}</div>
      </div>
    );
  }

  return (
    <div className="group animate-rise">
      <div className="mb-2 flex items-center gap-2">
        <span className="h-[8px] w-[8px] rounded-full bg-accent shadow-[0_0_0_3px_var(--accent-soft)]" />
        <span className="text-[12px] font-semibold text-muted">QuickJoiner</span>
        {m.ts && <span className="font-mono text-[10px] tabular-nums text-faint">{m.ts}</span>}
      </div>

      {m.learning && (
        <div className="mb-3 rounded-sm bg-fill px-3.5 py-2.5">
          <div className="mb-1.5 flex items-center gap-2 text-[11.5px] text-muted">
            {m.learning.complete ? (
              <Check size={12} className="flex-shrink-0 text-accent" />
            ) : (
              <Loader2 size={12} className="flex-shrink-0 animate-spin text-accent" />
            )}
            <span className="truncate">{m.learning.label}</span>
            <span className="ml-auto flex-shrink-0 font-mono text-[10.5px] tabular-nums text-faint">
              {m.learning.complete
                ? "done"
                : m.learning.total > 0
                  ? `${m.learning.done} / ${m.learning.total} chunks`
                  : "extracting…"}
            </span>
          </div>
          <div className="h-[4px] w-full overflow-hidden rounded-full bg-fill2">
            <div
              className={cn(
                "h-full rounded-full bg-accent transition-[width] duration-200 ease-out",
                (m.learning.total === 0 && !m.learning.complete) && "animate-pulse2",
              )}
              style={{
                width: m.learning.complete
                  ? "100%"
                  : m.learning.total > 0
                    ? `${Math.round((m.learning.done / m.learning.total) * 100)}%`
                    : "35%",
              }}
            />
          </div>
        </div>
      )}
      {m.thinking && (
        <details className="mb-3 overflow-hidden rounded-sm bg-fill" open={m.streaming}>
          <summary className="cursor-pointer px-3.5 py-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted transition hover:text-ink">
            Reasoning trace
          </summary>
          <pre className="max-h-[150px] overflow-y-auto whitespace-pre-wrap px-3.5 pb-3 font-mono text-[11.5px] leading-normal text-muted">
            {m.thinking}
          </pre>
        </details>
      )}
      {m.tools && m.tools.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {m.tools.map((t, i) => (
            <span
              key={i}
              title={t}
              className="inline-flex max-w-[280px] items-center gap-1.5 truncate rounded-full bg-fill px-3 py-1 font-mono text-[10.5px] text-muted"
            >
              <span
                className={cn(
                  "h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent",
                  m.streaming && i === m.tools!.length - 1 && "animate-pulse2",
                )}
              />
              {t}
            </span>
          ))}
        </div>
      )}
      {m.answer != null ? (
        <AnswerBody text={m.answer} candidates={m.candidates} reaction={reaction} onReact={onReact} onViewSources={onViewSources} />
      ) : m.text != null ? (
        // plain agent note (wizard prompts, confirmations) — no grounding stamp
        <div className="text-[15px] leading-[1.75] text-ink">{renderMarkdown(m.text, new CiteBook())}</div>
      ) : (
        <div className={cn("whitespace-pre-wrap text-[15.5px] leading-[1.75]", m.streaming && "caret")}>
          {m.streamText || (m.streaming ? "" : "(no answer)")}
        </div>
      )}
      {m.artifact && onOpenArtifact && (
        <button
          onClick={() => onOpenArtifact(m.artifact!)}
          className="mt-3 inline-flex items-center gap-2 rounded-full bg-gold-soft px-4 py-2 font-mono text-[11px] uppercase tracking-[0.12em] text-gold transition hover:brightness-125"
        >
          <ScrollText size={13} />
          View {m.artifact.title}
        </button>
      )}
    </div>
  );
}

export function Chat({
  messages,
  memo,
  onOpenArtifact,
  onLearned,
}: {
  messages: Msg[];
  memo?: string | null;
  onOpenArtifact?: (a: Artifact) => void;
  onLearned?: () => void;
}) {
  const bottom = useRef<HTMLDivElement>(null);
  const [reactions, setReactions] = useState<Record<string, Reaction>>({});
  const [sourcesFor, setSourcesFor] = useState<string[] | null>(null);
  const [feedbackFor, setFeedbackFor] = useState<{ question?: string } | null>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const react = (id: string, question: string | undefined, r: Reaction) => {
    setReactions((prev) => {
      const next = { ...prev };
      if (next[id] === r) delete next[id];
      else next[id] = r;
      return next;
    });
    if (r === "dislike" && reactions[id] !== "dislike") setFeedbackFor({ question });
  };

  // Nearest preceding user message = the question that produced each answer.
  let lastQuestion: string | undefined;

  return (
    <div className="scroll-thin flex-1 overflow-y-auto px-5 py-8 md:px-10">
      <div className="mx-auto flex max-w-[1100px] flex-col gap-9">
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
        {memo && (
          <details className="rounded-sm bg-accent-soft px-4 py-3">
            <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-[0.14em] text-accent">
              Older turns condensed into memory
            </summary>
            <pre className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-muted">{memo}</pre>
          </details>
        )}
        {messages.map((m) => {
          if (m.role === "user") lastQuestion = m.text;
          const question = lastQuestion;
          return (
            <Message
              key={m.id}
              m={m}
              reaction={reactions[m.id]}
              onReact={(r) => react(m.id, question, r)}
              onViewSources={setSourcesFor}
              onOpenArtifact={onOpenArtifact}
            />
          );
        })}
        <div ref={bottom} />
      </div>
      {sourcesFor && <SourcesModal refs={sourcesFor} onClose={() => setSourcesFor(null)} />}
      {feedbackFor && (
        <FeedbackModal question={feedbackFor.question} onClose={() => setFeedbackFor(null)} onLearned={onLearned} />
      )}
    </div>
  );
}
