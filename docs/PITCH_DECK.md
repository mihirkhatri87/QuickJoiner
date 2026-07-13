# QuickJoiner — Pitch Deck (markdown master)

One slide per `##`. Presenter notes in blockquotes. Source of truth for claims:
`PRD.md`, `MARKET_ASSESSMENT.md`. 2026-07-11.

---

## 1 · QuickJoiner
**Your new senior engineer, productive in weeks — not months.**
Onboarding intelligence that learns your org's systems and answers only what it can prove.

> Cold open: live demo of a refusal. "Ask it something it can't know. Watch it say so —
> and tell you exactly which system to connect to fix that." No other AI demo starts with
> honesty.

## 2 · The problem
- A senior engineering hire takes **3–6 months** to reach full productivity; at a
  $200k+ loaded cost that's **$50–100k of ramp burn per hire** — times every hire.
- Their questions tax the best engineers: mentors lose ~10–20% of a quarter to lookups.
- The answers exist — scattered across repos, Jira, wikis, CI, deploy dashboards, and
  shorthand nobody wrote down ("nautical models").

## 3 · Why current AI fails here
- Suite copilots see one suite. Enterprise search sees documents, not **structure**.
- General assistants **invent org facts**; one hallucinated URL destroys trust forever.
- None of them will tell you what the org *doesn't* know.

## 4 · The product
Connect your systems → QuickJoiner learns them into a private memory + an
evidence-carrying knowledge graph → ask anything:
- **Grounded answers, every claim cited** to the exact document.
- **Honest refusal** — "I haven't learned that yet, connect Octopus to fix it."
- **Structure, not just text**: "how is proj-a related to NAUT-9?" → a proven 3-hop chain.
- **Onboarding artifacts**: architecture map, week-1 brief, roadmap digest, quick-wins
  report — generated, cited, exportable.

> Demo beats: (1) cited answer with provenance ledger, (2) refusal + gap suggestion,
> (3) Knowledge graph view — consumer→package→provider with evidence chips,
> (4) "/scrape a partner's docs site" → mermaid-diagrammed report → Learn it.

## 5 · Why we're different (today, shipped)
1. **Honesty contract in the architecture** — calibrated refusal gate + eval harness that
   scores refusal accuracy. Not a prompt. A measurable system property.
2. **Deterministic evidence graph** — dependencies, tickets, deploys parsed (not guessed);
   every edge cites its document; conversations enrich it under strict validation.
3. **Local-first** — `pip install`, data never leaves the laptop; same code scales to a
   team server with one env var. The air-gapped option Glean can't offer.
4. **Every-mode connectors** — API pull, webhooks, live tools, *your own authenticated
   browser session*, polite scraping. Works in locked-down enterprises.
5. **Speaks your dialect** — org naming conventions resolved everywhere
   ("nautical models" finds AppRiver.Nautical.Models — in search and in the graph).

## 6 · Market
- Wedge: engineering onboarding — ~1.5M senior software hires/yr globally in orgs >200 eng;
  even $2k/hire ramp tooling ≈ **$3B wedge**.
- Expansion: engineering knowledge ops (every engineer, not just new ones) → the
  enterprise-AI-assistant market the incumbents price at $30–60/seat/mo.
- Beachhead buyers: regulated industries that cannot adopt SaaS assistants (finance,
  defense, health) — underserved, compliance-driven, budget-holding.

## 7 · Competition
Glean / M365 Copilot / Unblocked / Onyx-class OSS. (Full matrix: MARKET_ASSESSMENT.md.)
**Positioning line:** *they optimize for always answering; we optimize for being right —
and for making what's missing visible and fixable.*

## 8 · The moat we're building (roadmap → PRD §7)
- **Ramp Index**: own the time-to-productive metric; manager dashboards (W1).
- **Knowledge-debt backlog**: refusals cluster into ranked, one-click-fixable gaps (W2).
- **The dark map**: fog-of-war coverage UX — see what your org doesn't know (W10).
- **People graph**: refusal → "ask Meena — here's the evidence she owns this" (W5).
- **Audit-replayable answers** for compliance (W9.2).
- **Per-org tuned retrieval**: embedding fine-tunes + calibrated thresholds from the org's
  own evals (W7) — quality-per-tenant no generic SaaS matches.

## 9 · Business model
- **OSS core** (local, single-user) — adoption engine, bottom-up.
- **Team** ($15–25/seat/mo): shared server, auth/SSO, analytics, gap backlog, Slack.
- **Enterprise** (custom): air-gapped/customer-VPC, audit replay, SCIM, people-graph
  governance, per-org tuning. Compliance tier priced on value, not seats.

## 10 · Traction & proof (current state, honest)
- Working end-to-end system: 13 connectors, hybrid retrieval, knowledge graph (3 phases),
  agent bridge, web UI with graph visualization — **178 automated tests**, dual-backend
  parity verified against live Postgres, UI flows browser-verified.
- Grounding validated live (grounded+cited answers AND correct refusals) on local models.
- Next proof gate: real-corpus eval program (50+ cases) — the numbers for slide 5.

## 11 · Roadmap (four releases)
R2 **Prove it** — eval program, 90% testability, contextual retrieval.
R3 **Team product** — analytics, gap backlog, Slack, people graph, AWS reference deploy.
R4 **Category maker** — temporal knowledge, ragless router, Orrery UX, multi-tenant cloud.
R5 **Compliance flagship** — audit replay, air-gap certification, SOC2 → regulated wins.

## 12 · The ask
Choose per audience: (a) design partners: 3 orgs, 10+ senior hires/quarter, measured ramp
pilots; (b) pre-seed: 12–18 months to R3+R4 with the team below.

## 13 · Team
AI product owner · 2 frontier RAG/LLM engineers · AWS architect · senior React engineer ·
UX lead. (This deck's claims are backed by a shipped codebase, not slideware.)

## Appendix A · Demo script (7 minutes)
1. `qj learn` a repo pair → ask "what depends on nautical models?" → cited graph answer.
2. Ask an unlearned question → refusal + suggested connector.
3. Knowledge view: alias search, evidence panel, fog concept mock (design/).
4. `/scrape` a docs site → mermaid report → Learn → re-ask, now grounded.
5. "How is PAY-123 related to checkout?" → graph_path chain with evidence per hop.
