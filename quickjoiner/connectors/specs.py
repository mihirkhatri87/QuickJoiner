"""Connector form catalog: per-type option fields for UIs, plus supported modes.

Field keys mirror exactly what each connector reads from `options` — keep in
sync when a connector gains an option. `secret` fields support env indirection
(value `env:VAR_NAME`); `env` names the conventional variable.

A type may also carry `next_step`: what the user must do AFTER saving, for connectors that
aren't usable the moment they're created (OneDrive needs an interactive Microsoft 365 sign-in
that can't happen before the connector exists). The whole spec dict is spread into
`connector_catalog()`, so the web form picks a new key like this up with no API change.

Each type also carries `suggests`: seed questions the connector contributes to
question autocomplete (`quickjoiner/suggest.py`). They surface as soon as a source
of that type is configured — so **adding a connector here automatically teaches the
autocomplete what to offer**, alongside the live entity-based suggestions the ingest
pipeline mines from that source's data. Keep them short and in the user's voice.
"""

from __future__ import annotations

from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.registry import CONNECTOR_TYPES, _load_builtin_connectors


def _f(key: str, label: str, *, required: bool = False, secret: bool = False,
       env: str | None = None, placeholder: str = "", help: str = "",
       list_: bool = False, lock_after_sync: bool = False) -> dict:
    # lock_after_sync: editable only while the connector has 0 learned documents (same
    # rule as the connector name) — for identity-shaping fields whose edits could not be
    # applied consistently to already-ingested data. Enforced server-side in the PATCH
    # endpoint and rendered disabled (with the reason) in the edit form.
    return {"key": key, "label": label, "required": required, "secret": secret,
            "env": env, "placeholder": placeholder, "help": help, "list": list_,
            "lock_after_sync": lock_after_sync}


