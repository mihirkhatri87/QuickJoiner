# Plan 03 — Slack + Microsoft Teams Connectors (MARKET_ASSESSMENT Appendix A, priority #1 and #9)

Effort: ~5–6 dev-days (Slack 3, Teams 2–3) · Dependencies: none
Honest scope note: converters and sync logic are fully unit-testable offline (project
convention); live verification against real Slack/Teams tenants is a separate manual
smoke the implementer must list as "not verified" if no tenant is available.

## 1. Shared design

Both follow the connector contract (`connectors/base.py`): `test()`, `sync(state)`,
`tools()`, `handle_event(payload)`, `modes`. Both convert **threads** (not single
messages) into Documents — a thread is the atomic unit of tribal knowledge:

```
Document(
  uri  = permalink of thread root (stable),
  title= "#channel · first 60 chars of root message",
  text = "[#channel] thread started by @name on <date>\n
          @name (10:32): message\n@other (10:35): reply …",
  kind = "doc", updated_at = last reply ts)
```
Unthreaded traffic rolls up into per-channel per-day digest Documents
(`uri = <workspace>/archives/<channel>/<yyyy-mm-dd>`). User ids are resolved to display
names via a cached directory lookup (a doc full of `U03AB12CD` is retrieval-poison).
Ticket keys in messages feed the knowledge graph for free (pipeline regex).
Privacy defaults: **public channels only**; explicit `channels` allowlist option; private
content requires both the token scope AND `include_private=true`.

## 2. Slack (`quickjoiner/connectors/slack.py`, type `slack`, modes PULL|PUSH|LIVE)

Options (FORM_SPECS entry must match exactly what the code reads):
`bot_token`* (secret, env:SLACK_BOT_TOKEN) · `channels` (list, optional allowlist of
names) · `include_private` (default false) · `signing_secret` (secret — Events API) ·
`user_token` (secret, optional — enables the live search tool) ·
`max_messages_per_channel` (default 2000).

- **PULL** `sync(state)`: `conversations.list` (types per include_private) → filter by
  allowlist → per channel `conversations.history` paginated by cursor with
  `oldest = state["oldest_<channel_id>"]`; for roots with `thread_ts`,
  `conversations.replies`; group → thread/day documents; set new oldest per channel.
  Rate limits: respect `Retry-After` on 429 (single retry then continue — a slow channel
  must not kill the sync). Directory: `users.list` once per sync, cached on self.
- **LIVE** `tools()`: `slack_search(query)` via `search.messages` — **registered only
  when `user_token` is configured** (search API rejects bot tokens); returns formatted
  hits with permalinks for citation.
- **PUSH** `handle_event(payload)`: Slack Events envelope; `url_verification` → return
  the challenge (see hooks change below); `event_callback` with `event.type=="message"`
  (skip edits/deletes/bot_message subtypes) → single-message Document appended under the
  day-digest uri convention.
- **hooks.py extension** (the one framework change): per-source scheme — when the
  source's options contain `signing_secret`, verify Slack's scheme
  (`v0=HMAC_SHA256(signing_secret, "v0:{timestamp}:{body}")` from `X-Slack-Signature` +
  `X-Slack-Request-Timestamp`, reject |now-ts|>300s) instead of the generic
  `X-QJ-Signature`; and if the verified body is `{"type":"url_verification"}` respond
  `{"challenge": …}` without ingesting.
- `test()`: `auth.test` → ok/error with the Slack error string.

Pure converters (module level, no HTTP): `thread_document(channel_name, root, replies,
users)`, `day_digest_document(channel_name, date, messages, users)`,
`event_message_document(event, users)` — these carry the unit-test load.

## 3. Microsoft Teams (`quickjoiner/connectors/msteams.py`, type `teams`, modes PULL|LIVE)

