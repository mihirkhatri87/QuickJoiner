import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "./ui";

/** One card's display data. summaryNodes/sourceNodes are pre-rendered by the parent
 * (AnswerBody) via renderInline against the answer's SHARED CiteBook — so candidate
 * citations join the same numbering and sources modal, and all book mutation happens
 * during the parent's render pass, before the grounded stamp reads book.refs. */
export interface CandidateCard {
  rank: number;
  confidence: number | null;
  summaryNodes: ReactNode[];
  sourceNodes: ReactNode[];
}

/** Ranked multi-angle candidate answers (plan 06 §C): one card at a time,
 * keyboard-navigable prev/next, instant swap under reduced motion. Confidence is
 * server-computed (evidence shape + corroboration) — null renders as "unscored". */
export function CandidateCarousel({ cards }: { cards: CandidateCard[] }) {
  const [active, setActive] = useState(0);
  if (cards.length === 0) return null;
  const c = cards[Math.min(active, cards.length - 1)];

  const go = (delta: number) =>
    setActive((a) => (a + delta + cards.length) % cards.length);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      go(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      go(1);
    }
  };

  return (
    <div
      role="group"
      aria-roledescription="carousel"
      aria-label="Candidate answers"
      tabIndex={0}
      onKeyDown={onKey}
      className="mt-4 overflow-hidden rounded-lg bg-fill p-4 outline-none focus-visible:shadow-[0_0_0_1.5px_var(--accent)]"
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-faint">
          Candidate {Math.min(active, cards.length - 1) + 1} of {cards.length}
        </span>
        {c.confidence != null ? (
          <span
            title="Server-computed from evidence shape + corroboration — not the model's self-report"
            className="rounded-full bg-gold-soft px-2 py-0.5 font-mono text-[10px] tabular-nums text-gold"
          >
            confidence {(c.confidence * 100).toFixed(0)}%
          </span>
        ) : (
          <span className="rounded-full bg-fill2 px-2 py-0.5 font-mono text-[10px] text-faint">unscored</span>
        )}
        <span className="ml-auto flex gap-1">
          <button
            type="button"
            aria-label="Previous candidate"
            disabled={cards.length < 2}
            onClick={() => go(-1)}
            className="flex h-7 w-7 items-center justify-center rounded-full text-muted transition hover:bg-fill2 hover:text-ink disabled:opacity-40"
          >
            <ChevronLeft size={14} />
          </button>
          <button
            type="button"
            aria-label="Next candidate"
            disabled={cards.length < 2}
            onClick={() => go(1)}
            className="flex h-7 w-7 items-center justify-center rounded-full text-muted transition hover:bg-fill2 hover:text-ink disabled:opacity-40"
          >
            <ChevronRight size={14} />
          </button>
        </span>
      </div>
      {/* key on the active index so the card remounts; animate only when motion is OK */}
      {/* [overflow-wrap:anywhere] contains long relationship chains and dotted
          entity names (AppRiver.Connector.Monitor → …) that would otherwise spill
          past the card edge — plain break-words only helps at word boundaries. */}
      <div key={c.rank} className={cn("motion-safe:animate-rise")}>
        <div className="text-[14.5px] leading-relaxed [overflow-wrap:anywhere]">{c.summaryNodes}</div>
        <div className="mt-2 text-[12.5px] text-muted [overflow-wrap:anywhere]">Sources: {c.sourceNodes}</div>
      </div>
      {cards.length > 1 && (
        <div className="mt-3 flex justify-center gap-1.5">
          {cards.map((_card, i) => (
            <button
              key={i}
              type="button"
              aria-label={`Go to candidate ${i + 1}`}
              onClick={() => setActive(i)}
              className={cn(
                "h-1.5 w-1.5 rounded-full transition",
                i === Math.min(active, cards.length - 1) ? "bg-accent" : "bg-fill2 hover:bg-faint",
              )}
            />
          ))}
        </div>
      )}
    </div>
  );
}