FORM_SPECS: dict[str, dict] = {
    "files": {
        "label": "Files & folders",
        "blurb": "Index local documents, wikis exported to disk, or a folder of notes.",
        "suggests": ["Give me a quick onboarding overview.", "What are the main systems in this org?"],
        "fields": [
            _f("path", "Folder or file path", required=True, placeholder="D:\\docs\\team-wiki"),
            _f("aka", "Also known as", list_=True, lock_after_sync=True,
               placeholder="provisioning, stevedore-svc",
               help="Other names this system goes by (comma-separated) — powers alias search "
                    "and links it to same-named services/pipelines in the knowledge graph. "
                    "Editable only until the first sync."),
        ],
    },
    "git": {
        "label": "Git repository",
        "blurb": "Clone any git remote and learn its files and commit history.",
        "suggests": ["What are the main repositories?", "What does the codebase do?",
                     "How do I build and run this project?"],
        "fields": [
            _f("url", "Clone URL (https)", required=True,
               placeholder="https://gitlab.example.com/group/repo.git"),
            _f("token", "Access token", secret=True, env="GIT_TOKEN",
               help="GitLab tokens need the oauth2: prefix, e.g. oauth2:glpat-…"),
            _f("branch", "Branch", placeholder="main"),
            _f("aka", "Also known as", list_=True, lock_after_sync=True,
               placeholder="appriver.provisioning, stevedore",
               help="Other names this repo goes by (comma-separated) — powers alias search "
                    "and links it to same-named services/pipelines in the knowledge graph. "
                    "Editable only until the first sync."),
        ],
    },
    "github": {
        "label": "GitHub",
        "blurb": "Pull requests, issues, and CI runs. Live code search at question time.",
        "suggests": ["What are the main repositories?", "What changed in recent pull requests?"],
        "fields": [
            _f("repo", "Repository (owner/name)", required=True, placeholder="acme/payments"),
            _f("token", "Access token", secret=True, env="GITHUB_TOKEN"),
            _f("base_url", "API base URL", placeholder="https://api.github.com",
               help="GitHub Enterprise Server: https://github.company.com/api/v3"),
        ],
    },
    "gitlab": {
        "label": "GitLab",
        "blurb": "Merge requests, issues, pipelines, and wikis. Live code search too.",
        "suggests": ["What are the main repositories?", "What changed in recent merge requests?"],
        "fields": [
            _f("project", "Project (group/name)", placeholder="platform/payments",
               help="A single repo to connect. For a whole group instead, use 'Group' below."),
            _f("group", "Group / namespace", placeholder="zix",
               help="Group-scoped connector: its live tools reach ANY repo in the group by name and "
                    "can list group-wide (all open MRs). One connector instead of one per repo."),
            _f("projects", "Repos to ingest", list_=True, placeholder="grp/repo-a, grp/repo-b",
               help="For a group connector: which repos' MRs/issues/graph to actually ingest into "
                    "memory (tools still reach the whole group). Leave empty for tools-only."),
            _f("token", "Personal access token", secret=True, env="GITLAB_TOKEN"),
            _f("base_url", "Instance URL", placeholder="https://gitlab.com",
               help="Self-managed instances: https://gitlab.yourcompany.net"),
            _f("ticket_in_branch", "Ticket id in branch/MR name", placeholder="false",
               help="Org rule (opt-in): your branches/MRs start with the TFS work-item number "
                    "(e.g. 320753-fix). Links each MR/branch directly to ticket #320753."),
            _f("ticket_pattern", "…custom ticket regex", placeholder="(\\d{4,})",
               help="Override the ticket-id pattern (regex, group 1 = the id). Defaults to the "
                    "first run of 4+ digits when 'Ticket id in branch/MR name' is on."),
            _f("tfs_sync_stage", "TFS-sync job/stage name", placeholder="tfs",
               help="Org rule (opt-in): if your pipelines mirror branches to TFS, the job or stage "
                    "name (substring) of that step. Enables per-branch 'last synced to TFS' capture."),
            _f("tfs_sync_lookback_days", "TFS-sync look-back (days)", placeholder="15",
               help="How far back to search for the most recent successful TFS-sync pipeline."),
        ],
    },
    "jira": {
        "label": "Jira",
        "blurb": "Tickets and their history — what was built, why, and by whom.",
        "suggests": ["What are the active projects?", "What tickets are in progress?"],
        "fields": [
            _f("base_url", "Site URL", required=True, placeholder="https://acme.atlassian.net"),
            _f("email", "Account email", placeholder="you@acme.com"),
            _f("api_token", "API token", secret=True, env="JIRA_API_TOKEN"),
            _f("projects", "Project keys", list_=True, placeholder="PAY, CORE"),
            _f("jql", "Custom JQL filter", placeholder="updated >= -30d"),
        ],
    },
    "confluence": {
        "label": "Confluence",
        "blurb": "Wiki spaces and pages. Live CQL search at question time.",
        "suggests": ["How do I set up my dev environment?", "Where is the onboarding documentation?"],
        "fields": [
            _f("base_url", "Site URL", required=True,
               placeholder="https://acme.atlassian.net/wiki"),
            _f("email", "Account email", placeholder="you@acme.com"),
            _f("api_token", "API token", secret=True, env="CONFLUENCE_API_TOKEN"),
            _f("spaces", "Space keys", list_=True, placeholder="ENG, ARCH"),
        ],
    },
    "onedrive": {
        "label": "OneDrive / SharePoint",
        "blurb": "Learn individual OneDrive, SharePoint or Teams documents on demand, using "
                 "your own Microsoft 365 sign-in. Nothing is crawled: it ingests exactly the "
                 "files you point it at.",
        # Shown in the create form and after saving. Some connectors aren't usable the moment
        # they're saved — this one needs an interactive sign-in that can't happen earlier,
        # because the token is keyed to the connector and the connector doesn't exist yet.
        # Saying so on the form beats leaving someone hunting for a button that isn't there.
        "next_step": "Save this first, then press **Sign in with Microsoft** on the connector "
                     "to authorise it with your own Microsoft 365 account. After that, paste "
                     "document links to learn them — nothing is crawled.",
        "suggests": ["What does the design document say?",
                     "Summarise the documents I've learned from OneDrive."],
        "fields": [
            _f("client_id", "Application (client) ID", required=True,
               placeholder="00000000-0000-0000-0000-000000000000",
               help="From your organisation's Azure app registration (a public client with "
                    "delegated Microsoft Graph permissions). Not a secret."),
            _f("tenant", "Directory (tenant) ID or domain", placeholder="organizations",
               help="Your tenant GUID or domain for a single-tenant app registration. "
                    "Leave blank to accept any work or school account."),
            _f("access", "May reach", list_=True, placeholder="my_drive, shared_with_me",
               help="Which delegated permissions to request: my_drive (Files.Read), "
                    "shared_with_me (Files.Read.All), sharepoint (Sites.Read.All — usually "
                    "needs admin consent). Ask only for what you need."),
        ],
    },
    "azure_devops": {
        "label": "Azure DevOps",
        "blurb": "Work items by team over recent sprints, build pipelines, live code search + WIQL. "
                 "Cloud or on-prem Server/TFS.",
        "suggests": ["What work items are in the current sprint?", "What pipeline builds this repo?",
                     "What is team X working on?"],
        "fields": [
            _f("organization", "Organization (cloud)", placeholder="acme",
               help="For dev.azure.com SaaS. Leave blank and use Server URL + Collection for on-prem TFS / Azure DevOps Server."),
            _f("server_url", "Server URL (on-prem)", placeholder="https://tfs.company.com/tfs",
               help="On-prem Azure DevOps Server / TFS host up to and including /tfs. Use with Collection."),
            _f("collection", "Collection (on-prem)", placeholder="DefaultCollection",
               help="On-prem collection name, e.g. the segment after /tfs/ in the server URL."),
            _f("project", "Project", required=True, placeholder="Payments"),
            _f("token", "Personal access token", secret=True, env="AZURE_DEVOPS_PAT"),
            _f("api_version", "API version", placeholder="7.0",
               help="Match your server: 2022→7.0, 2020→6.0, 2019→5.0. Default 7.0."),
            _f("verify_tls", "Verify TLS certificate", placeholder="true",
               help="Set to false for on-prem servers with a self-signed / internal-CA certificate."),
            _f("teams", "Teams", list_=True, placeholder="Team Rocket, Payments Team",
               help="Ingest work items from these teams' recent sprints. Empty = every team in the "
                    "project (teams without sprints are skipped). Narrow this on large projects with "
                    "hundreds of teams to keep syncs fast."),
            _f("sprints", "Recent sprints per team", placeholder="10",
               help="How many of each team's most recent sprints to ingest work items from (default 10). "
                    "Anything outside this slice is answered live via the WIQL tool."),
            _f("stale_after_days", "Report inactive teams after (days)", placeholder="365",
               help="When a team's recent-sprint window has zero work items AND its most recent "
                    "checked sprint ended more than this many days ago, the sync log reports it as "
                    "inactive rather than staying silent (default 365). Doesn't change what's "
                    "ingested — an empty window was already skipped — only whether it's explained."),
        ],
    },
    "octopus": {
        "label": "Octopus Deploy",
        "blurb": "Projects, releases, and what is deployed where right now.",
        "suggests": ["What environments are configured in Octopus?",
                     "What is deployed to Production?", "What is deployed to Staging?"],
        "fields": [
            _f("server_url", "Server URL", required=True, placeholder="https://octopus.acme.com"),
            _f("api_key", "API key", secret=True, env="OCTOPUS_API_KEY"),
            _f("space_id", "Space", placeholder="Spaces-1"),
            _f("incremental", "Incremental releases", placeholder="false",
               help="Set to true to re-fetch a project's releases only when Octopus recorded an event for it since the last sync (project list + deployment dashboard always refresh). Faster on large spaces; leave false for a full refresh every sync."),
        ],
    },
    "grafana": {
        "label": "Grafana / Loki",
        "blurb": "Dashboard inventory now; logs queried live when you ask.",
        "suggests": ["What dashboards monitor production?", "Are there any errors in the logs?"],
        "fields": [
            _f("base_url", "Grafana URL", required=True, placeholder="https://grafana.acme.com"),
            _f("token", "Service account token", secret=True, env="GRAFANA_TOKEN"),
            _f("loki_datasource_uid", "Loki datasource UID", placeholder="P8E80F9AEF21F6940"),
        ],
    },
    "datadog": {
        "label": "Datadog",
        "blurb": "Monitors and dashboards inventory; logs searched live.",
        "suggests": ["What monitors are alerting?", "Are there any errors in the logs?"],
        "fields": [
            _f("site", "Site", placeholder="datadoghq.com"),
            _f("api_key", "API key", secret=True, env="DD_API_KEY"),
            _f("app_key", "Application key", secret=True, env="DD_APP_KEY"),
        ],
    },
    "dynatrace": {
        "label": "Dynatrace",
        "blurb": "Problems and entities inventory; logs queried live.",
        "suggests": ["What problems are open in Dynatrace?", "Are there any errors in the logs?"],
        "fields": [
            _f("base_url", "Environment URL", required=True,
               placeholder="https://abc123.live.dynatrace.com"),
            _f("token", "API token", secret=True, env="DYNATRACE_TOKEN"),
        ],
    },
    "elastic": {
        "label": "Elasticsearch",
        "blurb": "Index inventory now; log search runs live at question time.",
        "suggests": ["What indices are available?", "Are there any errors in the logs?"],
        "fields": [
            _f("base_url", "Cluster URL", required=True, placeholder="https://es.acme.com:9200"),
            _f("api_key", "API key", secret=True, env="ELASTIC_API_KEY"),
            _f("username", "Username (basic auth)", placeholder="elastic"),
            _f("password", "Password (basic auth)", secret=True, env="ELASTIC_PASSWORD"),
        ],
    },
    "web_scrape": {
        "label": "Web pages",
        "blurb": "Crawl internal docs or portals. Polite by default; falls back to a "
                 "real browser when a page refuses a plain fetch. Last resort when there's no API.",
        "suggests": ["What do the internal docs cover?"],
        "fields": [
            _f("start_urls", "Start URLs", required=True, list_=True,
               placeholder="https://docs.acme.internal/"),
            _f("allow_prefixes", "Only follow links under", list_=True,
               placeholder="https://docs.acme.internal/"),
            _f("max_pages", "Page limit", placeholder="50"),
            _f("max_depth", "Link depth limit", placeholder="4",
               help="How many link hops to follow from a start URL (start page = 0). "
                    "Empty = unlimited; the page limit still applies."),
            _f("use_browser", "Always use signed-in browser session", placeholder="true",
               help="Needs qj browser login first; renders JS and reuses your SSO session."),
            _f("fallback_to_browser", "Fall back to a browser if blocked", placeholder="true",
               help="On by default: if a plain fetch is refused (e.g. a 444/403), retry the "
                    "crawl with a real headless browser."),
            _f("respect_robots", "Respect robots.txt", placeholder="true",
               help="On by default. Turn off only for sites you own."),
            _f("rate_limit_seconds", "Delay between requests (s)", placeholder="1.0"),
            _f("user_agent", "Custom User-Agent", help="Overrides the default desktop-Chrome UA."),
        ],
    },
}

