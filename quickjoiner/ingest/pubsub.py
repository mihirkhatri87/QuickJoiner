"""Deterministic pub/sub + datastore extraction for the knowledge graph.

The runtime-coupling twin of code_graph.py: package manifests (deps.py) see
compile-time dependencies, but two services wired only through a Service Bus
topic — or a shared database — have no manifest relationship at all. This module
parses the places that coupling IS written down, per ecosystem:

    code (.cs/.py/.js/.ts/.java/.kt/.go — each SDK names channels differently):
      C#     [ServiceBusTrigger(...)], [ServiceBus(...)], CreateSender/Processor/
             Receiver, legacy TopicClient/SubscriptionClient
      Python azure-servicebus get_topic_sender / get_queue_sender /
             get_subscription_receiver / get_queue_receiver
      JS/TS  @azure/service-bus createSender / createReceiver
      Java   ServiceBusClientBuilder chains (sender()/processor() + topicName()/
             queueName(), matched within one statement), @JmsListener,
             jmsTemplate.convertAndSend
      Go     azservicebus NewSender / NewReceiverForQueue /
             NewReceiverForSubscription
    app config (appsettings*.json, *.config XML, application*.properties/yml):
      Topic/Queue keys -> subscribes_to when a Subscription* key appears in the
      same file (only consumers name a subscription), else --references-->
      (channel known, direction not claimed); connection strings
      (Database=/Initial Catalog=, mongodb://.../db, postgres://, mysql://)
      -> --stores_in--> datastore
    infra-as-code (CloudFormation/SAM/serverless templates, content-detected):
      AWS::SNS::Topic / AWS::SQS::Queue TopicName/QueueName, RDS DBName, and
      arn:aws:sns/sqs:...:name references -> --references--> / --stores_in-->
      (a template proves the channel exists and this repo touches it; it does
      not prove publish vs subscribe, so we don't claim it)

Honesty rules, in priority order:
- Only literals. A variable-held name is not deterministic. Substitution
  placeholders — Octopus `#{Var}`, env `%VAR%`/`${VAR}`, CFN `!Ref`/`Fn::` — are
  SKIPPED, never guessed: in orgs where deployment tooling holds the real values,
  the truth lives in the Octopus variable set / template parameters, and the
  right fix is ingesting THOSE artifacts (an Octopus-variables extraction in the
  octopus connector is the tracked follow-up — the variable document then asserts
  the real value with its own evidence), not resolving indirection by hope.
- Direction is asserted only when the API/key itself implies it; everything else
  is `references`. A wrong edge is worse than a missing one.
- New ecosystem == new pattern rows, not a new mechanism (MassTransit,
  NServiceBus, Kafka clients, Kubernetes/compose env are natural next rows).

Pure and testable, same shape as code_graph.extract_code_graph:
(text, uri, src_id) -> (entities, edges); the caller adds the source entity and
supplies the evidence document id.
"""

from __future__ import annotations

import re

# Cap per file so a generated config blob can't explode the graph.
_MAX = 40

# A legal channel/database name: no whitespace and no substitution syntax —
# the charset excludes `#{}`, `%`, `${}`, `!`, quotes, spaces, so Octopus/env/CFN
# placeholders are rejected wholesale rather than pattern-by-pattern.
_NAME_OK = re.compile(r"^[A-Za-z0-9._/-]+$")

# System databases that appear in connection strings but aren't org datastores.
_SYSTEM_DBS = {"master", "tempdb", "model", "msdb"}

_DQ = r'"([^"]+)"'          # double-quoted literal (C#, Java, Go, JSON)
_AQ = r"[\"'`]([^\"'`]+)[\"'`]"  # any-quoted literal (Python, JS/TS)
_STMT = r"[^;]{0,400}?"     # within one statement (Java builder chains)

