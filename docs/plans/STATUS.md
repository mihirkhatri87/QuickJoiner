# Plan status — master tracker

Single source of truth for what's **outstanding** across every execution plan, plus a ledger of
plans that have shipped and been removed. Update this file (and the banner at the top of each
live plan) **in the same change that ships a slice** — see the CLAUDE.md house rules "Plans and
roadmaps are forward-looking" + "Keep the strategy & design docs live." Finished plans are
deleted, not kept as ✅ tombstones; their substance graduates into the architecture/strategy docs.
Last reconciled against the tree: **2026-07-17**.

Legend: ✅ complete · ◐ partial (code done, verification/related-work open) · ⏸ deferred/runbook · ⬜ not started

## Live plans (unbuilt work)

| # | Plan | Status | What's outstanding |
|---|------|--------|--------------------|
| 03 | [Slack + Teams connectors](03-slack-teams-connectors.md) | ⬜ | Entire plan. No `connectors/slack.py` / `connectors/msteams.py`, no FORM_SPECS/registry entries, no hooks.py Slack-scheme branch. |
| 04 | [Coverage fog + remediation](04-coverage-fog.md) | ⬜ | Entire plan incl. **Phase 0 frontend test harness** (Vitest/RTL/MSW — still zero FE tests). No `coverage.py`, `/api/coverage`, `frontend/src/fog.ts`, GraphView fog layer. Its data dependency (gaps) shipped. |
| 05 | [Eval on connected org](05-eval-on-connected-org.md) | ⏸ | Runbook not yet executed on a real org — the deferred "decide fine-tuning with data" step. Needs an org-connected machine + 30–50 authored cases + the C0–C4 config matrix. `qj eval --calibrate/--compare` are now the measurement tools for it. |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ◐ | Code shipped (all four phases A→D→B→C, 2026-07-17). Open: **§2.7 / AC #7 live check** — `resolve_entity('webroot connector')` merging fires at ingest time, pending the next clean Connector + Confluence re-sync; **§4 related work** — repo-graph refresh (`repo-graph-refresh-pending`), connect Nautical↔Stevedore directly. When these close, graduate the substance and delete the plan. |
| 07 | [Multimodal derive-to-text (vision/audio/video)](07-multimodal-media.md) | ⬜ | Entire plan (phases A–E). No `quickjoiner/media/` package, no `media` config section, no `analyze_media` tool, no `/api/upload`, no composer attach. Roadmap item #23. |
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
3. **Plan 05** — Run the retrieval/correlation eval matrix on a connected org; unblocks the
   deferred embedding/fine-tuning decision.
4. **Plan 06 §2.7 + §4** — the live merge check and related-work items above.
5. **Plan 07** — multimodal derive-to-text. Phases A+B alone deliver the conversational
   "summarize this recording" story; D unlocks diagram/audio knowledge at sync time.

Also outstanding (pre-existing): `docs/evals/multi-hop-crosssource.yaml` is referenced by a test
and the docs but the `docs/evals/` directory does not exist — `test_multi_hop_eval_set_file_loads`
fails on it. Either restore the file or remove the reference.
