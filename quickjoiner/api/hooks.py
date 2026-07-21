"""Webhook receivers: push-mode learning. Events are verified (HMAC) then ingested.

Register a webhook in the external system pointing at:
    POST /hooks/{source_name}
Set option `webhook_secret` on the source (or env indirection: webhook_secret=env:NAME).
Supported signature schemes, checked in order:
  - GitHub:  X-Hub-Signature-256: sha256=<hmac-sha256(body)>
  - GitLab:  X-Gitlab-Token: <secret>  (plain shared token)
  - Generic: X-QJ-Signature: <hmac-sha256(body) hex>
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from quickjoiner.connectors.registry import create_connector
from quickjoiner.connectors.util import resolve_secret


def verify_signature(secret: str, body: bytes, headers: dict[str, str]) -> bool:
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
    return False


def build_hooks_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.post("/hooks/{source_name}", tags=["Webhooks"], summary="Receive an HMAC-verified push/webhook event from a source and ingest it immediately (PUSH mode).")
    async def receive_hook(source_name: str, request: Request):
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
        if not verify_signature(secret, body, headers):
            raise HTTPException(status_code=403, detail="Signature verification failed")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Body is not valid JSON")

        connector = create_connector(source, ctx.workspace)
        # Register the source so pushed documents show up in /api/sources even if
        # the source has never been pull-synced.
        ctx.catalog.upsert_source(connector.source_id, source.name, source.type, source.options)
        docs = list(connector.handle_event(payload))
        stats = ctx.pipeline.ingest(docs, connector.source_id)
        return {"received": True, "documents": len(docs), "ingested": stats.summary()}

    return router
