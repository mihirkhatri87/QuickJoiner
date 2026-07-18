# Plan status — master tracker

Single source of truth for what's **shipped**, **partial**, or **outstanding** across every
execution plan. Update this table (and the banner at the top of the individual plan file)
**in the same change that ships a slice** — see the CLAUDE.md house rule "Keep the plans
current." Last reconciled against the tree: **2026-07-17** (Plan 02 completed — B threshold
calibration + C alias query expansion).

Legend: ✅ complete · ◐ partial · ⏸ deferred/runbook · ⬜ not started

| # | Plan | Status | What's done | What's outstanding |
|---|------|--------|-------------|--------------------|
| 01 | [Knowledge-debt backlog](01-knowledge-debt-backlog.md) | ✅ | `gaps.py`, `gaps` table + `log_gap`/`list_gaps`/`resolve_gaps`, `/api/gaps` + resolve, `list_gaps` agent tool, Rail badge + `GapsPanel`, pg parity. Shipped 2026-07-13. | — |
| 02 | [Retrieval quality pack](02-retrieval-quality-pack.md) | ✅ | **A.** Contextual chunking (`pipeline.breadcrumb()`, `retrieval.contextual_chunks`, 2026-07-13). **B.** Threshold calibration — `harness.calibrate` + `qj eval --calibrate/--apply/--compare` (max-margin midpoint, thin-set warning, CI regression gate). **C.** Alias query expansion — `memory/expansion.py` + `retrieval.alias_expansion`, wired into `search_memory`/`api/search`. B+C shipped 2026-07-17. | — |
| 03 | [Slack + Teams connectors](03-slack-teams-connectors.md) | ⬜ | — | Entire plan. No `connectors/slack.py` / `connectors/msteams.py`, no FORM_SPECS/registry entries, no hooks.py Slack-scheme branch. |
| 04 | [Coverage fog + remediation](04-coverage-fog.md) | ⬜ | Dependency (Plan 01 gaps data) is ready. | Entire plan incl. **Phase 0 frontend test harness** (Vitest/RTL/MSW — still zero FE tests). No `coverage.py`, `/api/coverage`, `frontend/src/fog.ts`, GraphView fog layer. |
| 05 | [Eval on connected org](05-eval-on-connected-org.md) | ⏸ | Harness + eval layers exist (`qj eval [--agent]`, `hops`/`hop_coverage`). | Runbook not yet executed on a real org — the deferred "decide fine-tuning with data" step. Needs an org-connected machine + 30–50 authored cases + the C0–C4 config matrix. |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ✅ | All four phases A→D→B→C: `graph_path_candidates` + ambiguity text, `entity_evidence`-driven adjudicator, `confidence.py` (`score_edge`/`score_chain`), `candidates.py` + SSE event + `CandidateCarousel`. Shipped 2026-07-17. | **AC #7 / §1.D live check only:** `resolve_entity('webroot connector')` merging with `AppRiver.Connector.Web`/`.Unity` fires at ingest time — pending the next clean re-sync of Connector + Confluence. Code + fixtures done. |

## Genuinely outstanding work (the real backlog)

Everything not ✅ above, ordered by the README's suggested schedule and dependencies:

1. **Plan 03** — Slack + Teams connectors. No dependencies; the #1 catalog gap.
2. **Plan 04** — Coverage fog. Depends only on Plan 01 (done). Its Phase 0 also stands up the
   **first frontend test suite** — a prerequisite worth banking regardless of the fog UI.
3. **Plan 05** — Run the retrieval/correlation eval matrix on a connected org. Operational, not
   a code change; unblocks the deferred embedding/fine-tuning decision. Now easier to act on:
   Plan 02's `qj eval --calibrate/--compare` are the measurement tools for exactly this matrix.
4. **Plan 06 §2.7** — One live merge check after the next clean Connector + Confluence re-sync.
5. **Plan 06 §4 related work** — repo-graph refresh (`repo-graph-refresh-pending`), and connecting
   Nautical↔Stevedore directly once available. Improves confidence-score ground truth; not blocking.

Also outstanding (pre-existing, surfaced during Plan 02): `docs/evals/multi-hop-crosssource.yaml`
is referenced by a test and the docs but the `docs/evals/` directory does not exist — one test
(`test_multi_hop_eval_set_file_loads`) fails on it. Either restore the file or remove the reference.

Anything marked ✅ has no remaining work — don't reopen it. If a shipped feature later needs
changes, that's a new plan or a targeted fix, not "outstanding plan work."