Auth reality (be honest in help text): application-permission access to channel messages
requires Microsoft's **protected-API approval** (`ChannelMessage.Read.All` + model=A/B) —
many orgs won't have it. Support two modes:
- `access_token` (secret, env:MS_GRAPH_TOKEN) — a delegated token supplied by the org
  (simplest; works with `Team.ReadBasic.All` + `ChannelMessage.Read.All` delegated).
- OR app-only: `tenant_id`, `client_id`, `client_secret` (secret, env:MS_CLIENT_SECRET) —
  token via `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`
  (client_credentials, scope `https://graph.microsoft.com/.default`), cached until expiry.

- **PULL**: teams via `/v1.0/me/joinedTeams` (delegated) or
  `/v1.0/groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')` (app-only);
  channels `/teams/{id}/channels`; messages `/teams/{id}/channels/{id}/messages/delta`
  storing the **deltaLink** in sync state (`delta_<team>_<channel>`) — Graph's delta is
  the incremental mechanism, don't reinvent timestamps; replies
  `/messages/{id}/replies` for roots. Strip HTML bodies to text (BeautifulSoup, already
  a dependency) — Teams bodies are HTML.
- **LIVE**: `teams_search(query)` via `POST /v1.0/search/query`
  (entityTypes ["chatMessage"]) — delegated tokens only; register conditionally.
- **PUSH**: Graph change-notification subscriptions need a public HTTPS endpoint,
  validationToken handshake, and ≤60-min renewals — **explicitly out of scope**; document
  in the connector docstring and FORM_SPECS blurb (generic /hooks push still accepted for
  custom relays). modes = PULL|LIVE only.
- `test()`: token fetch (app-only) or `/v1.0/me` / `/v1.0/organization` probe.
- Pure converters mirror Slack's: `channel_thread_document(team, channel, root, replies)`
  with HTML→text.

