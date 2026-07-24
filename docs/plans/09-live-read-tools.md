# Plan 09 — Comprehensive read-only live tools (GitLab / GitHub / Azure DevOps)

> **STATUS: ◐ Phase 0 + P1 SHIPPED (2026-07-23); P2 outstanding.** The agent can now answer the
> ENUMERATION + CURRENT-STATE questions the GitLab/GitHub/ADO REST APIs expose — read-only, no mutating
> call. Shipped: `connectors/live_tools.py`, per-TYPE consolidation with a `project`/`repo` selector
> (`type_tools` classmethod + `app.py::connector_tools`), the GitLab 13-tool family, the **GitHub
> parallel family** (was just search+get_file), ADO `get_work_item`/`build_details`/`build_log`, and
> the enumeration/current-state→live-tool split in `agent/prompts.py`. Suite 597, live-verified. **P2
> is the only outstanding work** (see Phasing); when it lands, graduate the substance into CLAUDE.md +
> the connector bullets and delete this plan.

## Why

Learned (vector) memory holds an ingested *snapshot* and answers "what/why/how" conceptual questions
with citations. It is the wrong tool for two whole classes of question, which must go **live**:
- **Enumeration / filter** — "list all open MRs", "which branches has ticket #X", "who reviewed !88".
- **Current state** — "did the latest build pass", "is this MR mergeable", "link to the build",
  "what's failing in the pipeline".
Every one of those was a refusal or an empty answer until a narrow tool was bolted on. This plan
builds the whole read surface at once, in parallel families across the three systems.

## Principles (non-negotiable)

1. **Read-only.** GET calls only. No create/update/merge/close/comment/approve/retry/cancel. The tool
   layer is a *lens*, never a lever — the user was explicit. (A future "actions" plan, if ever, is
   separate and opt-in.)
2. **Live / current-state.** Complements memory; the prompt steers the model: enumeration + current
   state → live tool; conceptual/explanatory → `search_memory`.
3. **Bounded output.** Every tool caps its result (per-page limits; diffs/logs truncated with a
   "…truncated" marker) so a result can't overflow the model window (the Octopus-dashboard lesson).
4. **Parallel families.** GitLab and GitHub expose the *same* question set under parallel names, so
   "list open PRs/MRs", "did the build pass", "who reviewed" work regardless of host. ADO rounds out
   its own surface (work items + builds are its strength; MRs live in GitLab there).
5. **Consistent compact format.** One-line-per-item with a `web_url` link; stable field order.

## Phase 0 — the enabler: consolidate tools per connector TYPE (not per instance)

Today each connector **instance** emits its tools suffixed with the source name, so AppRiver's **3**
GitLab connectors × ~18 tools = **~54 GitLab tools** in the agent context — untenable (prompt bloat +
the model has to choose among near-duplicates). Fix before scaling the catalog:

- Register a live-tool set **once per connector type**, each tool taking a **`project`/`repo`
  selector** argument. With one connector of that type the selector defaults to it; with several the
  agent names the project (all AppRiver GitLab connectors share base_url + `$GITLAB_TOKEN`, differing
  only by `project`, so one tool set serves all three). The tool resolves the owning connector for
  base_url/credentials from the selector.
- `AppContext.build_agent` dedupes connector tools by type and injects the resolver.
- Net: ~18 GitLab + ~18 GitHub + ~14 ADO tools **total**, not ×N-connectors.
- *(Fallback if we defer this: keep per-instance but curate to ~8 essential each — still multiplies,
  so Phase 0 is strongly recommended first.)*

## The catalog

### GitLab / GitHub — parallel read families (GL name / GH name)

**A. Merge / pull requests**
- `list_merge_requests` / `list_pull_requests` — filters: state, author, assignee, reviewer,
  target_branch, label, milestone, search, max_results. *(MR list SHIPPED)*
- `get_merge_request` / `get_pull_request` — title, state, description, author, source→target,
  approvals/reviews, mergeable/conflicts, changes count, head-pipeline status, linked issues.
- `merge_request_discussions` / `pull_request_comments` — the review thread (bounded).
- `merge_request_changes` / `pull_request_files` — files changed + diffstat (bounded).
- `merge_request_approvals` / `pull_request_reviews` — who approved / requested changes; required count.

**B. Issues**
- `list_issues` — state, label, assignee, milestone, author, search. *(GL SHIPPED)*
- `get_issue` (+ comments).

**C. Commits & diffs**
- `list_commits` — ref, since, until, author, path.
- `get_commit` — message, author, date, stats, changed files.
- `compare` — from_ref, to_ref → ahead/behind + changed files (`repository/compare` / `compare`).

