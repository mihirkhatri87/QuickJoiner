"""Webhook receivers: push-mode learning. Events are verified then ingested (or trigger
a full re-sync — see `wants_resync` below).

Register a webhook in the external system pointing at:
    POST /hooks/{source_name}
Set option `webhook_secret` on the source (or env indirection: webhook_secret=env:NAME).
Supported verification schemes, checked in order — pick whichever the sending system can
actually produce; most systems outside GitHub/GitLab can't compute a per-request HMAC at
all, which is what the `token` query param exists for:
  - GitHub:  X-Hub-Signature-256: sha256=<hmac-sha256(body)>
  - GitLab:  X-Gitlab-Token: <secret>  (plain shared token, not hashed)
  - Generic: X-QJ-Signature: <hmac-sha256(body) hex>  (for a sender that CAN sign)
  - Query param: POST /hooks/{source_name}?token=<secret>  (plain shared secret in the
    URL itself — for a sender that can only configure a bare callback URL and nothing
    else: Azure DevOps Service Hooks and Octopus Deploy subscriptions both fall in this
    bucket, and this is the only scheme either can actually satisfy).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from quickjoiner.connectors.registry import create_connector
from quickjoiner.connectors.util import resolve_secret


def verify_signature(secret: str, body: bytes, headers: dict[str, str], token: str | None = None) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    github_sig = headers.get("x-hub-signature-256", "")
    if github_sig:
        return hmac.compare_digest(github_sig, f"sha256={digest}")
    gitlab_token = headers.get("x-gitlab-token", "")
    if gitlab_token:
        return hmac.compare_digest(gitlab_token, secret)
    generic = headers.get("x-qj-signature", "")
    if generic:
        return hmac.compare_digest(generic, digest)
    if token:
        return hmac.compare_digest(token, secret)
    return False


def build_hooks_router(ctx, syncs) -> APIRouter:
    router = APIRouter()

    @router.post("/hooks/{source_name}", tags=["Webhooks"], summary="Receive a verified push/webhook event from a source — ingests it immediately, or triggers a full re-sync for a connector that needs one (PUSH mode).")
    async def receive_hook(source_name: str, request: Request, token: str | None = None):
        source = next((s for s in ctx.config.sources if s.name == source_name), None)
        if source is None:
            raise HTTPException(status_code=404, detail=f"No source named {source_name!r}")

        secret = resolve_secret(source.options, "webhook_secret", "QJ_WEBHOOK_SECRET")
        if not secret:
            raise HTTPException(
                status_code=403,
                detail="Source has no webhook_secret configured; refusing unauthenticated pushes",
            )
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verify_signature(secret, body, headers, token):
            raise HTTPException(status_code=403, detail="Signature verification failed")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Body is not valid JSON")

        connector = create_connector(source, ctx.workspace)
        # Register the source so pushed documents show up in /api/sources even if
        # the source has never been pull-synced.
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)

        if connector.wants_resync(payload):
            # A push event that can't be turned into documents directly (the git-clone
            # connector: a payload carries commit metadata, never file contents) — run
            # the real sync() instead, as a normal background job (same as "Sync now"),
            # so a slow git pull/read never blocks this webhook's HTTP response.
            try:
                job = syncs.start(source.name)
                return {"received": True, "resync": True, "job": job.summary()}
            except RuntimeError:
                # Already syncing/paused for this source — that run (or the next push)
                # covers it; refusing here would just make the sender retry pointlessly.
                return {"received": True, "resync": "already-in-progress"}

        docs = list(connector.handle_event(payload))
        stats = ctx.pipeline.ingest(docs, connector.source_id)
        return {"received": True, "documents": len(docs), "ingested": stats.summary()}

    return router
