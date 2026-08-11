import { Check, ChevronRight, Globe, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { PromotionRow } from "../types";
import { Button, cn, TextInput } from "./ui";

/** The review queue: documents their authors have offered to the whole organisation.
 *
 * A taught note is private to its author by default — right, and on its own a dead end,
 * because the useful half of what a joiner works out is exactly what the next joiner needs.
 * This is the path across, and it is a review rather than a button precisely because one
 * person's unreviewed assertion must not become citable org truth by accident.
 *
 * Two things about this panel are deliberate. Its rows are documents that are **still
 * private** — a reviewer can read them here, and only here, because their authors offered
 * them — so the whole panel is gated on a reviewer capability rather than an ordinary read.
 * And approving is described as publishing, not as copying: the same document, the same
 * citations, the same graph edges, now readable by everyone.
 */
export function PromotionsPanel({
  canReview,
  onFlash,
  onChanged,
}: {
  canReview: boolean;
  onFlash: (msg: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [rows, setRows] = useState<PromotionRow[] | null>(null);

  const refresh = async () => {
    if (!canReview) return setRows([]);
    try {
      setRows((await api.promotions()).promotions);
    } catch (e) {
      setRows([]);
      onFlash(`Could not load the review queue: ${(e as Error).message}`, false);
    }
  };
  useEffect(() => {
    refresh();
  }, [canReview]);

  if (!canReview) return null;
  if (rows === null) return <div className="px-1 text-[12px] text-faint">Loading…</div>;

  return (
    <div>
      <p className="mb-3 px-1 text-[11.5px] leading-relaxed text-muted">
        Notes people have offered to the whole organisation. They are still private to their
        authors — approving publishes the same document, keeping every citation and graph
        edge it already has.
      </p>
      {rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line/70 p-4 text-center text-[12px] text-muted">
          Nothing is waiting for review.
        </div>
      ) : (
        rows.map((r) => (
          <PromotionPlate
            key={r.doc_id}
            row={r}
            onFlash={onFlash}
            onChanged={() => {
              refresh();
              onChanged();
            }}
          />
        ))
      )}
    </div>
  );
}

function PromotionPlate({
  row,
  onFlash,
  onChanged,
}: {
  row: PromotionRow;
  onFlash: (msg: string, ok?: boolean) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  // The text is fetched on expand rather than with the list: a queue of twenty notes should
  // not ship twenty documents to render twenty titles.
  const expand = async () => {
    const next = !open;
    setOpen(next);
    if (next && text === null) {
      try {
        setText((await api.promotionText(row.doc_id)).text);
      } catch (e) {
        setText(`Could not read this document: ${(e as Error).message}`);
      }
    }
  };

  const decide = async (approve: boolean) => {
    setBusy(true);
    try {
      onFlash((await api.decidePromotion(row.doc_id, approve, reason)).message);
      onChanged();
    } catch (e) {
      onFlash((e as Error).message, false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-2 rounded-xl border border-line/70 bg-fill p-3">
      <button className="flex w-full items-start gap-2 text-left" onClick={expand}>
        <ChevronRight
          size={14}
          className={cn("mt-[3px] shrink-0 text-faint transition-transform", open && "rotate-90")}
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] text-ink">{row.title}</span>
          <span className="block font-mono text-[10.5px] text-faint">
            offered by {row.author || "someone"}
            {row.requested_at ? ` · ${row.requested_at.slice(0, 10)}` : ""}
          </span>
        </span>
      </button>

      {row.note && <p className="mt-2 pl-6 text-[11.5px] italic text-muted">“{row.note}”</p>}

      {open && (
        <>
          <pre className="scroll-thin mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-fill2 p-2 font-mono text-[11px] leading-relaxed text-muted">
            {text ?? "Reading…"}
          </pre>
          <TextInput
            className="mt-2"
            value={reason}
            placeholder="Optional note back to the author"
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <Button disabled={busy} onClick={() => decide(true)}>
              <Globe size={14} />
              Publish to everyone
            </Button>
            <Button disabled={busy} onClick={() => decide(false)}>
              <X size={14} />
              Decline
            </Button>
          </div>
        </>
      )}
      {!open && (
        <div className="mt-2 flex gap-2 pl-6">
          <Button disabled={busy} onClick={() => decide(true)}>
            <Check size={14} />
            Publish
          </Button>
        </div>
      )}
    </div>
  );
}
