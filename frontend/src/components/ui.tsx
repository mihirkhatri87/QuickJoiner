import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "ghost" | "danger";
export function Button({
  variant = "ghost",
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-full text-[13px] px-4 py-2 transition disabled:opacity-40 disabled:cursor-default disabled:shadow-none";
  const styles: Record<Variant, string> = {
    primary:
      "bg-accent text-accent-ink font-semibold shadow-[0_6px_18px_-8px_var(--accent)] hover:bg-accent-hi",
    ghost: "bg-fill text-muted hover:bg-fill2 hover:text-ink",
    danger: "bg-danger-soft text-danger hover:bg-[color-mix(in_srgb,var(--danger)_22%,transparent)]",
  };
  return (
    <button className={cn(base, styles[variant], className)} {...rest}>
      {children}
    </button>
  );
}

export function IconButton({
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-full text-muted transition hover:bg-fill2 hover:text-ink",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Eyebrow({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 px-1 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-faint">
      {children}
      {action && <span className="ml-auto normal-case tracking-normal">{action}</span>}
    </div>
  );
}

export function Field({
  label,
  hint,
  note,
  children,
}: {
  label: string;
  hint?: ReactNode;
  /** Right-aligned annotation on the label row (e.g. the default-value marker). */
  note?: ReactNode;
  children: ReactNode;
}) {
  // h-full + mt-auto keeps controls bottom-aligned across a two-column grid row: a label
  // that wraps to two lines (or carries a note) must not shove its input out of line with
  // the field beside it.
  return (
    <label className="mb-3 flex h-full flex-col">
      <span className="mb-1.5 flex items-baseline gap-2 px-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted">
        {label}
        {note && <span className="ml-auto whitespace-nowrap">{note}</span>}
      </span>
      <span className="mt-auto block">{children}</span>
      {hint && <span className="mt-1 block px-1 text-[11.5px] leading-snug text-faint">{hint}</span>}
    </label>
  );
}

export function TextInput({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "w-full rounded-sm border border-transparent bg-fill px-3 py-2.5 text-[13.5px] text-ink outline-none transition placeholder:text-faint focus:border-[color-mix(in_srgb,var(--accent)_55%,transparent)] focus:bg-raised",
        className,
      )}
      {...rest}
    />
  );
}

export function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      rows={3}
      className={cn(
        "w-full resize-y rounded-sm border border-transparent bg-fill px-3 py-2.5 text-[13.5px] leading-relaxed text-ink outline-none transition placeholder:text-faint focus:border-[color-mix(in_srgb,var(--accent)_55%,transparent)] focus:bg-raised",
        className,
      )}
      {...rest}
    />
  );
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "w-full appearance-none rounded-sm border border-transparent bg-fill px-3 py-2.5 text-[13.5px] text-ink outline-none transition focus:border-[color-mix(in_srgb,var(--accent)_55%,transparent)] focus:bg-raised",
        className,
      )}
      {...rest}
    >
      {children}
    </select>
  );
}

export const SYNC_OPTIONS: [string, string][] = [
  ["", "Manual only"],
  ["15", "Every 15 min"],
  ["30", "Every 30 min"],
  ["60", "Hourly"],
  ["360", "Every 6 hours"],
  ["1440", "Daily"],
];

export function schedLabel(min: number | null): string {
  if (!min) return "manual sync";
  if (min % 1440 === 0) return `every ${min / 1440}d`;
  if (min % 60 === 0) return `every ${min / 60}h`;
  return `every ${min}m`;
}
