import { Check, CircleAlert, ScrollText } from "lucide-react";
import { useEffect, useRef } from "react";
import type { Artifact } from "./ArtifactModal";
import { CiteBook, renderMarkdown } from "./markdown";
import { cn } from "./ui";

export interface Msg {
  id: string;
  role: "user" | "agent" | "error";
  text?: string; // user bubble / error / plain agent note
  answer?: string; // final grounded answer (markdown + [citations])
  artifact?: Artifact; // generated document (e.g. scrape report) viewable in the modal
  streaming?: boolean;
  streamText?: string; // live delta accumulation before the final answer arrives
  thinking?: string;
  tools?: string[];
  ts?: string;
}

const REFUSAL = /haven'?t learned|not (yet )?learned|don'?t (yet )?have|no.*(learned|in memory)/i;

/* Answer rendering lives in markdown.tsx (shared with the artifact modal);
 * here we add the provenance ledger and the grounded/not-learned stamp. */

/** The provenance ledger — numbered sources beside the answer (below it on small screens). */
function Ledger({ refs }: { refs: string[] }) {
  return (
    <aside className="mt-4 border-t border-gold-line pt-3 lg:mt-0 lg:border-l lg:border-t-0 lg:pl-5 lg:pt-1">
      <div className="mb-2.5 text-[10px] font-semibold uppercase tracking-[0.2em] text-gold">Sources</div>
      <ol className="flex flex-col gap-1.5">
        {refs.map((ref, i) => {
          const isUrl = /^https?:\/\//.test(ref);
          const row = (
            <span className="flex min-w-0 items-baseline gap-2">
              <span className="font-mono text-[10px] font-semibold tabular-nums text-gold">{i + 1}</span>
              <span className="min-w-0 break-words font-mono text-[10.5px] leading-snug text-muted [overflow-wrap:anywhere]">
                {ref}
              </span>
            </span>
          );
          return (
            <li key={i} title={ref}>
              {isUrl ? (
                <a href={ref} target="_blank" rel="noopener noreferrer" className="block hover:opacity-75">
                  {row}
                </a>
              ) : (
                row
              )}
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

function AnswerBody({ text }: { text: string }) {
  const refused = REFUSAL.test(text.slice(0, 140));
  const book = new CiteBook();
  const blocks = renderMarkdown(text, book, { mermaid: true });
  const refs = book.refs;

  return (
    <div>
      <span
        className={cn(
          "mb-3 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-mono text-[9.5px] uppercase tracking-[0.16em]",
          refused ? "bg-unknown-soft text-unknown" : "bg-gold-soft text-gold",
        )}
      >
        {refused ? <CircleAlert size={12} /> : <Check size={12} />}
        {refused ? "Not learned yet" : refs.length ? `Grounded · ${refs.length} source${refs.length > 1 ? "s" : ""}` : "Grounded"}
      </span>
      <div className={cn(refs.length > 0 && "lg:grid lg:grid-cols-[minmax(0,1fr)_200px] lg:gap-6")}>
        <div className="break-words text-[15.5px] leading-[1.75]">{blocks}</div>
        {refs.length > 0 && <Ledger refs={refs} />}
      </div>
    </div>
  );
}

function Message({ m, onOpenArtifact }: { m: Msg; onOpenArtifact?: (a: Artifact) => void }) {
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
    <div className="animate-rise">
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
        <AnswerBody text={m.answer} />
      ) : m.text != null ? (
        // plain agent note (wizard prompts, confirmations) — no grounding stamp
        <div className="text-[15px] leading-[1.75] text-ink">
          {renderMarkdown(m.text, new CiteBook())}
        </div>
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
}: {
  messages: Msg[];
  memo?: string | null;
  onOpenArtifact?: (a: Artifact) => void;
}) {
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages]);
  return (
    <div className="scroll-thin flex-1 overflow-y-auto px-5 py-8 md:px-10">
      <div className="mx-auto flex max-w-[840px] flex-col gap-9">
        {memo && (
          <details className="rounded-sm bg-accent-soft px-4 py-3">
            <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-[0.14em] text-accent">
              Older turns condensed into memory
            </summary>
            <pre className="mt-2 whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-muted">{memo}</pre>
          </details>
        )}
        {messages.map((m) => (
          <Message key={m.id} m={m} onOpenArtifact={onOpenArtifact} />
        ))}
        <div ref={bottom} />
      </div>
    </div>
  );
}
