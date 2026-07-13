import { BookmarkPlus, Check, Download, X } from "lucide-react";
import { CiteBook, renderMarkdown } from "./markdown";
import { cn } from "./ui";

export interface Artifact {
  title: string;
  markdown: string;
}

/** Full-screen viewer for generated markdown artifacts (scrape reports, …):
 * renders headings/lists/code plus live mermaid diagrams, and offers
 * download-as-.md and an explicit "learn into memory" action. */
export function ArtifactModal({
  artifact,
  learnState,
  onLearn,
  onClose,
}: {
  artifact: Artifact | null;
  learnState: "idle" | "busy" | "done";
  onLearn: () => void;
  onClose: () => void;
}) {
  if (!artifact) return null;

  const download = () => {
    const blob = new Blob([artifact.markdown], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = artifact.title.replace(/[^\w.-]+/g, "-").toLowerCase() + ".md";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-3 backdrop-blur-[2px] md:p-8"
      onClick={onClose}
    >
      <div
        className="flex max-h-full w-full max-w-[920px] flex-col overflow-hidden rounded-lg bg-panel shadow-panel backdrop-blur-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-fill2 px-5 py-3.5">
          <span className="h-[8px] w-[8px] flex-shrink-0 rounded-full bg-gold shadow-[0_0_0_3px_var(--gold-soft)]" />
          <div className="min-w-0 flex-1 truncate text-[14px] font-semibold text-ink">{artifact.title}</div>
          <button
            onClick={onLearn}
            disabled={learnState !== "idle"}
            className={cn(
              "flex items-center gap-1.5 rounded-full px-3.5 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] transition",
              learnState === "done"
                ? "bg-gold-soft text-gold"
                : "bg-accent text-accent-ink hover:bg-accent-hi disabled:opacity-60",
            )}
          >
            {learnState === "done" ? <Check size={13} /> : <BookmarkPlus size={13} />}
            {learnState === "done" ? "Learned" : learnState === "busy" ? "Learning…" : "Learn this"}
          </button>
          <button
            onClick={download}
            title="Download as markdown"
            className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted transition hover:text-ink"
          >
            <Download size={14} />
          </button>
          <button
            onClick={onClose}
            aria-label="Close"
            className="flex h-8 w-8 items-center justify-center rounded-full bg-fill text-muted transition hover:text-ink"
          >
            <X size={15} />
          </button>
        </div>
        <div className="scroll-thin overflow-y-auto px-6 py-5 text-[14.5px] leading-[1.75] md:px-8">
          {renderMarkdown(artifact.markdown, new CiteBook(), { mermaid: true })}
        </div>
      </div>
    </div>
  );
}
