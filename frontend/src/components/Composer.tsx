import { ArrowUp } from "lucide-react";
import { useRef } from "react";

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

  const grow = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 180) + "px";
  };

  return (
    <div className="px-5 pb-6 pt-3 md:px-10">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSend();
        }}
        className="mx-auto flex max-w-[840px] items-end gap-2.5 rounded-[26px] bg-panel p-3 pl-5 shadow-panel backdrop-blur-xl transition-shadow focus-within:shadow-[0_0_0_1.5px_color-mix(in_srgb,var(--accent)_55%,transparent),0_24px_60px_-28px_rgba(0,0,0,.65)]"
      >
        <textarea
          ref={ref}
          rows={1}
          value={value}
          placeholder="Ask about this organization…"
          onChange={(e) => {
            onChange(e.target.value);
            grow();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          className="max-h-[180px] flex-1 resize-none bg-transparent py-2 font-sans text-[15.5px] leading-normal text-ink outline-none placeholder:text-faint"
        />
        <span className="hidden pb-3 font-mono text-[9.5px] uppercase tracking-[0.14em] text-faint sm:block">
          Enter ↵
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
      <p className="mx-auto mt-3 max-w-[840px] text-center text-[11.5px] text-faint">
        Answers are grounded in learned memory and <b className="font-semibold text-gold">cite their sources</b> — or
        say what hasn't been learned yet. Commands:{" "}
        <code className="font-mono text-[10.5px] text-gold">/learn &lt;fact&gt;</code>,{" "}
        <code className="font-mono text-[10.5px] text-gold">/scrape &lt;url&gt;</code>,{" "}
        <code className="font-mono text-[10.5px] text-gold">/connect</code>.
      </p>
    </div>
  );
}
