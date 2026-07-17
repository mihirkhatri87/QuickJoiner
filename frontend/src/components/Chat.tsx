import { Check, CircleAlert, Download, Loader2, Quote, ScrollText, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CandidateItem } from "../types";
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
  ts?: string;
}

type Reaction = "like" | "dislike";

const REFUSAL = /haven'?t learned|not (yet )?learned|don'?t (yet )?have|no.*(learned|in memory)/i;

/* Answer rendering lives in markdown.tsx (the shared <Markdown> engine); here we
 * add the grounded/not-learned stamp, per-message hover actions (download,
 * view sources, like/dislike → learn from feedback), and the citation superscripts
 * carry native hover tooltips (title=source) — no dedicated side panel. */

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
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const submit = async () => {
    const fact = text.trim();
    if (!fact || state === "busy") return;
    setState("busy");
    try {
      await api.learn(fact, question || "Answer feedback");
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
          <div className="mt-3 flex items-center justify-end gap-2">
            {state === "error" && <span className="mr-auto text-[11.5px] text-danger">Could not save — try again.</span>}
            {state === "done" && <span className="mr-auto text-[11.5px] text-gold">Learned — thank you.</span>}
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
