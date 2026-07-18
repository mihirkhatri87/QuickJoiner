"""Deterministic pub/sub + datastore extraction (ingest/pubsub.py): per-language
patterns, config direction rules, placeholder honesty, infra templates, and the
end-to-end cross-repo bridge that package manifests can never produce."""

from quickjoiner.config import RetrievalConfig
from quickjoiner.connectors.base import Document
from quickjoiner.ingest.pipeline import IngestPipeline
from quickjoiner.ingest.pubsub import extract_pubsub_graph, looks_like_config

REPO = "repo:svc"


def _rels(text, uri):
    _, edges = extract_pubsub_graph(text, uri, REPO)
    return [(rel, dst) for _, rel, dst, _ in edges]


# ------------------------------------------------------------------ C# code

def test_csharp_functions_bindings():
    text = """
    public static class Handlers {
        [FunctionName("OnOrder")]
        public static void Run([ServiceBusTrigger("order-events", "billing-sub")] string msg) {}
        [FunctionName("Emit")]
        [return: ServiceBus("invoice-events")]
        public static string Send() => "x";
    }
    """
    rels = _rels(text, "src/Handlers.cs")
    assert ("subscribes_to", "topic:order-events") in rels
    assert ("publishes_to", "topic:invoice-events") in rels


def test_csharp_sdk_clients():
    text = """
    var sender = client.CreateSender("order-events");
    var processor = client.CreateProcessor("audit-log", "audit-sub");
    var receiver = client.CreateReceiver("dead-letters");
    var legacyPub = new TopicClient(connStr, "legacy-topic");
    var legacySub = new SubscriptionClient(connStr, "legacy-topic", "sub-a");
    var queueClient = new QueueClient(connStr, "ambiguous-queue");  // direction unknowable
    """
    rels = _rels(text, "src/Bus.cs")
    assert ("publishes_to", "topic:order-events") in rels
    assert ("subscribes_to", "topic:audit-log") in rels
    assert ("subscribes_to", "topic:dead-letters") in rels
    assert ("publishes_to", "topic:legacy-topic") in rels
    assert ("subscribes_to", "topic:legacy-topic") in rels
    assert not any(dst == "topic:ambiguous-queue" for _, dst in rels)  # never guessed


# ------------------------------------------------------- other languages

def test_python_azure_servicebus():
    text = """
    sender = client.get_topic_sender(topic_name="order-events")
    qs = client.get_queue_sender("jobs")
    recv = client.get_subscription_receiver("order-events", subscription_name="py-sub")
    qr = client.get_queue_receiver(queue_name='jobs')
    """
    rels = _rels(text, "svc/bus.py")
    assert ("publishes_to", "topic:order-events") in rels
    assert ("publishes_to", "topic:jobs") in rels
    assert ("subscribes_to", "topic:order-events") in rels
    assert ("subscribes_to", "topic:jobs") in rels


def test_js_azure_servicebus():
    text = """
    const sender = sbClient.createSender("order-events");
    const receiver = sbClient.createReceiver("order-events", "web-sub");
    """
    rels = _rels(text, "src/bus.ts")
    assert ("publishes_to", "topic:order-events") in rels
    assert ("subscribes_to", "topic:order-events") in rels


def test_java_builder_chains_and_jms():
    # Two chains in one file must not cross-wire: sender->topic-a, processor->topic-b.
    text = """
    ServiceBusSenderClient s = new ServiceBusClientBuilder()
        .connectionString(c).sender().topicName("topic-a").buildClient();
    ServiceBusProcessorClient p = new ServiceBusClientBuilder()
        .connectionString(c).processor().topicName("topic-b")
        .subscriptionName("sub-b").buildProcessorClient();
    @JmsListener(destination = "jms-orders")
    void onMessage(String m) {}
    void emit() { jmsTemplate.convertAndSend("jms-invoices", inv); }
    """
    rels = _rels(text, "src/Bus.java")
    assert ("publishes_to", "topic:topic-a") in rels
    assert ("subscribes_to", "topic:topic-b") in rels
    assert ("publishes_to", "topic:topic-b") not in rels  # no cross-wiring
    assert ("subscribes_to", "topic:topic-a") not in rels
    assert ("subscribes_to", "topic:jms-orders") in rels
    assert ("publishes_to", "topic:jms-invoices") in rels


def test_go_azservicebus():
    text = """
    sender, _ := client.NewSender("order-events", nil)
    r1, _ := client.NewReceiverForQueue("jobs", nil)
    r2, _ := client.NewReceiverForSubscription("order-events", "go-sub", nil)
    """
    rels = _rels(text, "internal/bus.go")
    assert ("publishes_to", "topic:order-events") in rels
    assert ("subscribes_to", "topic:jobs") in rels
    assert ("subscribes_to", "topic:order-events") in rels


