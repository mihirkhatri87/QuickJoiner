/* Shared lightweight markdown renderer: **bold**, *italic*, `code`, headings,
 * lists, ``` fences and [citation] superscripts (collected into a CiteBook).
 * ```mermaid fences render as live diagrams when opts.mermaid is set.
 * Used by the chat answers and the artifact modal. */

import { Fragment } from "react";
import type { ReactNode } from "react";
import { MermaidBlock } from "./MermaidBlock";

/** Collects citation refs across one whole answer; same ref = same number. */
export class CiteBook {
  refs: string[] = [];
  private seen = new Map<string, number>();
  number(ref: string): number {
    let n = this.seen.get(ref);
    if (!n) {
      n = this.seen.size + 1;
      this.seen.set(ref, n);
      this.refs.push(ref);
    }
    return n;
  }
}

export function renderInline(text: string, book: CiteBook, keyBase: string): ReactNode[] {
  // Local regex: renderInline recurses (bold/italic bodies), and a shared
  // global regex's lastIndex would be clobbered mid-loop — infinite loop.
  const inline = /\[([^[\]\n]{2,200}?)\]|\*\*([^*\n]+?)\*\*|\*([^*\n]+?)\*|`([^`\n]+?)`/g;
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = inline.exec(text))) {
    if (m.index > last) out.push(<Fragment key={`${keyBase}t${last}`}>{text.slice(last, m.index)}</Fragment>);
    const key = `${keyBase}m${m.index}`;
    if (m[1] != null) {
      const n = book.number(m[1]);
      out.push(
        <sup
          key={key}
          title={m[1]}
          className="mx-0.5 inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-[4px] border border-gold-line bg-gold-soft px-[3px] align-super font-mono text-[9.5px] font-semibold leading-none text-gold"
        >
          {n}
        </sup>,
      );
    } else if (m[2] != null) {
      out.push(
        <strong key={key} className="font-semibold text-ink">
          {renderInline(m[2], book, key)}
        </strong>,
      );
    } else if (m[3] != null) {
      out.push(<em key={key}>{renderInline(m[3], book, key)}</em>);
    } else {
      out.push(
        <code key={key} className="rounded-[6px] bg-fill2 px-1.5 py-px font-mono text-[12.5px]">
          {m[4]}
        </code>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<Fragment key={`${keyBase}tend`}>{text.slice(last)}</Fragment>);
  return out;
}

const LIST_ITEM = /^\s*(?:[-*•]|\d+[.)])\s+/;
const HEADING = /^(#{1,4})\s+/;

export interface MarkdownOpts {
  mermaid?: boolean; // render ```mermaid fences as live diagrams
}

export function renderMarkdown(text: string, book: CiteBook, opts: MarkdownOpts = {}): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      const lang = line.trim().slice(3).trim().toLowerCase();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i += 1; // closing fence
      if (lang === "mermaid" && opts.mermaid) {
        blocks.push(<MermaidBlock key={k++} code={code.join("\n")} />);
      } else {
        blocks.push(
          <pre
            key={k++}
            className="my-2.5 overflow-x-auto rounded-sm bg-fill px-4 py-3 font-mono text-[12px] leading-relaxed"
          >
            {code.join("\n")}
          </pre>,
        );
      }
    } else if (HEADING.test(line)) {
      blocks.push(
        <div key={k++} className="mb-1 mt-3 text-[15.5px] font-semibold first:mt-0">
          {renderInline(line.replace(HEADING, ""), book, `h${k}`)}
        </div>,
      );
      i += 1;
    } else if (LIST_ITEM.test(line)) {
      const items: ReactNode[] = [];
      while (i < lines.length && LIST_ITEM.test(lines[i])) {
        const marker = lines[i].match(/^\s*(\d+)[.)]\s+/);
        items.push(
          <li key={items.length} className="flex gap-2">
            <span className="flex-shrink-0 font-mono text-[11px] leading-[1.9] text-accent">
              {marker ? `${marker[1]}.` : "—"}
            </span>
            <span className="min-w-0">{renderInline(lines[i].replace(LIST_ITEM, ""), book, `l${k}i${items.length}`)}</span>
          </li>,
        );
        i += 1;
      }
      blocks.push(
        <ul key={k++} className="my-1.5 flex flex-col gap-1">
          {items}
        </ul>,
      );
    } else if (line.trim() === "") {
      i += 1; // paragraph gap; margins handle the rhythm
    } else {
      const para: ReactNode[] = [];
      while (i < lines.length && lines[i].trim() !== "" && !LIST_ITEM.test(lines[i]) && !HEADING.test(lines[i]) && !lines[i].trim().startsWith("```")) {
        if (para.length) para.push(<br key={`b${i}`} />);
        para.push(...renderInline(lines[i], book, `p${k}l${i}`));
        i += 1;
      }
      blocks.push(
        <p key={k++} className="my-1.5 first:mt-0 last:mb-0">
          {para}
        </p>,
      );
    }
  }
  return blocks;
}
