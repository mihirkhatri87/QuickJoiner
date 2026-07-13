import { useEffect, useState } from "react";

let seq = 0;

/** Renders a ```mermaid fence as an SVG diagram. The mermaid library is
 * dynamically imported so it stays out of the main bundle; a diagram that
 * fails to parse (e.g. LLM syntax slip) falls back to a plain code block. */
export function MermaidBlock({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "dark",
          themeVariables: { fontFamily: "Geist Mono, monospace", fontSize: "13px" },
        });
        const { svg } = await mermaid.render(`qj-mmd-${seq++}`, code);
        if (alive) setSvg(svg);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [code]);

  if (failed) {
    return (
      <pre className="my-2.5 overflow-x-auto rounded-sm bg-fill px-4 py-3 font-mono text-[12px] leading-relaxed">
        {code}
      </pre>
    );
  }
  if (!svg) {
    return (
      <div className="my-2.5 rounded-sm bg-fill px-4 py-6 text-center font-mono text-[11px] uppercase tracking-[0.14em] text-faint">
        Rendering diagram…
      </div>
    );
  }
  return (
    <div
      className="my-3 overflow-x-auto rounded-sm bg-fill p-4 [&_svg]:mx-auto [&_svg]:max-w-full"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
