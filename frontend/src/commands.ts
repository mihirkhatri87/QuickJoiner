/* Prompt-handling pipeline, stage by stage:
 *
 *   input → [active Flow interceptor] → [Command registry] → [agent chat]
 *
 * A Flow is a transient conversational state that gets first claim on the next
 * input (the /connect wizard, the post-/scrape follow-up question). Commands
 * are deterministic slash actions — no LLM round-trip. Anything unclaimed
 * falls through to the agentic /api/chat tool loop (App.tsx).
 *
 * Everything here reaches the app through CommandCtx, so adding a command is
 * one entry in buildCommands — no App.tsx surgery. */

import { api, streamScrape } from "./api";
import type { Artifact } from "./components/ArtifactModal";
import type { Msg } from "./components/Chat";
import { pushNote } from "./components/ThinkingTrace";
import { startWizard, wizardInput, type WizardResult, type WizardState } from "./wizard";

export interface CommandCtx {
  pushUser(text: string): void;
  say(text: string): void; // plain agent note (markdown, no grounding stamp)
  sayError(text: string): void;
  addAgentPlaceholder(): string; // streaming agent message; returns its id
  patchMessage(id: string, fn: (m: Msg) => Msg): void;
  setBusy(busy: boolean): void;
  refresh(): void; // reload sources + status counters
  setFlow(flow: Flow | null): void;
  openArtifact(artifact: Artifact): void;
}

/** A transient conversational state with first claim on the next input.
 * Return true if the input was consumed (the flow must pushUser itself);
 * return false to dismiss the flow and let commands/chat handle the input. */
export interface Flow {
  handle(text: string): Promise<boolean>;
}

export interface Command {
  name: string;
  match: RegExp;
  run(m: RegExpMatchArray, raw: string): Promise<void>;
}

const YES = /^(y|yes|yep|sure|ok(ay)?)$/i;
const NO = /^(n|no|nope|nah)$/i;

const hostOf = (url: string) => {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
};

/* ------------------------------------------------------------- /connect */

function wizardFlow(ctx: CommandCtx, state: WizardState): Flow {
  const apply = async (res: WizardResult): Promise<void> => {
    if (res.say) ctx.say(res.say);
    if (!res.state) {
      ctx.setFlow(null);
      return;
    }
    const st = res.state;
    if (!res.effect) return;

    if (res.effect.kind === "create") {
      const retrying = res.effect.skipTest;
      ctx.setBusy(true);
      try {
        const row = await api.createConnector({
          name: st.name,
          type: st.type!.type,
          options: st.values,
          shared: st.shared ?? false,
          skip_test: res.effect.skipTest,
        });
        st.step = "sync";
        const tested = (row as { test?: { message?: string } | null }).test?.message;
        ctx.say(
          `Connector **${st.name}** (\`${st.type!.type}\`) saved${tested ? ` — test: ${tested}` : ""}. ` +
            "Run a sync now to start learning from it? (yes/no)",
        );
        ctx.refresh();
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (retrying) {
          ctx.setFlow(null);
          ctx.sayError("Couldn't save the connector: " + msg);
        } else {
          st.step = "failsave";
          ctx.say(
            `Couldn't save: ${msg}\nIf that's a failed connection test, reply **save** to store the ` +
              "connector anyway (fix options later in Settings). Anything else cancels.",
          );
        }
      } finally {
        ctx.setBusy(false);
      }
    } else if (res.effect.kind === "sync") {
      ctx.setBusy(true);
      ctx.say(`Syncing **${st.name}**…`);
      try {
        const r = await api.syncSource(st.name!);
        ctx.say(`Sync finished: ${r.result}`);
      } catch (e) {
        ctx.sayError("Sync failed: " + String(e));
      } finally {
        ctx.setFlow(null);
        ctx.setBusy(false);
        ctx.refresh();
      }
    }
  };

  return {
    async handle(text: string) {
      ctx.pushUser(text);
      await apply(wizardInput(state, text));
      return true;
    },
  };
}

/* -------------------------------------------------------------- /scrape */

function scrapeFollowupFlow(
  ctx: CommandCtx,
  pending: { url: string; depth: number; maxPages: number },
): Flow {
  return {
    async handle(text: string) {
      ctx.setFlow(null); // one shot either way
      if (YES.test(text)) {
        ctx.pushUser(text);
        ctx.setBusy(true);
        const name =
          hostOf(pending.url).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") ||
          "scraped-site";
        try {
          await api.createConnector({
            name,
            type: "web_scrape",
            options: { start_urls: pending.url, max_depth: pending.depth, max_pages: pending.maxPages },
            shared: false,
            skip_test: true, // we literally just crawled it
            sync_interval_minutes: 1440,
          });
          ctx.say(`Connector **${name}** saved with a daily sync. Ingesting the site into memory now…`);
          const r = await api.syncSource(name);
          ctx.say(`Sync finished: ${r.result}. The site's content is now searchable memory.`);
        } catch (e) {
          ctx.sayError("Couldn't add the connector: " + String(e));
        } finally {
          ctx.setBusy(false);
          ctx.refresh();
        }
        return true;
      }
      if (NO.test(text)) {
        ctx.pushUser(text);
        ctx.say("Okay — kept as a one-off scrape. The report is still in the artifact viewer.");
        return true;
      }
      return false; // anything else dismisses the question; input falls through
    },
  };
}