# "Also known as" applies to EVERY connected system, not just repos: declare the other
# names it goes by (comma-separated). It always powers alias search / query expansion /
# autocomplete for that source, and drives the `same_as` identity bridges wherever the
# source maps to a bridgeable entity (code repos, services, pipelines, projects). Same
# editability rule as the connector name (lock_after_sync). Appended here to every type so
# it's universal and consistent — git/files above already carry a tailored copy, which the
# guard preserves.
for _spec in FORM_SPECS.values():
    if not any(f["key"] == "aka" for f in _spec["fields"]):
        _spec["fields"].append(_f(
            "aka", "Also known as", list_=True, lock_after_sync=True,
            placeholder="other-name, legacy-name",
            help="Other names this system goes by (comma-separated) — powers alias search "
                 "and links it to same-named entities in the knowledge graph. Editable only "
                 "until the first sync."))

_MODE_LETTERS = [
    (Mode.PULL, "pull"), (Mode.PUSH, "hooks"), (Mode.LIVE, "live"),
    (Mode.BROWSER, "browser"), (Mode.SCRAPE, "scrape"),
]


def connector_catalog() -> list[dict]:
    """Types with form fields and supported modes, for building config UIs."""
    _load_builtin_connectors()
    catalog = []
    for type_name, spec in FORM_SPECS.items():
        cls = CONNECTOR_TYPES.get(type_name)
        modes = [label for flag, label in _MODE_LETTERS if cls and flag in cls.modes]
        catalog.append({"type": type_name, "modes": modes, **spec})
    return catalog
