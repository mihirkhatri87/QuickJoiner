"""Code-structural graph extraction: per-language defines/imports parsing, module
canonicalization, and end-to-end persistence of repo->defines->symbol /
repo->imports->module edges through the ingest pipeline."""

from __future__ import annotations

from quickjoiner.connectors.base import Document
from quickjoiner.ingest.code_graph import extract_code_graph, looks_like_code
from quickjoiner.ingest.pipeline import IngestPipeline


def _extract(text, uri):
    return extract_code_graph(text, uri, "repo:platform")


def _names(entities, type_):
    return {name for _id, name, t in entities if t == type_}


# ---------------------------------------------------------------- detection

def test_looks_like_code():
    assert looks_like_code("code", "x")
    assert looks_like_code("doc", "src/Api/Payment.cs")
    assert looks_like_code("", "main.go")
    assert not looks_like_code("doc", "handbook/deploy.md")
    assert not looks_like_code("doc", "notes")


def test_unrecognized_file_yields_nothing():
    assert extract_code_graph("whatever", "notes.md", "repo:x") == ([], [])


# ---------------------------------------------------------------- python

def test_python_defines_and_imports():
    text = (
        "import os\n"
        "import stripe.client\n"
        "from collections import OrderedDict\n"
        "from . import local_helper\n"          # relative -> skipped
        "from .models import Thing\n"            # relative -> skipped
        "class PaymentProcessor:\n"
        "    def __init__(self):\n"              # dunder -> skipped
        "        pass\n"
        "    async def settle(self):\n"
        "        pass\n"
        "def top_level():\n"
        "    pass\n"
    )
    ents, edges = _extract(text, "src/pay.py")
    assert _names(ents, "symbol") == {"PaymentProcessor", "settle", "top_level"}
    assert _names(ents, "module") == {"os", "stripe", "collections"}
    assert ("repo:platform", "defines", "symbol:paymentprocessor", "in src/pay.py") in edges
    assert ("repo:platform", "imports", "module:stripe", "from src/pay.py") in edges


# ---------------------------------------------------------------- js / ts

def test_jsts_defines_and_imports():
    text = (
        "import React from 'react';\n"
        "import { Client } from '@stripe/stripe-js';\n"
        "import helper from './local';\n"       # relative -> skipped
        "const x = require('lodash/fp');\n"
        "export function handlePayment() {}\n"
        "class Widget {}\n"
    )
    ents, edges = _extract(text, "web/app.ts")
    assert _names(ents, "symbol") == {"handlePayment", "Widget"}
    # scoped kept whole; sub-path collapsed to package; relative dropped
    assert _names(ents, "module") == {"react", "@stripe/stripe-js", "lodash"}


# ---------------------------------------------------------------- jvm

def test_java_canonicalizes_import_to_package():
    text = (
        "import com.acme.payments.Processor;\n"
        "import com.acme.util.*;\n"
        "public class Processor {}\n"
        "interface Ledger {}\n"
    )
    ents, _edges = _extract(text, "src/Processor.java")
    assert _names(ents, "symbol") == {"Processor", "Ledger"}
    assert _names(ents, "module") == {"com.acme.payments", "com.acme.util"}


# ---------------------------------------------------------------- c#

def test_csharp_defines_and_usings():
    text = (
        "using System.Text;\n"
        "using AppRiver.Nautical.Models;\n"
        "namespace Foo { public sealed class Boat { } public interface IHull { } }\n"
    )
    ents, _edges = _extract(text, "src/Boat.cs")
    assert _names(ents, "symbol") == {"Boat", "IHull"}
    assert _names(ents, "module") == {"System.Text", "AppRiver.Nautical.Models"}


# ---------------------------------------------------------------- go

def test_go_defines_and_imports():
    text = (
        'import (\n    "fmt"\n    "github.com/lib/pq"\n)\n'
        "func Settle() {}\n"
        "type Ledger struct {}\n"
    )
    ents, _edges = _extract(text, "pay/ledger.go")
    assert _names(ents, "symbol") == {"Settle", "Ledger"}
    assert _names(ents, "module") == {"fmt", "github.com/lib/pq"}


# ---------------------------------------------------------------- end to end

def test_ingesting_code_populates_graph(store, catalog):
    IngestPipeline(store, catalog).ingest(
        [Document(uri="src/Payment.py", title="Payment",
                  text="import stripe\nclass PaymentProcessor:\n    def settle(self):\n        pass\n",
                  kind="code")],
        "git:platform",
    )
    # symbol + module entities resolve, and the repo's edges include them with the
    # code file as evidence.
    assert catalog.resolve_entity("PaymentProcessor")["type"] == "symbol"
    assert catalog.resolve_entity("stripe")["type"] == "module"
    rels = {(r["rel"], r["dst"]) for r in catalog.graph_neighbors("repo:platform")}
    assert ("defines", "symbol:paymentprocessor") in rels
    assert ("imports", "module:stripe") in rels
