"""Promotion: a personal document becomes the organisation's, by review.

Knowledge scopes made a taught note private by default — the right default, and on its own
a dead end: the useful half of what a new joiner works out is exactly what the next joiner
needs, and nothing carried it across. This is that path, and it is deliberately a *review*
rather than a button, because the whole reason a note starts private is that one person's
unreviewed assertion must not become citable org truth for everybody by accident.

**A metadata flip, not a copy.** A document's `doc_id` is derived from `(source_id, uri)` at
ingest and treated as opaque forever after, so promotion rewrites which source owns it and
nothing else: the same chunks, the same vectors, the same `evidence_doc_id` on every graph
edge, the same labels, the same citations. No text is re-chunked and no embedding is
recomputed. Three writes make it real — the catalog row, the vector rows and the FTS rows —
because retrieval filters on the chunk's *own* copy of `source_id`, and a document moved in
one place but not the others would be retrievable through one leg and invisible through the
other.

The three-step shape and why each step exists:

* **request** — the owner offers it. Only they can, and only for a document in a source only
  they can read; there is nothing to promote otherwise.
* **queue** — a reviewer can read a requested document *even though it is still private*.
  That is a real, narrow exception to knowledge scopes, and it is sound because the owner
  explicitly offered it; it is scoped to documents in `requested` state and to the review
  endpoints, never to retrieval, so an offered note is still not answerable to anyone but
  its author until it is approved.
* **decide** — approve moves it to the commons; decline records the reason and leaves it
  exactly where it was.

Loose end, stated rather than discovered: the ingest-time merge guard blocked this
document's entities from merging into org ones while it was private, and promotion releases
those entities (`rehome_entities`) but does not re-run entity resolution — so a promoted
note that spells a service differently keeps its own node until the next `qj regraph` or
re-sync. That is a missing merge, never a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass

PROMOTED_SOURCE = "promoted:org"
PROMOTED_SOURCE_NAME = "Promoted to the organisation"

REQUESTED = "requested"
DECLINED = "declined"


class PromotionError(Exception):
    """A promotion that cannot proceed, with a reason meant for a person to read."""


@dataclass
class PromotionResult:
    doc_id: str
    status: str
    source_id: str
    message: str


def _document(catalog, doc_id: str) -> dict:
    doc = catalog.get_document(doc_id)
    if doc is None:
        raise PromotionError("That document is not in memory.")
    return doc


def request(catalog, doc_id: str, user: str | None, note: str = "") -> PromotionResult:
    """Offer one of your own private documents to the organisation.

    Refuses a document that is already readable by everyone — not as pedantry but because
    "promote" would then be a no-op that reads as success, and the requester would go on
    believing they had published something they had not.
    """
    doc = _document(catalog, doc_id)
    source_id = doc["source_id"]
    if not catalog.is_private_source(source_id):
        raise PromotionError("That document is already readable by everyone — "
                             "there is nothing to promote.")
    if not _owns(catalog, source_id, user):
        raise PromotionError("Only the owner of a private document can offer it to the "
                             "organisation.")
    if doc.get("promotion_status") == REQUESTED:
        raise PromotionError("That document is already waiting for review.")
    catalog.set_promotion(doc_id, REQUESTED, by=user or "", note=note)
    return PromotionResult(doc_id, REQUESTED, source_id,
                           "Offered to the organisation — a reviewer will decide.")


def pending(catalog) -> list[dict]:
    """The review queue, oldest first."""
    return catalog.list_promotions(REQUESTED)


def decide(catalog, store, doc_id: str, approve: bool, reviewer: str | None = None,
           note: str = "") -> PromotionResult:
    """Approve or decline a pending offer.

    On approval the document moves to `PROMOTED_SOURCE`, an ownerless commons bucket, so
    every existing visibility predicate starts including it with no read-path change at
    all — which is the whole reason promotion is expressed as a source move.
    """
    doc = _document(catalog, doc_id)
    if doc.get("promotion_status") != REQUESTED:
        raise PromotionError("That document has not been offered for review.")
    if not approve:
        catalog.set_promotion(doc_id, DECLINED, by=reviewer or "", note=note)
        return PromotionResult(doc_id, DECLINED, doc["source_id"],
                               "Declined — the document stays private to its author.")

    origin = doc["source_id"]
    catalog.upsert_source(PROMOTED_SOURCE, PROMOTED_SOURCE_NAME, "promoted")
    catalog.move_document(doc_id, PROMOTED_SOURCE)
    store.move_document(doc_id, PROMOTED_SOURCE)
    _release_entities(catalog, doc_id, origin)
    return PromotionResult(doc_id, "promoted", PROMOTED_SOURCE,
                           "Promoted — everyone can now find and cite this.")


def _owns(catalog, source_id: str, user: str | None) -> bool:
    row = catalog.get_source(source_id)
    return bool(row and user and row.get("owner") == user)


def _release_entities(catalog, doc_id: str, origin_source_id: str) -> int:
    """Hand the entities this document minted to the promoted bucket.

    While the document was private the merge guard kept its entities out of the `same_as`
    bridge layer, keyed on the *minting* source. Promotion publishes the document, so those
    names are org-visible through its edges anyway and withholding the bridges would only
    make the graph worse. Scoped to entities this document actually cites, so a promotion
    cannot release the rest of a private bucket's graph. Best-effort: an untidy bridge
    layer is a far smaller problem than a promotion that failed half-way.
    """
    try:
        moved = catalog.rehome_document_entities(doc_id, origin_source_id, PROMOTED_SOURCE)
        catalog.refresh_same_as_bridges()
        return moved
    except Exception:
        return 0
