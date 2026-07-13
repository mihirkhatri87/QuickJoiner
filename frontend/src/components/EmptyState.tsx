import { Check, CircleAlert, Sparkles, Zap } from "lucide-react";

const STARTERS = [
  "How do we deploy and roll back?",
  "What should I focus on in week one?",
  "Who owns the payments service?",
  "Where are the runbooks?",
];

/** Staggered entrance for the hero — one orchestrated moment, then quiet. */
const step = (i: number) => ({ animationDelay: `${i * 90}ms` });

export function EmptyState({
  learned,
  docs,
  sources,
  onConnect,
  onStarter,
}: {
  learned: boolean;
  docs: number;
  sources: number;
  onConnect: () => void;
  onStarter: (q: string) => void;
}) {
  const badge = (text: string) => (
    <span className="inline-flex items-center gap-2 rounded-full bg-accent-soft px-3.5 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-accent">
      <Sparkles size={12} />
      {text}
    </span>
  );

  if (!learned) {
    return (
      <div className="mx-auto mt-[7vh] max-w-[640px]">
        <div className="animate-rise" style={step(0)}>
          {badge("Nothing learned yet")}
        </div>
        <h1
          className="mb-4 mt-6 animate-rise font-display text-[clamp(32px,4.6vw,44px)] font-semibold leading-[1.08] tracking-tight"
          style={step(1)}
        >
          Connect your org, and I'll answer with <span className="text-gold">receipts</span>.
        </h1>
        <p className="mb-7 max-w-[560px] animate-rise text-[15.5px] leading-relaxed text-muted" style={step(2)}>
          QuickJoiner learns from the systems your team actually uses — code, tickets, wikis, CI/CD and
          logs — then answers your onboarding questions grounded only in what it learned. Connect a
          system to begin.
        </p>
        <div className="animate-rise" style={step(3)}>
          <button
            onClick={onConnect}
            className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-[14.5px] font-semibold text-accent-ink shadow-[0_10px_28px_-10px_var(--accent)] transition hover:bg-accent-hi"
          >
            <Sparkles size={16} /> Connect a system
          </button>
        </div>
        <div className="mt-10 grid animate-rise gap-2.5" style={step(4)}>
          <Promise icon={<Check size={17} />} tone="gold" title="Every claim is cited">
            Answers link back to the exact source they came from.
          </Promise>
          <Promise icon={<CircleAlert size={17} />} tone="unknown" title="It says when it doesn't know">
            No invented answers — just "haven't learned that yet," and what to connect.
          </Promise>
          <Promise icon={<Zap size={17} />} tone="accent" title="Ask across every system at once">
            One question spans code, tickets, wikis and deploys.
          </Promise>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-[7vh] max-w-[640px]">
      <div className="animate-rise" style={step(0)}>
        {badge(`${docs} docs · ${sources} systems learned`)}
      </div>
      <h1
        className="mb-4 mt-6 animate-rise font-display text-[clamp(32px,4.6vw,44px)] font-semibold leading-[1.08] tracking-tight"
        style={step(1)}
      >
        What would you like to know?
      </h1>
      <p className="mb-7 max-w-[560px] animate-rise text-[15.5px] leading-relaxed text-muted" style={step(2)}>
        Ask anything about this organization. I answer only from what I've learned, and every claim
        cites its source.
      </p>
      <div className="flex animate-rise flex-wrap gap-2" style={step(3)}>
        {STARTERS.map((q) => (
          <button
            key={q}
            onClick={() => onStarter(q)}
            className="rounded-full bg-fill px-4 py-2.5 text-[13.5px] text-muted transition hover:-translate-y-px hover:bg-fill2 hover:text-ink"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

function Promise({
  icon,
  tone,
  title,
  children,
}: {
  icon: React.ReactNode;
  tone: "gold" | "unknown" | "accent";
  title: string;
  children: React.ReactNode;
}) {
  const tones = {
    gold: "bg-gold-soft text-gold",
    unknown: "bg-unknown-soft text-unknown",
    accent: "bg-accent-soft text-accent",
  }[tone];
  return (
    <div className="flex items-start gap-3.5 rounded-lg bg-fill p-4">
      <span className={`flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-full ${tones}`}>
        {icon}
      </span>
      <div>
        <div className="text-[14px] font-semibold">{title}</div>
        <div className="mt-0.5 text-[13px] leading-relaxed text-muted">{children}</div>
      </div>
    </div>
  );
}