async function runIngest(ctx: CommandCtx, path: string, raw: string): Promise<void> {
  ctx.pushUser(raw);
  ctx.setBusy(true);
  try {
    const r = await api.ingestLocalPath(path);
    if (r.ingested) {
      ctx.say(`Learned **${r.title}** — ${r.result ?? "ingested"}. It's searchable memory now.`);
      ctx.refresh();
    } else {
      // Honest about the difference between "failed" and "nothing readable in it".
      ctx.sayError(
        `Could not learn ${r.file || path}: ${r.reason ?? "no readable text in this file"}. ` +
          "A picture-only or scanned document needs vision support, which isn't enabled yet.",
      );
    }
  } catch (e) {
    ctx.sayError(`Could not ingest ${path}: ${String((e as Error).message)}`);
  } finally {
    ctx.setBusy(false);
  }
}

async function runScrape(ctx: CommandCtx, url: string, raw: string): Promise<void> {
  const depth = 4;
  const maxPages = 40;
  ctx.pushUser(raw);
  const id = ctx.addAgentPlaceholder();
  ctx.setBusy(true);
  try {
    await streamScrape({ url, depth, max_pages: maxPages }, (e) => {
      if (e.type === "status")
        ctx.patchMessage(id, (m) => ({ ...m, trace: pushNote(m.trace || [], e.data) }));
      else if (e.type === "delta")
        ctx.patchMessage(id, (m) => ({ ...m, streamText: (m.streamText || "") + e.data }));
      else if (e.type === "answer") {
        const art = { title: `Scrape report: ${hostOf(url)}`, markdown: e.data };
        ctx.patchMessage(id, (m) => ({
          ...m,
          streaming: false,
          streamText: undefined,
          text: `Crawled **${e.pages} pages** (depth ${depth}) and synthesized a report with diagrams. Saved to \`${e.path}\`.`,
          artifact: art,
        }));
        ctx.openArtifact(art);
        ctx.setFlow(scrapeFollowupFlow(ctx, { url, depth, maxPages }));
        ctx.say(
          `Want me to keep **${hostOf(url)}** fresh? I can save this as a persistent \`web_scrape\` ` +
            "connector with a daily sync so its content becomes learned memory. (yes/no)",
        );
      } else if (e.type === "error")
        ctx.patchMessage(id, (m) => ({ ...m, role: "error", text: "Scrape failed: " + e.data, streaming: false }));
    });
  } catch (err) {
    ctx.patchMessage(id, (m) => ({ ...m, role: "error", text: "Scrape failed: " + String(err), streaming: false }));
  } finally {
    ctx.setBusy(false);
    ctx.patchMessage(id, (m) => ({ ...m, streaming: false }));
  }
}

/** Open the /connect wizard, optionally preselecting a connector type (used by the
 * knowledge-gap "Connect <type>" CTA). Loads the catalog, then hands off to the flow. */
export async function startConnectFlow(ctx: CommandCtx, preselectType?: string): Promise<void> {
  ctx.setBusy(true);
  try {
    const types = await api.connectorTypes();
    const start = startWizard(types, preselectType);
    ctx.setFlow(wizardFlow(ctx, start.state));
    ctx.say(start.say);
  } catch (e) {
    ctx.sayError("Couldn't load the connector catalog: " + String(e));
  } finally {
    ctx.setBusy(false);
  }
}

/* ------------------------------------------------------------- registry */

export function buildCommands(ctx: CommandCtx): Command[] {
  // /connect and /learn were retired in favour of `/qj <natural language>` (plan 08): the agent
  // now creates connectors and teaches facts through QuickJoiner's own API, so "connect a git
  // repo" or "teach: deploys are on Fridays" work in plain language, RBAC-gated. /scrape stays
  // (it's a distinct streaming crawl+synthesis flow, not an API mutation). The wizard machine is
  // still reachable from the knowledge-gap "Connect <type>" CTA via startConnectFlow.
  return [
    {
      name: "scrape",
      match: /^\/scrape\s+(\S+)/i,
      run: (m, raw) => runScrape(ctx, m[1], raw),
    },
    {
      // Deterministic file ingestion. This exists because the agentic route kept failing:
      // asked to "ingest C:\...\deck.pptx" the model replied "I can't read files directly
      // from your computer" — it *can* (POST /api/uploads/local), it just didn't connect the
      // request to the capability. A slash command is the house's deterministic fast path,
      // and it cannot be talked out of doing its job. Quotes are stripped so a path pasted
      // from Explorer ("Copy as path" wraps it in quotes) works as-is.
      name: "ingest",
      match: /^\/ingest\s+(.+)$/i,
      run: (m, raw) => runIngest(ctx, m[1].trim().replace(/^["']|["']$/g, ""), raw),
    },
  ];
}
