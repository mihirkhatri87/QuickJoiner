import { ArrowUp, BookmarkPlus, Paperclip, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Extensions the rolling Uploads connector can ingest — DOC_EXTENSIONS + ARCHIVE_EXTENSIONS +
// TEXT_EXTENSIONS from ingest/extract.py (IMAGE_EXTENSIONS deliberately excluded: those aren't
// readable yet, see the vision seam there). This only hints the OS file-picker dialog's filter —
// drag-and-drop is unaffected by it, and the server is the source of truth either way. Keep in
// sync with extract.py when its extension sets change, or a type it already supports (like the
// .zip archive expansion) silently can't be *picked*, only dropped.
const UPLOAD_ACCEPT =
  // office / PDF + archives
  ".pdf,.docx,.pptx,.xlsx,.zip," +
  // plain text / markup / data
  ".md,.markdown,.txt,.text,.rst,.log,.json,.jsonl,.ndjson,.csv,.tsv,.html,.htm,.xml,.css," +
  ".yaml,.yml,.toml,.ini,.cfg,.conf,.env,.editorconfig,.lock,.properties,.tfvars,.hcl,.tf," +
  // dependency manifests
  ".csproj,.vbproj,.fsproj,.sln,.props,.targets,.nuspec,.config,.mod,.kts,.gradle," +
  // docs-in-other-markup, transcripts, notebooks
  ".adoc,.asciidoc,.org,.tex,.bib,.vtt,.srt,.ipynb," +
  // code
  ".py,.js,.ts,.tsx,.jsx,.java,.cs,.go,.rb,.php,.rs,.c,.h,.cpp,.hpp,.sql,.proto,.graphql," +
  ".vue,.svelte,.scala,.kt,.swift,.r,.jl,.pl,.lua,.groovy,.dart,.ex,.exs,.erl,.clj,.fs,.vb,.m,.mm," +
  // shell / build scripts
  ".sh,.bash,.zsh,.fish,.ps1,.psm1,.bat,.cmd,.make,.cmake,.dockerfile";

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  pending,
  onAttachFiles,
  onRemovePending,
  uploadStatus,
  learnPending,
  onToggleLearnPending,
  scopeControl,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  disabled: boolean;
  // Per-question context files staged for the next question (filenames only); they upload on send.
  pending?: string[];
  onAttachFiles?: (files: File[]) => void;
  onRemovePending?: (idx: number) => void;
  /** Set while the staged files above are in flight — null the rest of the time. One request
   * covers every staged file, so all chips share the same phase/percent; there's no per-file
   * granularity to show without one request per file. "processing" means every byte has been
   * sent but the server is still extracting text, which is the multi-second gap users actually
   * hit on a several-MB file — see the api.uploadChatAttachments doc comment. */
  uploadStatus?: { phase: "uploading" | "processing"; pct: number } | null;
  /** "Also learn permanently": attachments are context for ONE question by design, so
   * ingesting them into memory stays an explicit, visible opt-in rather than a surprise. */
  learnPending?: boolean;
  onToggleLearnPending?: (next: boolean) => void;
  /** The scope chip (ScopePicker). Passed in rather than built here so the composer stays
   * a dumb input and App owns which slice of memory the conversation is asking about. */
  scopeControl?: React.ReactNode;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  // Guards against out-of-order responses (a slow request resolving after a newer one)
  // and against re-opening the dropdown right after a suggestion is accepted.
  const reqId = useRef(0);
  const justAccepted = useRef(false);

  const grow = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 180) + "px";
  };

  // Debounced fetch of completions as the user types a natural-language question.
  useEffect(() => {
    const q = value.trim();
    // Skip slash-commands (/qj, /scrape) and near-empty input.
    if (justAccepted.current || q.length < 2 || q.startsWith("/")) {
      setSuggestions([]);
      setOpen(false);
      return;
    }
    const id = ++reqId.current;
    const t = setTimeout(() => {
      api
        .suggest(q, 6)
        .then((r) => {
          if (id !== reqId.current) return; // stale response
          setSuggestions(r.suggestions);
          setActive(-1);
          setOpen(r.suggestions.length > 0);
        })
        .catch(() => {
          if (id === reqId.current) setOpen(false);
        });
    }, 120);
    return () => clearTimeout(t);
  }, [value]);

  const pickFiles = (list: FileList | null) => {
    if (!list || !onAttachFiles) return;
    const files = Array.from(list);
    if (files.length) onAttachFiles(files);
  };

  const accept = (s: string) => {
    justAccepted.current = true;
    onChange(s);
    setOpen(false);
    setSuggestions([]);
    setActive(-1);
    requestAnimationFrame(() => {
      justAccepted.current = false;
      ref.current?.focus();
      grow();
    });
  };

  const close = () => {
    setOpen(false);
    setActive(-1);
  };

  const showList = open && suggestions.length > 0;

  return (
    <div className="px-5 pb-6 pt-3 md:px-10">
      <div className="relative mx-auto max-w-[1100px]">
        {showList && (
          <ul
            role="listbox"
            className="absolute inset-x-0 bottom-full z-20 mb-2 overflow-hidden rounded-[18px] bg-panel py-1.5 shadow-panel backdrop-blur-xl"
          >
            <li className="flex items-center gap-1.5 px-4 pb-1 pt-0.5 font-mono text-[9.5px] uppercase tracking-[0.14em] text-faint">
              <Sparkles size={11} strokeWidth={2} /> Suggested questions
            </li>
            {suggestions.map((s, i) => (
              <li key={s} role="option" aria-selected={i === active}>
                <button
                  type="button"
                  // onMouseDown (not onClick) so it fires before the textarea blur.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    accept(s);
                  }}
                  onMouseEnter={() => setActive(i)}
                  className={
                    "flex w-full items-center gap-2 px-4 py-2 text-left text-[14px] leading-snug transition " +
                    (i === active ? "bg-fill2 text-ink" : "text-muted hover:text-ink")
                  }
                >
                  <span className="truncate">{s}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            close();
            onSend();
          }}
          onDragOver={(e) => {
            if (!onAttachFiles || uploadStatus) return;
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={(e) => {
            if (e.currentTarget.contains(e.relatedTarget as Node)) return;
            setDragOver(false);
          }}
          onDrop={(e) => {
            if (!onAttachFiles || uploadStatus) return;
            e.preventDefault();
            setDragOver(false);
            pickFiles(e.dataTransfer.files);
          }}
          className={
            "flex flex-col gap-1.5 rounded-[26px] bg-panel p-3 pl-3 shadow-panel backdrop-blur-xl transition-shadow focus-within:shadow-[0_0_0_1.5px_color-mix(in_srgb,var(--accent)_55%,transparent),0_24px_60px_-28px_rgba(0,0,0,.65)] " +
            (dragOver ? "shadow-[0_0_0_2px_var(--accent)]" : "")
          }
        >
          {scopeControl && <div className="flex flex-wrap gap-1.5 px-1.5 pt-0.5">{scopeControl}</div>}
          {/* Staged per-question context files — upload on send, shown beneath the question after. */}
          {pending && pending.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1.5 pt-0.5">
              {pending.map((name, i) => {
                const busy = Boolean(uploadStatus);
                const barPct = uploadStatus?.phase === "processing" ? 100 : uploadStatus?.pct ?? 0;
                return (
                  <span
                    key={`${name}-${i}`}
                    className="flex max-w-[240px] flex-col gap-1 rounded-2xl bg-fill2 py-1 pl-2.5 pr-1.5 text-[12px] text-muted"
                  >
                    <span className="flex items-center gap-1.5">
                      <Paperclip size={12} className="flex-shrink-0" />
                      <span className="min-w-0 flex-1 truncate">{name}</span>
                      {busy ? (
                        <span
                          className="flex-shrink-0 whitespace-nowrap font-mono text-[10px] tabular-nums text-faint"
                          aria-live="polite"
                        >
                          {uploadStatus!.phase === "processing" ? "processing…" : `${uploadStatus!.pct}%`}
                        </span>
                      ) : (
                        <button
                          type="button"
                          aria-label={`Remove ${name}`}
                          onClick={() => onRemovePending?.(i)}
                          className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full text-faint transition hover:bg-fill hover:text-ink"
                        >
                          <X size={12} />
                        </button>
                      )}
                    </span>
                    {busy && (
                      <span className="block h-[3px] w-full overflow-hidden rounded-full bg-fill">
                        <span
                          className={
                            "block h-full rounded-full bg-accent transition-[width] duration-200 ease-out " +
                            (uploadStatus!.phase === "processing" ? "animate-pulse" : "")
                          }
                          style={{ width: `${barPct}%` }}
                        />
                      </span>
                    )}
                  </span>
                );
              })}
              {onToggleLearnPending && (
                <button
                  type="button"
                  onClick={() => onToggleLearnPending(!learnPending)}
                  title={
                    learnPending
                      ? "These files will be added to learned memory permanently, as well as answering this question."
                      : "Attachments are context for this question only. Turn this on to also add them to memory permanently."
                  }
                  className={
                    "inline-flex items-center gap-1.5 rounded-full py-1 pl-2 pr-2.5 text-[11.5px] transition " +
                    (learnPending
                      ? "bg-accent-soft text-accent"
                      : "bg-fill2 text-faint hover:text-muted")
                  }
                >
                  <BookmarkPlus size={12} className="flex-shrink-0" />
                  {learnPending ? "Learning permanently" : "Ask only"}
                </button>
              )}
            </div>
          )}
          <div className="flex items-end gap-2.5">
          {onAttachFiles && (
            <>
              <input
                ref={fileRef}
                type="file"
                multiple
                accept={UPLOAD_ACCEPT}
                className="hidden"
                onChange={(e) => {
                  pickFiles(e.target.files);
                  e.target.value = ""; // allow re-selecting the same file
                }}
              />
              <button
                type="button"
                aria-label="Attach files as context"
                title={
                  uploadStatus
                    ? "Uploading the current attachment(s)…"
                    : "Attach files as context for your question (Word, PowerPoint, Excel, PDF, Markdown, …). Kept for this conversation only, not added to memory."
                }
                disabled={Boolean(uploadStatus)}
                onClick={() => fileRef.current?.click()}
                className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full text-muted transition hover:bg-fill2 hover:text-ink disabled:pointer-events-none disabled:opacity-40"
              >
                <Paperclip size={19} strokeWidth={2} />
              </button>
            </>
          )}
          <textarea
            ref={ref}
            rows={1}
            value={value}
            placeholder="Ask about this organization…"
            role="combobox"
            aria-expanded={showList}
            aria-autocomplete="list"
            onChange={(e) => {
              onChange(e.target.value);
              grow();
            }}
            onBlur={() => setTimeout(close, 120)}
            onKeyDown={(e) => {
              if (showList && e.key === "ArrowDown") {
                e.preventDefault();
                setActive((a) => (a + 1) % suggestions.length);
                return;
              }
              if (showList && e.key === "ArrowUp") {
                e.preventDefault();
                setActive((a) => (a <= 0 ? suggestions.length - 1 : a - 1));
                return;
              }
              if (showList && e.key === "Tab") {
                e.preventDefault();
                accept(suggestions[active >= 0 ? active : 0]);
                return;
              }
              if (e.key === "Escape" && showList) {
                e.preventDefault();
                close();
                return;
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (showList && active >= 0) {
                  accept(suggestions[active]); // highlighted suggestion -> fill, don't send
                } else {
                  close();
                  onSend();
                }
              }
            }}
            className="max-h-[180px] flex-1 resize-none bg-transparent py-2 font-sans text-[15.5px] leading-normal text-ink outline-none placeholder:text-faint"
          />
          <span className="hidden pb-3 font-mono text-[9.5px] uppercase tracking-[0.14em] text-faint sm:block">
            {showList ? "Tab ⇥" : "Enter ↵"}
          </span>
          <button
            type="submit"
            disabled={disabled}
            aria-label="Ask"
            className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full bg-accent text-accent-ink shadow-[0_6px_18px_-8px_var(--accent)] transition hover:bg-accent-hi disabled:opacity-40 disabled:shadow-none"
          >
            <ArrowUp size={19} strokeWidth={2.2} />
          </button>
          </div>
        </form>
      </div>
      <p className="mx-auto mt-3 max-w-[1100px] text-center text-[11.5px] text-faint">
        Answers are grounded in learned memory and <b className="font-semibold text-gold">cite their sources</b> — or
        say what hasn't been learned yet. Commands:{" "}
        <code className="font-mono text-[10.5px] text-gold">/qj &lt;control QuickJoiner in words&gt;</code>,{" "}
        <code className="font-mono text-[10.5px] text-gold">/ingest &lt;file path&gt;</code>,{" "}
        <code className="font-mono text-[10.5px] text-gold">/scrape &lt;url&gt;</code>.
      </p>
    </div>
  );
}
