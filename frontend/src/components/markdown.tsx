/* Lightweight, dependency-free markdown renderer (engine).
 *
 * Supports a broad GFM subset so it reads close to react-markdown + remark-gfm,
 * without the bundle or the runtime cost:
 *   inline — **bold** / __bold__, *italic* / _italic_, ~~strike~~, `code`,
 *            [text](url) links, ![alt](url) images, bare-URL and <url>
 *            angle-bracket autolinks, and citation superscripts: a bare [ref]
 *            or 【source: ref】 (the CJK lenticular form some models emit)
 *            collected into a CiteBook and rendered clickable (opens in a new
 *            tab) when the ref itself is a URL. <url> is NOT a citation form —
 *            models also use it to autolink a URL mentioned inside retrieved
 *            content (e.g. a PR link quoted from a wiki page), which is not
 *            the same as citing that page as a source.
 *   block  — # … ###### headings, - / * / 1. lists WITH nesting and - [ ] task
 *            items, > blockquotes (nested), GFM | pipe | tables, --- rules,
 *            ``` fences (```mermaid renders live when opts.mermaid is set).
 *
 * The public surface is the shared <Markdown> component (defined at the bottom
 * of this file) — `<Markdown text={md} />` is the org-wide entry point. The
 * engine (renderMarkdown / renderInline / CiteBook) is also exported for
 * citation-aware callers that need the CiteBook (e.g. the chat provenance
 * ledger reads book.refs after rendering). */

import { Fragment } from "react";
import type { ReactNode } from "react";
import { MermaidBlock } from "./MermaidBlock";

/** Collects citation refs across one whole answer; same ref = same number. */
export class CiteBook {
  refs: string[] = [];
  private seen = new Map<string, number>();
  number(ref: string): number {
    let n = this.seen.get(ref);
    if (n === undefined) {
      n = this.seen.size + 1;
      this.seen.set(ref, n);
      this.refs.push(ref);
    }
    return n;
  }
}

/** Strip an optional leading "source:" / "sources:" label from a citation ref. */
function cleanRef(ref: string): string {
  return ref.replace(/^\s*sources?\s*[:：]\s*/i, "").trim();
}

// One alternation over every inline construct. Ordered so the greediest /
// most-specific wins at a given position: image, then link (`[x](y)`), then the
// citation forms — bare `[ref]` and `【source: ref】` (models cite in either;
// every model we've seen picks a different one) — then `<url>` angle-autolinks
// (a plain link, NOT a citation: models also use this GFM syntax to make a URL
// *mentioned inside* a retrieved document clickable — e.g. a TFS/PR link quoted
// from a Confluence page — which is not the same as citing that source; treating
// it as a citation over-counted the page's own content as separate sources), then
// emphasis, code, bare autolink. Named groups keep the branch handling readable.
// A fresh RegExp is built per renderInline call because the function recurses
// (emphasis bodies) and a shared global's lastIndex would be clobbered.
const INLINE_SRC = [
  String.raw`!\[(?<imgAlt>[^\]\n]*)\]\((?<imgUrl>[^)\s]+)[^)]*\)`,
  String.raw`\[(?<linkText>[^\]\n]+?)\]\((?<linkUrl>[^)\s]+)[^)]*\)`,
  String.raw`\[(?<citeRef>[^[\]\n]{2,200}?)\]`,
  String.raw`【\s*(?<lentRef>[^【】\n]{2,200}?)】`,
  String.raw`<(?<angleUrl>https?:\/\/[^>\s]+)>`,
  String.raw`\*\*(?<boldA>[^\n]+?)\*\*`,
  String.raw`__(?<boldB>[^\n]+?)__`,
  String.raw`~~(?<strike>[^\n]+?)~~`,
  String.raw`\*(?<italA>[^*\n]+?)\*`,
  String.raw`(?<![A-Za-z0-9])_(?<italB>[^_\n]+?)_(?![A-Za-z0-9])`,
  "`(?<code>[^`\\n]+?)`",
  String.raw`(?<auto>https?:\/\/[^\s<>()\]]+)`,
].join("|");

