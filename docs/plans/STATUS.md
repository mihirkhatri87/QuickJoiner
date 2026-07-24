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
| 05 | [Eval on connected org](05-eval-on-connected-org.md) | ◐ | **Step 1 (discovery) + step 2 (author cases) done 2026-07-23**: `docs/evals/multi-hop-crosssource.yaml` now holds 20 real cases mined from the fully-synced AppRiver graph (Connector/Nautical/Stevedore dependency + pubsub coupling, an LLM-triple ticket→code edge, Octopus deploys, Confluence, symbol lookup, `aka` aliases, 4 refusal cases). Retrieval-layer sanity run (read-only, no config change) against production: **16/16 answerable cases hit rank ≤2, grounded_recall 1.0, mrr 0.969** — confirms the authored cases are real and gradeable. Found a genuine result already: **refusal_accuracy 0.0 at the retrieval layer** — all 4 refusal questions clear `min_score=0.55` via topically-adjacent real docs (an "Annual DevSecOps Budget" page for a Kubernetes-budget question, a "Roadmap" page for a 2028-roadmap question, PTO tickets for a PTO question) — the known big-corpus leakage risk flagged in the 2026-07-07 smoke test, now confirmed at 23.8k-doc scale; whether the **agent layer** (LLM judgment on the actual doc text) still refuses correctly is the open question the C0 run answers. **Outstanding**: set up the dedicated `~/.quickjoiner/eval` workspace (methodology note 4 — avoid re-embed thrash on production) and run the C0–C4 config matrix with `--agent` (real LLM calls, hours of re-sync for C3/C4). Expand past 20 cases toward 30–50 once C0 results are in. |
| 06 | [Multi-angle, confidence-scored answers](06-multi-angle-confidence-scored-answers.md) | ◐ | Code shipped (all four phases A→D→B→C, 2026-07-17). Open: **§2.7 / AC #7 live check** — `resolve_entity('webroot connector')` merging fires at ingest time, pending the next clean Connector + Confluence re-sync; **§4 related work** — repo-graph refresh (`repo-graph-refresh-pending`), connect Nautical↔Stevedore directly. When these close, graduate the substance and delete the plan. |
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
3. **Plan 05** — Cases authored + retrieval-layer sanity-verified 2026-07-23 (see the plan row
   above); remaining work is standing up the dedicated eval workspace and running the C0–C4
   `--agent` matrix — unblocks the deferred embedding/fine-tuning decision.
4. **Plan 06 §2.7 + §4** — the live merge check and related-work items above.
5. **Plan 07** — multimodal derive-to-text. Phases A+B alone deliver the conversational
   "summarize this recording" story; D unlocks diagram/audio knowledge at sync time.