# ------------------------------------------------------------- app config

def test_appsettings_with_subscription_is_consumer():
    text = '{"ServiceBus": {"TopicName": "order-events", "SubscriptionName": "billing-sub"}}'
    assert looks_like_config("src/appsettings.Production.json")
    assert ("subscribes_to", "topic:order-events") in _rels(text, "src/appsettings.Production.json")


def test_appsettings_without_subscription_is_reference_only():
    text = '{"ServiceBus": {"TopicName": "order-events"}}'
    rels = _rels(text, "appsettings.json")
    assert rels == [("references", "topic:order-events")]  # direction never guessed


def test_xml_config_and_spring_properties():
    xml = '<configuration><appSettings><add key="Bus:QueueName" value="jobs"/></appSettings></configuration>'
    assert ("references", "topic:jobs") in _rels(xml, "web.config")
    props = "azure.servicebus.topic-name=order-events\nspring.jms.subscription-name=billing\n"
    assert ("subscribes_to", "topic:order-events") in _rels(props, "src/application.properties")


def test_connection_strings_to_stores_in():
    text = (
        '{"ConnectionStrings": {'
        '"Orders": "Server=sql1;Initial Catalog=OrdersDb;Integrated Security=true",'
        '"Legacy": "Server=sql2;Database=BillingDb;User Id=x",'
        '"Sys": "Server=sql3;Database=master",'
        '"Docs": "mongodb+srv://user:pw@cluster0.mongodb.net/CustomerDocs"}}'
    )
    rels = _rels(text, "appsettings.json")
    assert ("stores_in", "datastore:ordersdb") in rels
    assert ("stores_in", "datastore:billingdb") in rels
    assert ("stores_in", "datastore:customerdocs") in rels
    assert not any(dst == "datastore:master" for _, dst in rels)  # system DBs skipped


def test_substitution_placeholders_are_skipped_never_guessed():
    """Octopus #{Var}, env %VAR%/${VAR}: the real value lives in deployment
    tooling — extraction must skip, not invent (the Octopus-variables follow-up
    resolves these from the variable set itself)."""
    text = (
        '{"TopicName": "#{Orders.TopicName}", "QueueName": "%JOBS_QUEUE%",'
        ' "Backup": "${BACKUP_TOPIC}",'
        ' "ConnectionStrings": {"Db": "Server=x;Database=#{DbName}"}}'
    )
    assert _rels(text, "appsettings.json") == []


# ---------------------------------------------------------------- infra IaC

def test_cloudformation_template_references_without_direction():
    text = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  OrderTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: order-events
  AppDb:
    Type: AWS::RDS::DBInstance
    Properties:
      DBName: OrdersDb
  Sub:
    Type: AWS::SNS::Subscription
    Properties:
      TopicArn: arn:aws:sns:us-east-1:123456789:legacy-events
"""
    rels = _rels(text, "deploy/stack.yaml")
    assert ("references", "topic:order-events") in rels
    assert ("references", "topic:legacy-events") in rels
    assert ("stores_in", "datastore:ordersdb") in rels
    assert not any(rel in ("publishes_to", "subscribes_to") for rel, _ in rels)


def test_plain_yaml_without_infra_marker_is_ignored():
    assert _rels("TopicName: not-infra\n", "notes/plan.yaml") == []


def test_unrelated_files_produce_nothing():
    assert _rels("# order-events discussion", "docs/notes.md") == []
    assert _rels('CreateSender("x")', "src/readme.txt") == []


# --------------------------------------------- end-to-end: the cross-repo bridge

def test_pipeline_bridges_publisher_and_subscriber_repos(store, catalog):
    """Two repos with NO manifest relationship — one publishes (C# code), one
    subscribes (appsettings) — become graph-connected through the topic entity,
    with each repo's own file as evidence. deps.py structurally cannot do this."""
    pipe = IngestPipeline(store, catalog, RetrievalConfig())
    pipe.ingest(
        [Document(uri="src/Publisher.cs", title="Publisher.cs", kind="code",
                  text='var s = client.CreateSender("order-events");')],
        "git:checkout",
    )
    pipe.ingest(
        [Document(uri="appsettings.json", title="appsettings.json", kind="doc",
                  text='{"TopicName": "order-events", "SubscriptionName": "billing-sub"}')],
        "git:billing",
    )
    path = catalog.graph_path("repo:checkout", "repo:billing")
    assert path is not None and [p["rel"] for p in path] == ["publishes_to", "subscribes_to"]
