"""Cross-source identity bridges (AI roadmap #24): deterministic `same_as` edges.

Entities are keyed `type:name` and entity resolution only ever merges within one type,
so the same real-world thing shows up as `service:connector` (Octopus), `repo:connector`
(git/deps) and a `pipeline:*` that builds it — three islands the graph cannot cross.
Measured on the live AppRiver workspace (2026-07-20): 158 exact service↔repo name
matches, 305 pipeline↔repo, 147 service↔pipeline — yet only 18 of 3,580 entities had
evidence from more than one source.

This module computes `same_as` edges between entities of DIFFERENT types whose names are
exact matches after normalization. Deliberately a *bridge*, not a merge: both nodes
survive with their own provenance, a wrong bridge is one row to delete, and the type
distinction (a service is not its repo) stays real.

Honesty contract:
- A bridge is an inference from name equality — no document asserts the identity. It is
  stored with an EMPTY evidence_doc_id so it can never be cited as document evidence,
  `edge_corroboration` counts it as zero, and the confidence layer scores it as its own
  lowest-tier class ('name-bridge'). The `detail` says exactly what it is.
- Guards against false identity: cross-type only; normalized name must be >= 5
  alphanumeric chars; names made entirely of generic tokens ("api", "core", "web"...)
  are skipped; a name shared by more than `MAX_GROUP` entities is treated as generic
  and skipped entirely (a real identity is not shared seven ways).
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Iterable

from quickjoiner.connectors.deps import _GENERIC_TOKENS

# The runtime/code identity cluster — the types whose name collisions were measured to be
# the same real-world thing. Tickets/people/environments etc. are excluded on purpose.
BRIDGE_TYPES = frozenset({"service", "repo", "project", "pipeline"})

MIN_NORM_LEN = 5  # "api"/"web"/"core" are too short & generic to assert identity
MAX_GROUP = 6  # a name shared by more entities than this is generic, not an identity

_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Collapse a spoken/dotted/hyphenated name to its comparable core:
    'AppRiver.Connector' / 'appriver-connector' / 'AppRiver Connector' all agree."""
    return _ALNUM.sub("", (name or "").lower())


def _all_generic(name: str) -> bool:
    tokens = [t for t in _ALNUM.split((name or "").lower()) if t]
    return bool(tokens) and all(t in _GENERIC_TOKENS for t in tokens)


def compute_same_as_bridges(
    entities: Iterable[dict[str, Any]],
    aliases: Iterable[tuple[str, str]] = (),
) -> list[tuple[str, str, str, str]]:
    """(src, 'same_as', dst, detail) rows for every cross-type exact-name identity.

    `aliases` — optional (entity_id, alias) pairs — put an entity into extra name
    groups, so a *declared* other name (the connector's "also known as" field, or the
    org-convention spoken forms from deps.py) bridges what raw names never could:
    repo "Stevedore" aka "appriver.provisioning" ⇔ `service:appriver.provisioning`.
    An alias is a name *claim*, so the same guards apply to it as to a name.

    One row per pair, endpoints in sorted order (the graph consumers are undirected).
    Pure: rows with id/name/type in, edge tuples out — no I/O."""
    by_id: dict[str, dict[str, Any]] = {
        e["id"]: e for e in entities if e.get("type") in BRIDGE_TYPES
    }
    # norm -> {entity_id: via} — via is None for the entity's own name, else the alias
    # that put it there (kept for the detail text; own-name membership wins).
    groups: dict[str, dict[str, str | None]] = {}

    def _join(norm_source: str, entity: dict[str, Any], via: str | None) -> None:
        norm = normalize_name(norm_source)
        if len(norm) < MIN_NORM_LEN or _all_generic(norm_source):
            return
        members = groups.setdefault(norm, {})
        if via is None or entity["id"] not in members:
            members[entity["id"]] = via

    for e in by_id.values():
        _join(e.get("name") or "", e, None)
    for entity_id, alias in aliases:
        e = by_id.get(entity_id)
        if e is not None and alias:
            _join(alias, e, alias)

    bridges: list[tuple[str, str, str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for norm, members in groups.items():
        if len(members) < 2 or len(members) > MAX_GROUP:
            continue  # nothing to bridge / too generic to trust
        rows = sorted(members.items())
        for (aid, avia), (bid, bvia) in combinations(rows, 2):
            a, b = by_id[aid], by_id[bid]
            if a["type"] == b["type"] or (aid, bid) in seen_pairs:
                continue  # same-type dedup is entity resolution's job, not ours
            seen_pairs.add((aid, bid))
            how = f"same normalized name '{norm}'"
            if avia or bvia:
                how += f" (via declared alias {avia or bvia!r})"
            detail = (
                f"deterministic identity bridge: {how} "
                f"({a['type']} = {b['type']}); no document asserts this identity"
            )
            bridges.append((aid, "same_as", bid, detail))
    return bridges