**D. Branches / tags / releases / milestones**
- `list_branches` — search → name, last commit, protected.
- `list_tags`.
- `list_releases` — name, tag, notes, date.
- `list_milestones` — title, state, due, progress.

**E. CI / pipelines / builds**
- `pipeline_status(ref)` / `workflow_runs(branch)` — latest run(s) + status + link. *(GL SHIPPED)*
- `pipeline_jobs(pipeline_id)` / `run_jobs(run_id)` — per-stage/job status, which job failed + link.
- `job_log(job_id)` — bounded tail of a job log ("why did it fail").
- `pipeline_for_commit(sha)` / `commit_checks(sha)` — CI state for a commit (GH check-runs / statuses).

**F. Repo / code / people / environments**  *(`search_code` + `get_file` already exist)*
- `project_info` / `repo_info` — description, default branch, visibility, topics, languages, open
  MR/issue counts, last activity.
- `list_tree(path, ref)` — directory listing.
- `list_members` / `list_contributors` — access + top contributors.
- `list_environments` / `list_deployments` — where it deploys + latest deploy status.

### Azure DevOps / TFS — round out the read surface
*(exist: `ado_query_work_items` (WIQL), `ado_search_code`, `ado_get_file`, `ado_build_status`)*
- `ado_get_work_item(id)` — fields, description, **acceptance criteria**, state, assignee,
  **relations** (dev-links → commits/branches/PRs), tags.
- `ado_work_item_comments(id)` — the discussion.
- `ado_list_builds(definition?, branch?, top)` — recent runs + result + link.
- `ado_build_details(build_id)` — timeline (stages/jobs) + result.
- `ado_build_log(build_id)` — bounded log tail.
- `ado_test_results(build_id)` — pass/fail summary.
- `ado_list_pipelines` — pipelines + the repo each builds.
- `ado_list_repos` — TFS Git repos.
- `ado_list_commits(repo, branch, top)` / `ado_get_commit(repo, sha)`.
- `ado_list_teams` / `ado_list_iterations(team)` — teams + sprints.
- *(No ADO PR tools — AppRiver's merge requests live in GitLab.)*

## Cross-cutting

- A shared bounded-line formatter + the existing retrying `util.get_json`; a small `_read_tool`
  factory per connector to kill boilerplate.
- Prompt guidance (in `agent/prompts.py`) teaching the enumeration/current-state → live-tool split so
  the model reaches for these instead of looping on `search_memory`.
- Tests: each converter/formatter is a pure function (mock `get_json`) — no live dependency, matching
  the existing connector-test convention; a per-type tool-name lockstep test.

## Phasing

- **Phase 0** — per-type consolidation + selector (the enabler).
- **Phase 1 (the questions users actually ask)** — get_merge_request/PR + reviews/approvals;
  pipeline_jobs + job_log; list_commits + compare; list_branches; project_info; **GitHub parity**
  for list_pull_requests / list_issues / workflow_runs / pipeline_jobs / job status; `ado_get_work_item`
  (+relations/comments) + `ado_build_details`/`ado_build_log`.
- **Phase 2** — releases/milestones, members/contributors, environments/deployments, test_results,
  tree listing, commit-level tools, remaining parity. **SHIPPED 2026-07-24** (GitLab 19-tool /
  GitHub 18-tool families; ADO list_repos/pipelines/commits/test_results).
- **Phase 3 — group/org-scoping. SHIPPED for GitLab 2026-07-24** (GitHub org-scoping parity is the
  remaining bit). Instead of one
  connector per repo, configure ONE GitLab connector at a **group** (e.g. `zix`) / one GitHub at an
  **org**, and let the `project`/`repo` selector resolve any repo **by name** dynamically via the
  group API (`GET /groups/{g}/projects?search=…&include_subgroups=true`, cached), plus group-scoped
  enumeration (`/groups/{g}/merge_requests`, `/groups/{g}/issues`, `/groups/{g}/search`). This turns
  "connect every repo" into "connect the org once". `make_resolver` gains a dynamic (API-backed) mode
  for a group-configured connector; live tools only (ingestion scope stays a separate, explicit
  choice — don't auto-ingest hundreds of repos). Contained extension of the Phase-0 resolver.

## Paste-in prompt

```
Build the read-only live-tools suite per docs/plans/09-live-read-tools.md. Start with Phase 0
(consolidate connector live-tools per TYPE with a project/repo selector; dedupe in
AppContext.build_agent), then Phase 1. Read-only ONLY — no mutating API calls. Bound every tool's
output. Keep GitLab/GitHub families parallel. Pure converters tested with a mocked get_json; add the
enumeration/current-state → live-tool split to agent/prompts.py. Update CLAUDE.md + README + the
connector bullets in the same change.
```