# ---- per-language code patterns: ext -> [(regex, relation)] -----------------
_LANG_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    ".cs": [
        # Azure Functions trigger: 1 string arg = queue, 2 = topic+subscription.
        (re.compile(r"\[\s*ServiceBusTrigger\s*\(\s*" + _DQ), "subscribes_to"),
        # Azure Functions output binding (not the trigger): plain, [return: ...]
        # attribute-target, and the isolated-worker ServiceBusOutput form.
        (re.compile(r"\[\s*(?:return\s*:\s*)?ServiceBus(?:Output)?\s*\(\s*" + _DQ), "publishes_to"),
        # Modern SDK (Azure.Messaging.ServiceBus).
        (re.compile(r"\bCreateSender\s*\(\s*" + _DQ), "publishes_to"),
        (re.compile(r"\bCreateProcessor\s*\(\s*" + _DQ), "subscribes_to"),
        (re.compile(r"\bCreateReceiver\s*\(\s*" + _DQ), "subscribes_to"),
        # Legacy SDK: channel is the 2nd ctor arg. QueueClient (bidirectional)
        # is deliberately ignored — direction would be a guess.
        (re.compile(r"\bnew\s+TopicClient\s*\([^)]*?,\s*" + _DQ), "publishes_to"),
        (re.compile(r"\bnew\s+SubscriptionClient\s*\([^)]*?,\s*" + _DQ), "subscribes_to"),
    ],
    ".py": [
        (re.compile(r"\bget_topic_sender\s*\(\s*(?:topic_name\s*=\s*)?" + _AQ), "publishes_to"),
        (re.compile(r"\bget_queue_sender\s*\(\s*(?:queue_name\s*=\s*)?" + _AQ), "publishes_to"),
        (re.compile(r"\bget_subscription_receiver\s*\(\s*(?:topic_name\s*=\s*)?" + _AQ), "subscribes_to"),
        (re.compile(r"\bget_queue_receiver\s*\(\s*(?:queue_name\s*=\s*)?" + _AQ), "subscribes_to"),
    ],
    ".java": [
        # Builder chains are one statement; match sender()/processor() and the
        # channel name within it, in either order.
        (re.compile(r"\.sender\s*\(\s*\)" + _STMT + r"\.(?:topicName|queueName)\s*\(\s*" + _DQ), "publishes_to"),
        (re.compile(r"\.(?:topicName|queueName)\s*\(\s*" + _DQ + _STMT + r"\.sender\s*\(\s*\)"), "publishes_to"),
        (re.compile(r"\.(?:processor|receiver)\s*\(\s*\)" + _STMT + r"\.(?:topicName|queueName)\s*\(\s*" + _DQ), "subscribes_to"),
        (re.compile(r"\.(?:topicName|queueName)\s*\(\s*" + _DQ + _STMT + r"\.(?:processor|receiver)\s*\(\s*\)"), "subscribes_to"),
        # Spring JMS.
        (re.compile(r"@JmsListener\s*\([^)]*?destination\s*=\s*" + _DQ), "subscribes_to"),
        (re.compile(r"\.convertAndSend\s*\(\s*" + _DQ), "publishes_to"),
    ],
    ".go": [
        (re.compile(r"\bNewSender\s*\(\s*" + _DQ), "publishes_to"),
        (re.compile(r"\bNewReceiverForQueue\s*\(\s*" + _DQ), "subscribes_to"),
        (re.compile(r"\bNewReceiverForSubscription\s*\(\s*" + _DQ), "subscribes_to"),
    ],
}
for _js_ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
    _LANG_PATTERNS[_js_ext] = [
        (re.compile(r"\bcreateSender\s*\(\s*" + _AQ), "publishes_to"),
        (re.compile(r"\bcreateReceiver\s*\(\s*" + _AQ), "subscribes_to"),
    ]
_LANG_PATTERNS[".kt"] = _LANG_PATTERNS[".java"]  # Kotlin uses the same SDKs

# ---- app-config patterns ----------------------------------------------------
# JSON:  "TopicName": "chan"   /   "QueueName": "chan"
_JSON_CHANNEL = re.compile(r'"(?:[A-Za-z]*(?:Topic|Queue)(?:Name)?)"\s*:\s*' + _DQ)
# XML:   <add key="ServiceBus:TopicName" value="chan"/>
_XML_CHANNEL = re.compile(r'key\s*=\s*"[^"]*(?:Topic|Queue)(?:Name)?"\s+value\s*=\s*' + _DQ, re.I)
# Spring: azure.servicebus.topic-name=chan  /  topicName: chan (properties or yml)
_SPRING_CHANNEL = re.compile(
    r"(?im)^[ \t]*[A-Za-z0-9.\-]*(?:topic|queue)[-.]?name\s*[:=]\s*['\"]?([A-Za-z0-9._/-]+)")
# A subscription name anywhere in the file marks it a CONSUMER's config.
_SUBSCRIPTION_KEY = re.compile(
    r'(?i)(?:"[A-Za-z]*Subscription(?:Name)?"\s*:|key\s*=\s*"[^"]*Subscription|subscription[-.]?name\s*[:=])')
