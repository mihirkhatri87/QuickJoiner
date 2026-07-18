# Execution plans — highest-ROI features

Selected from the strategy pack (PRD wishlist, MARKET_ASSESSMENT, and the AI roadmap — now
`docs/AI_ROADMAP.md`, whose frontier process is the standing intake for new plans) by
ROI = (differentiation × user value × strategic leverage) ÷ (effort × risk). 2026-07-12.

Each plan file is self-contained: design, acceptance criteria, test matrix, and a
**ready-to-paste implementation prompt** for a fresh Claude Code session that encodes the
project's conventions, gotchas, and verification gates.

**Live status lives in [STATUS.md](STATUS.md)** — the master tracker reconciled against the
tree, incl. the ledger of shipped-and-removed plans. Each live plan file also carries a status
banner at its top. Only **unbuilt** work stays here: when a plan is fully done its file is deleted
and its substance graduates into the architecture/strategy docs (CLAUDE.md house rules). Legend:
✅ complete · ◐ partial · ⏸ deferred/runbook · ⬜ not started.

| # | Plan | Status | Effort | Depends on | Delivers |
|---|---|---|---|---|---|
| 03 | [Slack + Teams connectors](03-slack-teams-connectors.md) | ⬜ | ~5–6d | — | The #1 catalog gap closed, full mode ladder, thread-level documents |
| 04 | [Coverage fog + remediation](04-coverage-fog.md) | ⬜ | ~4–5d | gaps data (shipped) | The dark map on the real GraphView + one-click fixes; stands up the FE test harness |
| 05 | [Eval on connected org](05-eval-on-connected-org.md) | ⏸ | runbook | — | The "decide fine-tuning with data" matrix on a real corpus |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ◐ | ~7–9d | — | Ambiguity detection + clarifying questions, deterministic confidence scoring, a candidate-answer traversal UI — code shipped 2026-07-17; live check + related work open (see STATUS.md) |

**Shipped & removed** (substance graduated — see the STATUS.md ledger): **01** Knowledge-debt
backlog (2026-07-13), **02** Retrieval quality pack (2026-07-17). Suggested order for the rest:
03 → 04, with 05 anytime on a connected machine.

Rules that apply to every plan: full pytest suite green from `.venv`; Postgres parity via
dockerized pgvector whenever the catalog/stores change; `npm run build` clean whenever the
frontend changes; scratch workspaces and scratch ports for live verification (never
`~/.quickjoiner/default`, never port 8787); CLAUDE.md and the strategy docs updated with
every shipped slice; final reports map every acceptance criterion to its covering test
and name unmet items honestly.
