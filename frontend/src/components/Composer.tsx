import { ArrowUp, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export function Composer({
  value,
  onChange,
  onSend,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  disabled: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
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
          className="flex items-end gap-2.5 rounded-[26px] bg-panel p-3 pl-5 shadow-panel backdrop-blur-xl transition-shadow focus-within:shadow-[0_0_0_1.5px_color-mix(in_srgb,var(--accent)_55%,transparent),0_24px_60px_-28px_rgba(0,0,0,.65)]"
        >
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
        </form>
      </div>
      <p className="mx-auto mt-3 max-w-[1100px] text-center text-[11.5px] text-faint">
        Answers are grounded in learned memory and <b className="font-semibold text-gold">cite their sources</b> — or
        say what hasn't been learned yet. Commands:{" "}
        <code className="font-mono text-[10.5px] text-gold">/qj &lt;control QuickJoiner in words&gt;</code>,{" "}
        <code className="font-mono text-[10.5px] text-gold">/scrape &lt;url&gt;</code>.
      </p>
    </div>
  );
}
