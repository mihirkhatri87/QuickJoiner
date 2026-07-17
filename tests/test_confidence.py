"""Evidence classification (plan 06 §B classifier; scoring added in Phase B)."""

from __future__ import annotations

import pytest

from quickjoiner.agent.confidence import classify_evidence


@pytest.mark.parametrize("title,uri,kind,expected", [
    # dependency-map: synthesized manifest doc URIs win first
    ("a: dependencies & packages", "file:///repo::dependency-map", "doc", "dependency-map"),
    # meeting-notes: date-shaped titles (the motivating "Jan 6, 2026" page)
    ("Jan 6, 2026", "https://wiki/x/1", "doc", "meeting-notes"),
    ("2026-01-06", "https://wiki/x/2", "doc", "meeting-notes"),
    ("01/06/26", "https://wiki/x/3", "doc", "meeting-notes"),
    ("December 25th, 2025", "https://wiki/x/4", "doc", "meeting-notes"),
    # meeting-notes: vocabulary anywhere in the title
    ("Platform team meeting notes", "https://wiki/x/5", "doc", "meeting-notes"),
    ("Weekly sync — infra", "https://wiki/x/6", "doc", "meeting-notes"),
    ("Q2 Retrospective", "https://wiki/x/7", "doc", "meeting-notes"),
    ("Sprint 12 stand-up", "https://wiki/x/8", "doc", "meeting-notes"),
    # authored-doc: deliberate architecture/overview files by basename
    ("Connector/AGENTS.md", "file:///repo/AGENTS.md", "doc", "authored-doc"),
    ("readme", "https://git/x/README.md?ref=main", "doc", "authored-doc"),
    ("design", "file:///docs/design.rst", "doc", "authored-doc"),
    ("adr 12", "file:///docs/adr-012-queues.md", "doc", "authored-doc"),
    # generic: everything else — incl. formal wiki pages (no computable signal)
    ("AppRiver.Nautical", "https://wiki/x/nautical", "doc", "generic"),
    ("Release note", "w://n", "doc", "generic"),
    # date INSIDE a sentence is not a date-titled page
    ("Migration finished on 2026-01-06 successfully", "https://wiki/x/9", "doc", "generic"),
    # None-safety: LEFT-JOIN rows with missing evidence docs
    (None, None, None, "generic"),
])
def test_classify_evidence_table(title, uri, kind, expected):
    assert classify_evidence(title, uri, kind) == expected
