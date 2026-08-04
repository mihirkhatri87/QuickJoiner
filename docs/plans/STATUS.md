# Plan status — master tracker

Single source of truth for what's **outstanding** across every execution plan, plus a ledger of
plans that have shipped and been removed. Update this file (and the banner at the top of each
live plan) **in the same change that ships a slice** — see the CLAUDE.md house rules "Plans and
roadmaps are forward-looking" + "Keep the strategy & design docs live." Finished plans are
deleted, not kept as ✅ tombstones; their substance graduates into the architecture/strategy docs.
Last reconciled against the tree: **2026-07-29**.

Legend: ✅ complete · ◐ partial (code done, verification/related-work open) · ⏸ deferred/runbook · ⬜ not started

## Live plans (unbuilt work)

| # | Plan | Status | What's outstanding |
|---|------|--------|--------------------|
| 03 | [Slack + Teams connectors](03-slack-teams-connectors.md) | ⬜ | Entire plan. No `connectors/slack.py` / `connectors/msteams.py`, no FORM_SPECS/registry entries, no hooks.py Slack-scheme branch. |
| 04 | [Coverage fog + remediation](04-coverage-fog.md) | ⬜ | Entire plan incl. **Phase 0 frontend test harness** (Vitest/RTL/MSW — still zero FE tests). No `coverage.py`, `/api/coverage`, `frontend/src/fog.ts`, GraphView fog layer. Its data dependency (gaps) shipped. |
| 05 | [Eval on connected org](05-eval-on-connected-org.md) | ◐ | **All stated deliverables now met** (2026-07-29): query-time matrix (2026-07-27) + C3 contextual-chunking leg + C4 LLM-triples leg (both 2026-07-28, n=3/arm) all complete with recommendations (see plan doc for full tables — hybrid/reranker/graph_expansion/contextual_chunks stay on, `graph.extract_triples` stays off); the IVF_PQ scoring defect the C3 run surfaced is **fixed** (`ann_refine_factor`, regression-tested crossing `ann_min_rows` for the first time); the case set grew 21→32 (4→12 refusal cases, clearing `CALIBRATION_MIN_CASES`, +2 relation-shaped person/team cases); `min_score` retuned 0.55→0.64 (a deliberately conservative user call below the calibrator's own 0.72 max-margin pick — zero answerable-recall cost vs. the old default). Two production-path bugs found along the way both fixed: `graph.triple_workers` default 4→16 (broker throughput), and LLM-proposed entity names no longer overwrite deterministic ones on conflict. **What's left is a documentation step, not eval work**: per the CLAUDE.md house rule, an emptied plan should be deleted with its substance graduated into CLAUDE.md/AI_ROADMAP/PRD/TEST_STRATEGY — deferred as a follow-up (CLAUDE.md's grounding-threshold + connectors bullets already carry the load-bearing substance). Also still open: root-cause the `webroot-connector-alias` false refusal (unresolved since 2026-07-27); re-test C4 with the new relation-shaped cases if ever revisited. **2026-07-31**: the retune's delivery gap is closed — it had reached no existing workspace (`save_config` materialized every field), so the live corpus ran 0.55 for two days; the settings blob is now sparse with a logged one-time adoption of superseded defaults, verified against a copy of the real workspace. |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ◐ | Code shipped (all four phases A→D→B→C, 2026-07-17); suites green (`test_confidence`/`test_candidates`/`test_graph`/`test_entity_resolution`). **Re-checked 2026-07-30: everything outstanding is live-org work, not code** — AC #1 (worked example against the real Connector/Nautical/Stevedore graph), AC #7 / §1.D (`resolve_entity('webroot connector')` merging, fires only at ingest time), §4 related work (repo-graph refresh, connect Nautical↔Stevedore). Not reproducible on a dev machine, so the plan stays open until it runs on the user's org. One premise corrected: `GRAPH_EXTRACTOR_VERSION`/`documents.graph_version` (shipped after this plan) mean an **ordinary** sync re-runs entity resolution for any re-provided doc below the current version — a *clean* re-sync is only needed for docs already stamped current. When these close, graduate the substance and delete the plan. |
| 07 | [Multimodal derive-to-text (vision/audio/video)](07-multimodal-media.md) | ⬜ | Entire plan (phases A–E). No `quickjoiner/media/` package, no `media` config section, no `analyze_media` tool, no `/api/upload`, no composer attach. Roadmap item #23. |
| 09 | [Comprehensive read-only live tools](09-live-read-tools.md) | ◐ | **Phase 0 + P1 + P2 shipped 2026-07-23/24.** `connectors/live_tools.py`; tools consolidated per TYPE with a `project`/`repo` selector (`type_tools` classmethod; `app.py::connector_tools` groups by type). GitLab **19-tool** family + GitHub **18-tool** parallel family + ADO (get_work_item/build_details/build_log/list_repos/list_pipelines/list_commits/test_results). Read-only; prompt teaches the enumeration/current-state→live split. **Phase 3 (group/org-scoping) shipped 2026-07-24 for GitLab:** a `group=zix` connector resolves any repo by name (`_resolve_project_in_group`, ranked) + enumerates group-wide; ingests only its `projects` list. Live-verified against real zix; AppRiver's 3 GitLab connectors collapsed into one **Gitlab Zix**. Suite **598**. **Outstanding:** GitHub org-scoping parity (same pattern), then graduate + delete. |
| 08 | [Self-control API + RBAC](08-self-control-api-and-rbac.md) | ✅→graduate | Stages 1–5 **shipped + verified 2026-07-21**: RBAC (`rbac.py`, `role` column, `Auth` roles); generic `qj_api`/`qj_api_reference` dispatch (`agent/control.py`) + permanent control connector; HTTP `_require` on all mutating routes + `GET/PATCH /api/auth/users` + Postman/Bruno runbooks; frontend `/qj` (replaced `/connect`+`/learn`), People & access admin UI, control plate, role-gated affordances; CLAUDE/README graduated. Suite **530 passed**, frontend typecheck+build green. **Live-verified 2026-07-21** via curl (full RBAC flow: open→admin, first-user→admin, viewer writes 403, admin role admin, control-connector delete 409) + headless Playwright (admin sees People & access + control plate + Danger zone; viewer sees neither; `/qj` help note + updated composer commands). Only environment-gated remainder: the full `/qj`→agent→`qj_api` round-trip needs a live LLM (same standing gap as the untested Anthropic path; `qj_api` mechanics are unit-tested). Substance graduated into CLAUDE.md — this plan can now be deleted. |

## Shipped & removed (graduated into the architecture/strategy docs)

Plan files deleted once nothing was outstanding (git preserves them). "Lives now in" points to
the current-behavior source of truth.

| # | Plan | Shipped | Lives now in |
|---|------|---------|--------------|
| 01 | Knowledge-debt backlog | 2026-07-13 | `CLAUDE.md` (gaps.py architecture bullet + `/api/gaps`), `docs/PRD.md` W2 (✅ DONE) |
| 02 | Retrieval quality pack (contextual chunks + threshold calibration + alias query expansion) | A 2026-07-13, B+C 2026-07-17 | `CLAUDE.md` (evals-harness + memory/expansion bullets, min_score note), `docs/AI_ROADMAP.md` §2 Shipped ledger (#1/#3/#4), `docs/TEST_STRATEGY.md` |

## The real backlog, ordered

1. **Plan 03** — Slack + Teams connectors. No dependencies; the #1 catalog gap.
2. **Plan 04** — Coverage fog. Its Phase 0 also stands up the **first frontend test suite** — a
   prerequisite worth banking regardless of the fog UI.
3. **Plan 05** — Complete as of 2026-07-29 (see the tracker row above); only a documentation
   graduation step (delete-plan + fold substance into CLAUDE.md/AI_ROADMAP/PRD) remains, deferred
   as its own follow-up rather than rushed. Not blocking anything else in this backlog.
4. **Plan 06 AC #1/#7 + §4** — live-org verification only; **cannot be advanced on a dev
   machine**, so it should not be picked up as ordinary backlog work. It runs the next time
   the user syncs their own Connector + Confluence connectors.
5. **Plan 07** — multimodal derive-to-text. Phases A+B alone deliver the conversational
   "summarize this recording" story; D unlocks diagram/audio knowledge at sync time.
