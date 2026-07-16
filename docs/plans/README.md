# Execution plans — highest-ROI features

Selected from the strategy pack (PRD wishlist, MARKET_ASSESSMENT, AI_ARCHITECTURE) by
ROI = (differentiation × user value × strategic leverage) ÷ (effort × risk). 2026-07-12.

Each plan file is self-contained: design, acceptance criteria, test matrix, and a
**ready-to-paste implementation prompt** for a fresh Claude Code session that encodes the
project's conventions, gotchas, and verification gates.

| # | Plan | Effort | Depends on | Delivers |
|---|---|---|---|---|
| 01 | [Knowledge-debt backlog](01-knowledge-debt-backlog.md) | ~4d | — | Refusals → clustered, remediable gap backlog (the moat loop's first gear) |
| 02 | [Retrieval quality pack](02-retrieval-quality-pack.md) | ~5d | — | Contextual index text, per-workspace gate calibration, alias query expansion — eval-gated |
| 03 | [Slack + Teams connectors](03-slack-teams-connectors.md) | ~5–6d | — | The #1 catalog gap closed, full mode ladder, thread-level documents |
| 04 | [Coverage fog + remediation](04-coverage-fog.md) | ~4–5d | **01** | The dark map on the real GraphView + one-click fixes; stands up the FE test harness |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ~7–9d | none to start | Ambiguity detection + clarifying questions, deterministic confidence scoring, a candidate-answer traversal UI — motivated by a real reproduced session failure (2026-07-15) |

Suggested schedule (3 weeks): 01 ∥ 02 → 03 → 04. Plan 06 was added later (2026-07-15) and
is not yet scheduled — revisit when ready.

Rules that apply to every plan: full pytest suite green from `.venv`; Postgres parity via
dockerized pgvector whenever the catalog/stores change; `npm run build` clean whenever the
frontend changes; scratch workspaces and scratch ports for live verification (never
`~/.quickjoiner/default`, never port 8787); CLAUDE.md and the strategy docs updated with
every shipped slice; final reports map every acceptance criterion to its covering test
and name unmet items honestly.