const CITE_SUP =
  "mx-0.5 inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-[4px] " +
  "border border-gold-line bg-gold-soft px-[3px] align-super font-mono text-[9.5px] " +
  "font-semibold leading-none text-gold";

export function renderInline(text: string, book: CiteBook, keyBase: string): ReactNode[] {
  const inline = new RegExp(INLINE_SRC, "g");
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = inline.exec(text))) {
    if (m.index > last) out.push(<Fragment key={`${keyBase}t${last}`}>{text.slice(last, m.index)}</Fragment>);
    const key = `${keyBase}m${m.index}`;
    const g = m.groups!;
    if (g.imgUrl != null) {
      out.push(<img key={key} src={g.imgUrl} alt={g.imgAlt || ""} className="my-2 max-w-full rounded-sm" />);
    } else if (g.linkUrl != null) {
      out.push(
        <a
          key={key}
          href={g.linkUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline decoration-hair underline-offset-2 hover:decoration-accent"
        >
          {renderInline(g.linkText!, book, key)}
        </a>,
      );
    } else if (g.citeRef != null || g.lentRef != null) {
      const ref = cleanRef((g.citeRef ?? g.lentRef)!);
      if (ref.length < 2) {
        out.push(<Fragment key={key}>{m[0]}</Fragment>);
      } else {
        const n = book.number(ref);
        const sup = (
          <sup title={ref} className={CITE_SUP}>
            {n}
          </sup>
        );
        // Clickable when the citation ref is itself a URL — opens in a new tab;
        // hover still shows the source via the native title tooltip either way.
        out.push(
          /^https?:\/\//i.test(ref) ? (
            <a
              key={key}
              href={ref}
              target="_blank"
              rel="noopener noreferrer"
              title={ref}
              className="no-underline transition hover:brightness-110"
            >
              {sup}
            </a>
          ) : (
            <Fragment key={key}>{sup}</Fragment>
          ),
        );
      }
    } else if (g.angleUrl != null) {
      out.push(
        <a
          key={key}
          href={g.angleUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline decoration-hair underline-offset-2 hover:decoration-accent [overflow-wrap:anywhere]"
        >
          {g.angleUrl}
        </a>,
      );
    } else if (g.boldA != null || g.boldB != null) {
      out.push(
        <strong key={key} className="font-semibold text-ink">
          {renderInline((g.boldA ?? g.boldB)!, book, key)}
        </strong>,
      );
    } else if (g.strike != null) {
      out.push(
        <del key={key} className="text-muted">
          {renderInline(g.strike, book, key)}
        </del>,
      );
    } else if (g.italA != null || g.italB != null) {
      out.push(<em key={key}>{renderInline((g.italA ?? g.italB)!, book, key)}</em>);
    } else if (g.code != null) {
      out.push(
        <code key={key} className="rounded-[6px] bg-fill2 px-1.5 py-px font-mono text-[12.5px]">
          {g.code}
        </code>,
      );
    } else if (g.auto != null) {
      out.push(
        <a
          key={key}
          href={g.auto}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline decoration-hair underline-offset-2 hover:decoration-accent [overflow-wrap:anywhere]"
        >
          {g.auto}
        </a>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<Fragment key={`${keyBase}tend`}>{text.slice(last)}</Fragment>);
  return out;
}

// ---- block-level parsing ----

const HEADING = /^(#{1,6})\s+/;
const LIST_ITEM = /^(\s*)(?:[-*+•]|\d+[.)])\s+/;
const ORDERED = /^\s*(\d+)[.)]\s+/;
const TASK = /^\[([ xX])\]\s+/;
const HR = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const BLOCKQUOTE = /^\s*>\s?/;
const HEADING_SIZES = ["text-[17px]", "text-[15.5px]", "text-[14.5px]", "text-[13.5px]", "text-[13px]", "text-[12.5px]"];

export interface MarkdownOpts {
  mermaid?: boolean; // render ```mermaid fences as live diagrams
}

const indentOf = (line: string) => line.length - line.trimStart().length;

/** A GFM table separator row: only pipes/colons/dashes/spaces, ≥1 dash + ≥1 pipe. */
function isTableSep(line: string): boolean {
  const t = line.trim();
  return t.includes("|") && t.includes("-") && /^[\s|:-]+$/.test(t);
}

/** Split a table row into trimmed cells on unescaped, non-code-span pipes. */
function splitRow(line: string): string[] {
  const cells: string[] = [];
  let cur = "";
  let inCode = false;
  const s = line.trim();
  for (let j = 0; j < s.length; j++) {
    const ch = s[j];
    if (ch === "\\" && s[j + 1] === "|") {
      cur += "|";
      j += 1;
    } else if (ch === "`") {
      inCode = !inCode;
      cur += ch;
    } else if (ch === "|" && !inCode) {
      cells.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  cells.push(cur);
  if (cells[0].trim() === "") cells.shift();
  if (cells.length && cells[cells.length - 1].trim() === "") cells.pop();
  return cells.map((c) => c.trim());
}

const isTableStart = (lines: string[], i: number) =>
  lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]);

const isBlockStart = (lines: string[], i: number) => {
  const line = lines[i];
  return (
    line.trim() === "" ||
    line.trim().startsWith("```") ||
    HEADING.test(line) ||
    HR.test(line) ||
    BLOCKQUOTE.test(line) ||
    LIST_ITEM.test(line) ||
    isTableStart(lines, i)
  );
};

let keySeq = 0;

export function renderMarkdown(text: string, book: CiteBook, opts: MarkdownOpts = {}): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const k = keySeq++;

    if (line.trim().startsWith("```")) {
      const lang = line.trim().slice(3).trim().toLowerCase();
      const code: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i += 1; // closing fence
      if (lang === "mermaid" && opts.mermaid) {
        blocks.push(<MermaidBlock key={k} code={code.join("\n")} />);
      } else {
        blocks.push(
          <pre
            key={k}
            className="my-2.5 overflow-x-auto rounded-sm bg-fill px-4 py-3 font-mono text-[12px] leading-relaxed"
          >
            {code.join("\n")}
          </pre>,
        );
      }
    } else if (HEADING.test(line)) {
      const level = line.match(HEADING)![1].length;
      const size = HEADING_SIZES[level - 1];
      blocks.push(
        <div key={k} className={`mb-1 mt-3 font-semibold first:mt-0 ${size} ${level >= 6 ? "text-muted" : ""}`}>
          {renderInline(line.replace(HEADING, ""), book, `h${k}`)}
        </div>,
      );
      i += 1;
    } else if (HR.test(line)) {
      blocks.push(<hr key={k} className="my-4 border-0 border-t border-hair" />);
      i += 1;
    } else if (BLOCKQUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && BLOCKQUOTE.test(lines[i])) inner.push(lines[i++].replace(BLOCKQUOTE, ""));
      blocks.push(
        <blockquote key={k} className="my-2 border-l-2 border-accent pl-3 text-muted">
          {renderMarkdown(inner.join("\n"), book, opts)}
        </blockquote>,
      );
    } else if (isTableStart(lines, i)) {
      const header = splitRow(line);
      i += 2; // header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim() !== "" && lines[i].includes("|") && !lines[i].trim().startsWith("```")) {
        rows.push(splitRow(lines[i++]));
      }
      blocks.push(
        <div key={k} className="my-2.5 overflow-x-auto rounded-sm border border-hair">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b border-hair bg-fill2">
                {header.map((cell, c) => (
                  <th key={c} className="px-3 py-2 text-left align-top font-semibold text-ink">
                    {renderInline(cell, book, `th${k}c${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((cells, r) => (
                <tr key={r} className="border-b border-hair last:border-0">
                  {header.map((_, c) => (
                    <td key={c} className="px-3 py-2 align-top">
                      {renderInline(cells[c] ?? "", book, `td${k}r${r}c${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else if (LIST_ITEM.test(line)) {
      const [node, next] = renderList(lines, i, indentOf(line), book, opts);
      blocks.push(<Fragment key={k}>{node}</Fragment>);
      i = next;
    } else if (line.trim() === "") {
      i += 1; // paragraph gap; margins handle the rhythm
    } else {
      const para: ReactNode[] = [];
      let first = true;
      while (i < lines.length && !isBlockStart(lines, i)) {
        if (!first) para.push(<br key={`b${i}`} />);
        para.push(...renderInline(lines[i], book, `p${k}l${i}`));
        first = false;
        i += 1;
      }
      blocks.push(
        <p key={k} className="my-1.5 first:mt-0 last:mb-0">
          {para}
        </p>,
      );
    }
  }
  return blocks;
}

/** Parse a (possibly nested) list starting at `start`, whose items sit at
 * `baseIndent`. Deeper-indented lines under an item are re-parsed as that item's
 * child blocks (nested lists, continuation paragraphs). Returns [node, nextIndex]. */
function renderList(
  lines: string[],
  start: number,
  baseIndent: number,
  book: CiteBook,
  opts: MarkdownOpts,
): [ReactNode, number] {
  const ordered = ORDERED.test(lines[start]);
  const startNo = ordered ? parseInt(lines[start].match(ORDERED)![1], 10) : 1;
  const items: ReactNode[] = [];
  let i = start;

  while (i < lines.length) {
    if (lines[i].trim() === "") {
      // allow a blank between items only if the list continues at this indent
      let j = i + 1;
      while (j < lines.length && lines[j].trim() === "") j++;
      if (j < lines.length && LIST_ITEM.test(lines[j]) && indentOf(lines[j]) === baseIndent) {
        i = j;
        continue;
      }
      break;
    }
    const m = LIST_ITEM.exec(lines[i]);
    if (!m || indentOf(lines[i]) !== baseIndent) break;

    let content = lines[i].slice(m[0].length);
    const task = TASK.exec(content);
    if (task) content = content.slice(task[0].length);
    i += 1;

    // Gather deeper-indented / continuation lines belonging to this item.
    const childRaw: string[] = [];
    while (i < lines.length && (lines[i].trim() === "" || indentOf(lines[i]) > baseIndent)) {
      childRaw.push(lines[i++]);
    }
    while (childRaw.length && childRaw[childRaw.length - 1].trim() === "") childRaw.pop();

    let childBlocks: ReactNode[] = [];
    if (childRaw.length) {
      const dedent = Math.min(...childRaw.filter((l) => l.trim()).map(indentOf));
      childBlocks = renderMarkdown(childRaw.map((l) => l.slice(dedent)).join("\n"), book, opts);
    }

    const key = items.length;
    if (task) {
      items.push(
        <li key={key} className="ml-1 flex list-none items-start gap-2">
          <input
            type="checkbox"
            checked={task[1].toLowerCase() === "x"}
            readOnly
            className="mt-[3px] flex-shrink-0 accent-accent"
          />
          <span className="min-w-0">
            {renderInline(content, book, `li${key}`)}
            {childBlocks}
          </span>
        </li>,
      );
    } else {
      items.push(
        <li key={key} className={ordered ? "ml-5 list-decimal" : "ml-5 list-disc marker:text-accent"}>
          {renderInline(content, book, `li${key}`)}
          {childBlocks}
        </li>,
      );
    }
  }

  const cls = "my-1.5 flex flex-col gap-1";
  const node = ordered ? (
    <ol className={cls} start={startNo}>
      {items}
    </ol>
  ) : (
    <ul className={cls}>{items}</ul>
  );
  return [node, i];
}

// ---- shared component ----

export interface MarkdownProps extends MarkdownOpts {
  text: string;
  /** Reuse a CiteBook to read `.refs` after render (numbered, deduped, in order). */
  book?: CiteBook;
  /** Extra classes on the wrapper element. */
  className?: string;
}

/** The org-wide way to render markdown text: `<Markdown text={md} />`.
 * Renders a broad GFM subset (tables, task/nested lists, blockquotes,
 * strikethrough, links, autolinks, rules, ```mermaid) with zero runtime deps.
 * Pass `book` when you need the collected source list afterwards (e.g. a ledger);
 * otherwise one is created internally. */
export function Markdown({ text, book, className, ...opts }: MarkdownProps) {
  const b = book ?? new CiteBook();
  return <div className={className}>{renderMarkdown(text, b, opts)}</div>;
}