# Connection strings: SQL Server style + URI style.
_SQL_DB = re.compile(r"(?:Database|Initial\s+Catalog)\s*=\s*([A-Za-z0-9._-]+)", re.I)
_URI_DB = re.compile(r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql)://[^/\s\"']+/([A-Za-z0-9._-]+)")

# ---- infra-as-code patterns (direction never claimed: references/stores_in) --
# Content-detected CloudFormation/SAM/serverless: don't trust filenames alone.
_INFRA_MARKER = re.compile(
    r"AWSTemplateFormatVersion|Type:\s*['\"]?AWS::|service:\s*\S+[\s\S]{0,200}provider:\s*\n\s+name:\s*aws")
_INFRA_CHANNEL = re.compile(r"(?im)^[ \t]*(?:TopicName|QueueName)\s*:\s*['\"]?([A-Za-z0-9._/-]+)")
_INFRA_DB = re.compile(r"(?im)^[ \t]*(?:DBName|DatabaseName)\s*:\s*['\"]?([A-Za-z0-9._/-]+)")
_ARN_CHANNEL = re.compile(r"arn:aws:(?:sns|sqs):[a-z0-9-]*:[0-9*]*:([A-Za-z0-9._-]+)")

_CONFIG_BASENAMES = re.compile(
    r"(?:^|[/\\])(?:appsettings[^/\\]*\.json|local\.settings\.json|[^/\\]*\.config"
    r"|application[^/\\]*\.(?:properties|ya?ml))$", re.I)


def _path(uri: str) -> str:
    return uri.split("?")[0].split("#")[0]


def _ext(uri: str) -> str:
    path = _path(uri).rstrip("/")
    dot, slash = path.rfind("."), max(path.rfind("/"), path.rfind("\\"))
    return path[dot:].lower() if dot > slash else ""


def looks_like_config(uri: str) -> bool:
    """appsettings*.json / local.settings.json / *.config / application*.properties|yml."""
    return bool(_CONFIG_BASENAMES.search(_path(uri)))


def _ok(name: str) -> bool:
    return bool(name) and bool(_NAME_OK.match(name))


def extract_pubsub_graph(text: str, uri: str, src_id: str) -> tuple[list[tuple], list[tuple]]:
    """(entities, edges) linking `src_id` to the message channels it publishes/
    subscribes to (or provably touches) and the datastores it stores in. Empty
    for files that are none of: recognized code, app config, infra template.
    Caller adds the source entity and supplies the evidence document id."""
    path = _path(uri)
    lang = _LANG_PATTERNS.get(_ext(uri))
    is_config = looks_like_config(uri)
    is_yamlish = _ext(uri) in {".yaml", ".yml", ".json", ".template"}

    try:
        found: list[tuple[str, str, str]] = []  # (rel, type, name)
        if lang:
            for pattern, rel in lang:
                for name in pattern.findall(text):
                    found.append((rel, "topic", name))
        if is_config:
            # Direction from config: only consumers configure a subscription name.
            channel_rel = "subscribes_to" if _SUBSCRIPTION_KEY.search(text) else "references"
            for name in (_JSON_CHANNEL.findall(text) + _XML_CHANNEL.findall(text)
                         + _SPRING_CHANNEL.findall(text)):
                found.append((channel_rel, "topic", name))
            for name in _SQL_DB.findall(text) + _URI_DB.findall(text):
                if name.lower() not in _SYSTEM_DBS:
                    found.append(("stores_in", "datastore", name))
        if is_yamlish and not is_config and _INFRA_MARKER.search(text):
            # A template proves the channel/db exists and this repo touches it —
            # not who publishes vs subscribes, so direction is never claimed.
            for name in _INFRA_CHANNEL.findall(text) + _ARN_CHANNEL.findall(text):
                found.append(("references", "topic", name))
            for name in _INFRA_DB.findall(text):
                found.append(("stores_in", "datastore", name))
    except Exception:  # a pathological file must never break ingestion
        return [], []
    if not found:
        return [], []

    entities: list[tuple] = []
    edges: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    short = path[-80:]
    for rel, type_, name in found:
        if not _ok(name):
            continue
        eid = f"{type_}:{name.lower()}"
        if (rel, eid) in seen:
            continue
        seen.add((rel, eid))
        entities.append((eid, name, type_))
        edges.append((src_id, rel, eid, f"in {short}"))
        if len(edges) >= _MAX:
            break
    return entities, edges