Shared HTTP seam for tests: both connectors route every request through an injectable
`self._get(url, **kw)` (mirrors the scraper's `_fetch_http` seam) so sync logic is
testable with canned JSON, no MockTransport gymnastics.

## 4. Acceptance criteria
1. Registry knows `slack` and `teams`; FORM_SPECS render both with correct
   required/secret/list flags; `qj connect slack --name org-slack -o bot_token=env:SLACK_BOT_TOKEN` works.
2. Slack sync from canned API JSON yields: threads as single documents with resolved
   @names and permalink uris; day digests for loose messages; allowlist honored;
   incremental second sync fetches only new (oldest cursor asserted).
3. Slack webhook: valid v0 signature accepted, invalid rejected (401), stale timestamp
   rejected, url_verification echoes challenge without creating documents, message event
   ingests (and appears in /api/sources doc count — the upsert_source-before-ingest rule).
4. Teams delta flow: first sync stores deltaLink; second sync calls the deltaLink URL
   (asserted via seam); HTML bodies arrive as clean text.
5. Both `tools()` lists are conditional on the right credential and absent otherwise.
6. 429/Retry-After on one channel does not abort the sync (error recorded, others complete).
7. Secrets only via resolve_secret (env indirection) — a literal-token config still works
   but the FORM_SPECS help text steers to env:.
8. CLAUDE.md commands/connector lists updated; MARKET_ASSESSMENT Appendix A rows for
   Slack/Teams annotated "(now shipped)".

## 5. Test matrix
`tests/test_slack_connector.py`: converters (thread/day/event, name resolution, emoji &
markup intact), sync pagination + allowlist + incremental via seam, 429 resilience,
conditional tool, test() ok/err. `tests/test_teams_connector.py`: token fetch (app-only,
cached), delta store/reuse, HTML stripping, converters, conditional tool.
`tests/test_api.py`: Slack-scheme webhook cases (valid/invalid/stale/challenge/message).
`tests/test_connectors.py`: registry + FORM_SPECS keys-match-options assertions for both.

---

## 6. Implementation prompt (paste into a fresh Claude Code session)

```
Implement Slack and Microsoft Teams connectors for QuickJoiner exactly per
docs/plans/03-slack-teams-connectors.md. Read that plan, CLAUDE.md, and one existing
connector end-to-end (quickjoiner/connectors/confluence.py is the best template: PULL +
PUSH + LIVE) before writing code.

PROJECT CONVENTIONS (mandatory):
- Connector contract in connectors/base.py; register with @register AND add the module
  import to registry._load_builtin_connectors (forgetting the import = connector
  invisible; there is a registry test that will catch you).
- Payload→Document converters are MODULE-LEVEL PURE FUNCTIONS (no self, no HTTP) — the
  test suites hit them directly. All HTTP goes through one injectable seam method
  `self._get(url, ...)` per connector so sync logic tests use canned JSON.
- Secrets: read via connectors/util.resolve_secret(options, "key", "ENV_DEFAULT") —
  supports token=env:VAR indirection. Never log tokens.
- FORM_SPECS in connectors/specs.py MUST list exactly the option keys the code reads
  (label/required/secret/env/list flags) — there's a stated project rule to keep these
  in sync; the web UI and /connect wizard render from it.
- Webhooks: hooks.py currently verifies generic HMAC X-QJ-Signature. Extend it so that
  when the target source's options include signing_secret, it verifies Slack's v0 scheme
  (v0:{timestamp}:{raw_body}, X-Slack-Signature, X-Slack-Request-Timestamp, 300s replay
  window, constant-time compare) and handles url_verification by echoing the challenge
  WITHOUT ingesting. Keep the existing rule: catalog.upsert_source BEFORE pipeline.ingest
  or pushed docs won't appear in /api/sources.
- normalize/graph come free: do NOT add ticket-key extraction in the connector — the
  ingest pipeline already does it for every document.
- Windows/venv: run `.venv\Scripts\python.exe -m pytest -q` from D:\Claude\QuickJoiner.
  No network in tests — if a test would touch slack.com or graph.microsoft.com, it's
  wrong; use the seam.

BUILD ORDER:
1. slack.py converters + tests (pure, fastest feedback).
2. slack.py connector class (test/sync/tools/handle_event) + seam tests.
3. hooks.py Slack scheme + tests in tests/test_api.py.
4. msteams.py token handling + converters + delta sync + tests.
5. specs.py entries for both; registry imports; registry/FORM_SPECS tests.
6. Docs: CLAUDE.md `qj connect` type list + connectors architecture bullet;
   docs/MARKET_ASSESSMENT.md Appendix A — mark Slack and Microsoft Teams "(now shipped)"
   in their rows; docs/PRD.md E2.1 connector list.

DETAILS THAT DISTINGUISH PERFECT FROM DONE:
- Slack message text contains <@U123> mentions and <http://url|label> link markup —
  resolve mentions to @DisplayName and unwrap links to "label (url)" in converters.
- Skip message subtypes {message_changed, message_deleted, channel_join, bot_message}.
- Thread doc updated_at = latest reply ts (ISO-converted); day digests sort messages
  chronologically.
- Teams HTML: use BeautifulSoup get_text("\n") + the project's normalize will handle the
  rest; strip <at> mention tags to @Name.
- Graph API paging: follow @odata.nextLink inside a sync; store ONLY @odata.deltaLink.
- Conditional tools: build tools() list dynamically; a connector with no user_token /
  delegated token returns [] for the respective tool — assert both branches.
- Rate limit: on 429 honor Retry-After once (cap 30s), then record an error string into
  the sync (connectors may raise per-doc; pipeline.ingest already accumulates errors —
  prefer yielding nothing further from that channel and continuing others).

DEFINITION OF DONE:
- Full suite green (report the number; zero regressions). No pg work needed here (no
  catalog changes) — say so explicitly rather than skipping silently.
- `qj connect slack …` and `qj connect teams …` reach the "saved" path against a fake
  seam in a CLI test OR document precisely why CLI-level coverage is deferred.
- Final report: AC→test mapping for all 8 ACs in plan §4, plus an honest "not verified
  live against real Slack/Teams tenants" note unless you were given credentials.
```
