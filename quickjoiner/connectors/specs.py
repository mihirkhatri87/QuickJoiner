"""Connector form catalog: per-type option fields for UIs, plus supported modes.

Field keys mirror exactly what each connector reads from `options` — keep in
sync when a connector gains an option. `secret` fields support env indirection
(value `env:VAR_NAME`); `env` names the conventional variable.
"""

from __future__ import annotations

from quickjoiner.connectors.base import Mode
from quickjoiner.connectors.registry import CONNECTOR_TYPES, _load_builtin_connectors


def _f(key: str, label: str, *, required: bool = False, secret: bool = False,
       env: str | None = None, placeholder: str = "", help: str = "",
       list_: bool = False) -> dict:
    return {"key": key, "label": label, "required": required, "secret": secret,
            "env": env, "placeholder": placeholder, "help": help, "list": list_}


FORM_SPECS: dict[str, dict] = {
    "files": {
        "label": "Files & folders",
        "blurb": "Index local documents, wikis exported to disk, or a folder of notes.",
        "fields": [
            _f("path", "Folder or file path", required=True, placeholder="D:\\docs\\team-wiki"),
        ],
    },
    "git": {
        "label": "Git repository",
        "blurb": "Clone any git remote and learn its files and commit history.",
        "fields": [
            _f("url", "Clone URL (https)", required=True,
               placeholder="https://gitlab.example.com/group/repo.git"),
            _f("token", "Access token", secret=True, env="GIT_TOKEN",
               help="GitLab tokens need the oauth2: prefix, e.g. oauth2:glpat-…"),
            _f("branch", "Branch", placeholder="main"),
        ],
    },
    "github": {
        "label": "GitHub",
        "blurb": "Pull requests, issues, and CI runs. Live code search at question time.",
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
        "fields": [
            _f("project", "Project (group/name)", required=True, placeholder="platform/payments"),
            _f("token", "Personal access token", secret=True, env="GITLAB_TOKEN"),
            _f("base_url", "Instance URL", placeholder="https://gitlab.com",
               help="Self-managed instances: https://gitlab.yourcompany.net"),
        ],
    },
    "jira": {
        "label": "Jira",
        "blurb": "Tickets and their history — what was built, why, and by whom.",
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
        "fields": [
            _f("base_url", "Site URL", required=True,
               placeholder="https://acme.atlassian.net/wiki"),
            _f("email", "Account email", placeholder="you@acme.com"),
            _f("api_token", "API token", secret=True, env="CONFLUENCE_API_TOKEN"),
            _f("spaces", "Space keys", list_=True, placeholder="ENG, ARCH"),
        ],
    },
    "azure_devops": {
        "label": "Azure DevOps",
        "blurb": "Work items, PRs, pipelines. Live code search. Cloud or on-prem Server/TFS.",
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
        ],
    },
    "octopus": {
        "label": "Octopus Deploy",
        "blurb": "Projects, releases, and what is deployed where right now.",
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
        "fields": [
            _f("base_url", "Grafana URL", required=True, placeholder="https://grafana.acme.com"),
            _f("token", "Service account token", secret=True, env="GRAFANA_TOKEN"),
            _f("loki_datasource_uid", "Loki datasource UID", placeholder="P8E80F9AEF21F6940"),
        ],
    },
    "datadog": {
        "label": "Datadog",
        "blurb": "Monitors and dashboards inventory; logs searched live.",
        "fields": [
            _f("site", "Site", placeholder="datadoghq.com"),
            _f("api_key", "API key", secret=True, env="DD_API_KEY"),
            _f("app_key", "Application key", secret=True, env="DD_APP_KEY"),
        ],
    },
    "dynatrace": {
        "label": "Dynatrace",
        "blurb": "Problems and entities inventory; logs queried live.",
        "fields": [
            _f("base_url", "Environment URL", required=True,
               placeholder="https://abc123.live.dynatrace.com"),
            _f("token", "API token", secret=True, env="DYNATRACE_TOKEN"),
        ],
    },
    "elastic": {
        "label": "Elasticsearch",
        "blurb": "Index inventory now; log search runs live at question time.",
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
