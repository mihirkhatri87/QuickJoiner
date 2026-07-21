# Plan 07 — Multimodal derive-to-text: vision, audio, video (ingest + live tools + UI)

> **STATUS: ⬜ NOT STARTED.** Accepted via AI_ROADMAP intake 2026-07-18 (item #23, Tier 3).
> See [STATUS.md](STATUS.md).

Effort: ~9–12 dev-days across 5 phases (A–E; A+B is a demo-able first slice) ·
Dependencies: none (LiteLLM enhancement pack shipped 2026-07-18 provides the retry/strip
machinery this reuses) · Gate: every acceptance criterion in §12 maps to a covering test;
"summarize this recording" works end-to-end in a conversation on a scratch workspace.

## 0. Motivation

Org knowledge lives in formats the text-native stack cannot read today: **architecture
diagrams** (a Confluence page whose entire content is one draw.io export contributes ~nothing
to retrieval), screenshots in tickets, and **recorded meetings/walkthroughs** that never got
written up. Separately, users will ask conversational one-shots ("summarize
`sprint-42.mp4`") that must not require ingesting anything first.

The answering model (gpt-oss-120b on the live workspace) has no vision or audio and **must not
be swapped for a weaker multimodal model** — tool-calling discipline and multi-hop reasoning
are why it was chosen. The design principle is therefore:

> **Derive-to-text.** Modality specialists (a vision model, an ASR model) convert
> images/audio/video into *cited, provenance-stamped text*; the existing text stack
> (embeddings, hybrid retrieval, grounding gate, knowledge graph, one answering LLM) handles
> that text unchanged. The agent never becomes multimodal; its evidence does.

This mirrors the extractor pattern already in the tree: `deps.py` (manifests → dependency-map
docs), `code_graph.py` (source → edges), `pubsub.py` (SDK patterns → runtime edges),
`triples.py` (prose → relations). Vision/audio are two more extractors on the same seams —
plus a live-tool surface mirroring `/scrape` + `agent/ops.py` ("both hit the same service
functions"; learning stays explicit).

## 1. Architecture — one primitives layer, three surfaces

```
quickjoiner/media/  (new package — ALL derivation logic lives here, surfaces are thin)
├── capabilities.py   resolve what's possible for the configured providers (pure)
├── probe.py          ffmpeg/ffprobe wrappers + presence detection (injectable run seam)
├── vision.py         describe_images(...) — per-provider adapters (litellm/ollama/anthropic)
├── transcribe.py     transcribe(...) — OpenAI-compatible /audio/transcriptions client
│                     (+ optional local faster-whisper backend, extra `.[media-local]`)
├── diagrams.py       DETERMINISTIC text/edge extraction from SVG / .drawio / .excalidraw
├── frames.py         keyframe selection: ffmpeg scene sampling + perceptual-hash dedupe (pure core)
└── derive.py         orchestration: derive_image / derive_audio / derive_video →
                      artifacts in <workspace>/media/<sha16>/ + condensation (map-reduce,
                      deterministic keyless fallback — sessions.py digest pattern)

Surface 1 — ingest (sync):        pipeline seam beside deps/code_graph; auto-derive docs
Surface 2 — live agent tool:      analyze_media(path, focus) in conversation; explicit learn
Surface 3 — UI plumbing:          composer attach → POST /api/upload → staged path in message
```

**Invariants preserved (verify in review):**
- **I1 grounding gate untouched.** Derived docs are ordinary text documents → chunks → cosine.
  No per-modality routing at answer time; `min_score` semantics unchanged.
- **Honesty.** A vision description is *interpretive* (a model can misread a diagram). Every
  derived doc carries an in-band provenance header — `> Derived from <file> by <model> on
  <date>; verify against the original` — and a **discounted evidence class** in
  `confidence.py` (§5). Deterministic diagram extraction (§4.3) emits `references` edges
  only — no direction guessed, same posture as `pubsub.py`.
- **Explicit learning** for conversational derivations: `analyze_media` output is turn-local;
  nothing enters memory unless the user says learn (the `/scrape` rule).
- **Keyless-safe / cost-gated.** Master gate `media.enabled=False`; ingest separately
  `media.ingest=False`. When off: no tools registered, no upload UI, zero sync overhead.
- **No catalog schema change.** Derived docs use the existing documents/chunks/edges tables;
  Postgres parity holds automatically (state this in the final report; no pgvector re-run
  needed unless something changes after all).

## 2. Provider capability matrix (the "optional per provider" design)

Capabilities are **resolved from config, never hardcoded per provider**. `media.enabled` is
the master switch; within it, each modality lights up only when its dependency chain exists:

| Capability | litellm | ollama | anthropic |
|---|---|---|---|
| **Vision** | `media.vision_model` on the same proxy (e.g. `openai/RedHatAI/gemma-3-27b-it-quantized.w4a16`), OpenAI `image_url` content parts | native `images` (base64) on `/api/chat` with a vision model (`media.vision_model`, e.g. gemma4:cloud — the live default workspace's model already has vision) | native Claude image blocks — `media.vision_model` defaults to `llm.model` (no second model needed) |
| **Audio (ASR)** | `{transcribe_url or llm.base_url}/audio/transcriptions` with `media.transcribe_model` (whisper-large-v3 on the broker) | no ollama ASR endpoint → `media.transcribe_url` pointing at any OpenAI-compatible whisper server, OR local `faster-whisper` (`.[media-local]` extra) | same as ollama: external `transcribe_url` or local extra (Anthropic has no ASR API) |
| **Video** | audio-capability **AND** ffmpeg present (frames additionally need vision) | same | same |

`capabilities.py::resolve(config) -> MediaCapabilities(vision: bool, audio: bool, video:
bool, reasons: dict[str, str])` — pure, unit-tested across the full matrix. `reasons` carries
human-readable "why not" strings ("audio: no transcribe_url configured and faster-whisper not
installed"; "video: ffmpeg not found on PATH") surfaced verbatim in the Settings UI and the
attach-button tooltip. `GET /api/settings` returns the resolved capabilities alongside the
raw `media` config; `qj status` prints them.

So "only when litellm is being used" falls out naturally: a litellm workspace pointing at the
AppRiver broker configures two model keys and gets everything; an anthropic workspace gets
vision for free and audio only if they stand up a whisper endpoint; an ollama workspace gets
vision if their model has it and audio via the local extra. Nothing anywhere branches on
`llm.provider == "litellm"`.

**Config (`MediaConfig` on `Config.media`):**
```python
class MediaConfig(BaseModel):
    enabled: bool = False          # master gate: live tools + upload UI + capability resolution
    ingest: bool = False           # auto-derive during sync (costs LLM/ASR calls; needs enabled)
    vision_provider: str | None = None   # None -> llm.provider
    vision_model: str | None = None      # None -> llm.model for anthropic/ollama; REQUIRED for litellm
    transcribe_url: str | None = None    # None -> llm.base_url when provider is litellm, else unset
    transcribe_model: str = "whisper-large-v3"
    transcribe_local: bool = False       # use faster-whisper (.[media-local]) instead of HTTP
    max_frames: int = 12                 # vision-call budget per video
    frame_max_px: int = 1024             # deterministic downscale before vision (token saver)
    min_image_bytes: int = 10_240        # skip icons/pixels below this
    min_image_px: int = 64               # and below this dimension
    max_upload_mb: int = 512
    max_audio_minutes: int = 180         # refuse absurd inputs with a clear message
```

## 3. Inference layer (the actual model calls)

All three vision adapters live behind one function — `vision.describe_images(cfg, images:
list[bytes], prompt: str) -> str` — with per-provider wire shapes:

- **litellm/OpenAI-compat**: `messages=[{role:"user", content:[{type:"text",...},
  {type:"image_url", image_url:{url:"data:image/png;base64,..."}}...]}]` to
  `/chat/completions` with `model=media.vision_model`. Reuses the shipped retry/backoff +
  `_strip_rejected` helpers (import from `litellm_provider`) and an injectable transport.
  Multi-image in one call where possible (one request for N frames, not N requests).
- **ollama**: `/api/chat`, message carries `images: [<b64>, ...]`.
- **anthropic**: `{type:"image", source:{type:"base64", media_type, data}}` blocks; frames are
  already ≤`frame_max_px` so per-image token cost stays bounded.

`transcribe.py`: multipart POST `{file, model, response_format: "verbose_json"}` →
segments with timestamps (the citation currency: `[recording.mp4 @ 12:34]`). Long audio is
**pre-chunked deterministically** (~10-minute segments on silence boundaries via ffmpeg
`silencedetect`, falling back to fixed windows) so one flaky 90-minute request becomes nine
resumable ones; segment offsets re-based so timestamps stay global. Optional
`transcribe_local` backends run in-process with no network, behind one interchangeable
seam (same timestamped-segment return shape):
- `faster_whisper` (CPU int8 default) — the quality option; strongest on accents, noisy
  meeting audio, and the proper nouns/jargon that entity extraction depends on.
- `vosk` (Kaldi-based, ~50 MB models, plain pip install, true streaming, per-word
  confidence) — the lightweight option for weak CPUs / minimal-dependency installs.
  Trade-offs to encode honestly: no punctuation/casing (degrades chunking slightly;
  misheard entity names break graph linking), but structurally hallucination-free —
  unlike whisper it never fabricates fluent text over silence (whisper's known failure,
  mitigated for it by the silence-trim rung). Its per-word confidence can annotate
  low-confidence transcript spans for the `derived-media` evidence class.
Backend choice: `media.transcribe_local_backend: "faster-whisper" | "vosk"`; capability
resolution reports whichever is importable.

These are **single-shot description/transcription calls — no tool calling involved** — which
is exactly the regime where a quantized gemma is safe (sidesteps the tool-parser risk
entirely) and why the answering model never changes.

## 4. Deterministic load-reduction ladder (do cheap things first, always)

Ordered; each rung either avoids an LLM/ASR call entirely or shrinks one. All pure/testable.

1. **Skip lists** — extension allowlists per modality; images below `min_image_bytes` /
   `min_image_px` (icons, badges, tracking pixels) skipped outright. *0 calls.*
2. **sha256 derivation cache** — artifacts keyed by source-binary hash under
   `<workspace>/media/<sha16>/`; unchanged file ⇒ derive **once ever**, re-syncs and repeat
   tool calls are free. (`media_meta.json` records model+params so a model upgrade can
   invalidate deliberately.) *0 calls on every re-encounter.*
3. **Text-format diagrams never see the vision model.** `.svg`, `.drawio`, `.excalidraw`
   (and mermaid, already text) are XML/JSON — `diagrams.py` extracts node labels, edge
   arrows, and free text deterministically → a `diagram` doc + `references` graph edges
   (labels resolved via `catalog.resolve_entity`, `_GENERIC_TOKENS` guarded, no direction
   asserted). This covers the single most valuable diagram class (draw.io in Confluence)
   with **zero** LLM calls. Only raster exports (PNG screenshots of diagrams) fall through
   to vision.
4. **ffprobe metadata header** — duration/resolution/codec/streams as deterministic doc
   header lines; also powers the refuse-early checks (`max_audio_minutes`). *0 calls.*
5. **Frame economy** — ffmpeg scene-change selection, then perceptual-hash (Pillow, pure
   Python) near-duplicate collapse (a 40-minute screen-share of one slide deck → a handful
   of distinct frames), capped at `max_frames`, each downscaled to `frame_max_px` before
   encoding (a deterministic token saver on every provider). *N_frames ≤ 12 calls, usually
   1 multi-image call.*
6. **Audio economy** — 16 kHz mono downmix + silence trim before upload: smaller payloads,
   faster ASR, fewer chunks.
7. **Condensation with a keyless fallback** — transcripts beyond the live-tool cap are
   map-reduce condensed (timestamps preserved) with the LLM; without a provider, the
   deterministic digest fallback (the `sessions.py` compression pattern) still returns
   something honest. The **full** artifact is always saved first — condensation only shapes
   what re-enters context, never what's stored, so "learn this" ingests everything.

## 5. Ingestion surface (Surface 1) + knowledge graph

- **Seam**: beside `deps.py`/`code_graph.py` in the sync path for `files`/`git` trees
  (phase D; Confluence/Jira **attachment fetching** is phase E — those connectors don't pull
  attachments today, and that's where the diagrams actually live). Gated on `media.enabled
  AND media.ingest`; a per-source `media=false` option can exclude a noisy source.
- **Documents**: `<uri>::vision-description` (kind `image-description`),
  `<uri>::transcript` (kind `meeting`), `<uri>::diagram` (kind `diagram`, deterministic).
  Hash-keyed to the parent binary (rung 2) → idempotent through the existing sha256 dedupe;
  `delete_documents_for_source` / clean re-sync purge derived docs + `<workspace>/media/`
  artifacts together (extend `sync_manager` cleanup).
- **confidence.py**: new evidence class `derived-media`, discounted like meeting-notes
  (transcripts literally *are* meetings; image descriptions are model-interpretive).
  `classify_evidence` keys on the doc kinds above. Deterministic `diagram` docs rank as
  authored-doc-adjacent — they are literal extractions, not interpretations.
- **Graph**: derived text flows through the existing extractors automatically (ticket-key
  regex, entity mentions, alias expansion benefits); `diagrams.py` adds its `references`
  edges with the diagram doc as evidence. **No new LLM triple path** — `graph.extract_triples`
  applies to derived prose only if the user already turned it on.

## 6. Live tool surface (Surface 2)

One tool, registered **only when capabilities allow** (its description tells the model what
it can take, so an audio-less workspace never advertises transcription):

- `analyze_media(path: str, focus: str = "")` — dispatch by extension:
  image → describe; audio → transcribe + condense; video → ffprobe → audio track transcript
  + keyframe descriptions → one fused markdown return. Output = condensed markdown with
  timestamps + the provenance header + the saved artifact path + an explicit "condensed;
  full transcript saved at …" note when condensation fired. `focus` threads a user question
  into the vision prompt ("what does the deploy pipeline diagram show?").
- **Path safety** (`media/paths.py`, pure): a resolved path must be inside one of — the
  visible sources' roots (files roots, `<workspace>/repos/` clones), `<workspace>/uploads/`,
  `<workspace>/media/`. `Path.resolve()` + `is_relative_to`; rejects traversal, symlink
  escapes, UNC tricks. Tool returns a clear refusal otherwise. (Tool inputs are model
  output — treat as untrusted.)
- **Learning stays explicit**: after an analyze, the user's "learn this" ingests the saved
  artifacts via the pipeline into a `media:learned` bucket (`upsert_source`, `configured=0` —
  the taught-notes pattern). Wire as both a follow-up UI affordance (§7) and a
  `learn_media(path)` agent tool that requires the artifact to already exist.
- **prompts.py**: short guidance — cite `[file @ mm:ss]`; derived content is interpretive;
  prefer memory over re-deriving; never analyze paths the user didn't give (mirror the
  scrape rule).
- Result flows through the existing `live_tool_result_max_chars` cap as a backstop; the
  map-reduce condensation targets ~half the cap so the backstop never truncates blindly.

## 7. Backend API (Surface 3 plumbing)

- `POST /api/upload` — multipart, auth-gated (`_user()`), `max_upload_mb` enforced
  streaming-side, extension+sniff allowlist (images/audio/video only), stored under
  `<workspace>/uploads/<uuid>-<safe-name>`; returns `{path}` for the composer to reference.
- `GET /api/media/artifact?path=` — serves saved transcripts/descriptions for the
  ArtifactModal; **scoped to `<workspace>/media/` via the same path-safety resolver** (no
  traversal; test it).
- `POST /api/media/test` — the `/api/llm/test` twin: probes vision with a tiny embedded PNG
  and transcription with a ~1 s embedded WAV, accepts unsaved `media` overrides so the
  Settings form verifies before saving, never persists.
- `GET/PATCH /api/settings` — round-trips `media` (add to the settings surface + tests);
  response includes `media_capabilities` (resolved) for the UI.
- SSE chat: unchanged — `tool_call` events already stream; a long transcription is just a
  slow tool round (the 300 s httpx timeout in the transcribe client is per-chunk thanks to
  §3 pre-chunking).

## 8. Frontend (phase C; matches the "evidence ledger" skin)

- **Composer**: 📎 attach button (hidden when `media.enabled` off; tooltip = capability
  `reasons` when a modality is unavailable). Upload with progress → chip above the input →
  on send, message text gets `[attached: uploads/<name>]` appended so the agent sees the
  path naturally. Reject oversize/unsupported client-side first.
- **SettingsDrawer**: new **Media** section — master + ingest toggles (ingest hinted
  "ingest-time, costs one call per new file"), vision provider/model fields, transcribe
  URL/model, max frames, and a **Test media** button (`/api/media/test`), mirroring the LLM
  section's layout and the shared `Toggle` primitive. Show resolved capabilities as
  green/grey stamps with reasons.
- **ArtifactModal**: transcripts/descriptions open via `GET /api/media/artifact` with the
  existing markdown renderer (timestamps render as plain text; mermaid in derived diagram
  docs renders via the existing `MermaidBlock`); **"Learn this"** posts the artifact to
  `/api/learn` (the scrape pattern, `onLearned` refresh included).
- Build discipline: `npm run build` (slow machine, 2–5 min, don't kill); no FE test harness
  exists yet (plan 04 Phase 0) — verification is typecheck + Playwright screenshot of attach
  → analyze → artifact flow, per current practice.

## 9. ffmpeg integration

- `probe.py`: `shutil.which("ffmpeg"/"ffprobe")` detection feeding capabilities;
  `ffprobe -print_format json -show_format -show_streams` parsed via a fixture-tested pure
  function; audio extraction (`-vn -ac 1 -ar 16000`), keyframes
  (`-vf "select='gt(scene,0.3)'"` + fps fallback for slideless video), `silencedetect`.
  All subprocess calls go through an injectable `run` seam so unit tests never need the
  binary; integration tests `skipif` ffmpeg absent.
- **This machine (Windows)**: `winget install Gyan.FFmpeg` (document in README).
- **Docker**: `--build-arg WITH_MEDIA=1` installs ffmpeg (the `WITH_BROWSER` pattern);
  `.[media]` extra pins Pillow (phash + resize), `.[media-local]` adds faster-whisper.

## 10. What is deliberately NOT in scope

- Swapping or augmenting the **answering** model — gpt-oss (or whatever `llm.model` is)
  remains the only agent brain. No multimodal message format in `llm/base.py`.
- Image **embeddings** / CLIP-style visual retrieval — retrieval stays text-native.
- OCR engines (tesseract) — the vision model covers raster text; revisit only with eval
  evidence.
- Real-time/streaming transcription, speaker diarization, video *generation* — no.
- PDF page rendering → vision (PDFs already ingest as text; scanned-PDF support is a
  possible follow-up, not this plan).

## 11. Testing matrix

| Layer | Tests (all offline/deterministic unless marked) |
|---|---|
| capabilities | full provider × config matrix incl. every `reasons` string |
| diagrams.py | SVG/drawio/excalidraw fixtures → labels, edges, generic-token guard, malformed XML never raises |
| frames.py | synthetic Pillow images → phash dedupe collapses near-duplicates, cap respected, downscale applied |
| derive.py | condensation chunking, timestamp preservation, keyless digest fallback, artifact layout + meta, cache short-circuit on same sha |
| vision adapters | wire-shape per provider via MockTransport (litellm strip-and-retry inherited-behavior test incl.) |
| transcribe | multipart shape, verbose_json segment parsing, chunk offset re-basing; local backend behind import guard |
| probe.py | ffprobe JSON fixture parsing; which-detection fake; `skipif`-gated real-ffmpeg smoke |
| paths | traversal/symlink/UNC rejection, allowed roots accepted (Windows + posix cases) |
| agent tool | conditional registration; dispatch per extension with faked derive fns; condensed-note honesty; ScriptedProvider loop with an analyze_media call |
| pipeline seam | derived docs created idempotently; re-sync no-op on unchanged binary; clean re-sync purges docs + artifacts; confidence class mapping |
| API | upload roundtrip/auth/size/type-reject; artifact endpoint scoping; `media` settings roundtrip; `/api/media/test` ok + failure |
| eval | `docs/evals/multimodal.yaml` cases (also restores the missing `docs/evals/` dir — clears the standing `test_multi_hop_eval_set_file_loads` failure by re-adding `multi-hop-crosssource.yaml`) |

## 12. Acceptance criteria

1. With `media.enabled=false` (default): zero behavior change anywhere — no tools, no attach
   button, no sync overhead; full suite green with no media deps installed.
2. Capability resolution: each cell of the §2 matrix produces the documented result, with a
   reason string for every unavailable modality, on all three providers.
3. A `.drawio`/`.svg` diagram in a synced repo yields a `diagram` doc + `references` edges
   **without any LLM call** (assert call-count zero).
4. An image in a synced tree (above thresholds) yields a provenance-stamped
   `image-description` doc exactly once across two syncs (hash cache).
5. `analyze_media` on an mp4 in a conversation returns a timestamped, condensed summary
   citing `[file @ mm:ss]`, saves full artifacts, and states when condensation fired; the
   answer is NOT ingested until the user learns it, after which it is retrievable and cited.
6. Path safety: traversal attempts from tool input and from `/api/media/artifact` are
   rejected (tests on both).
7. Upload → attach → analyze → artifact modal → "Learn this" works in the browser (Playwright
   walkthrough, screenshots) with `npm run build` clean.
8. `confidence.py` scores derived-media evidence below authored docs; plan-06 candidate
   confidence reflects it (unit test at the `classify_evidence` level).
9. Long-audio: a >cap transcript round-trips through condensation with global timestamps
   intact (fixture test; no real hour-long file in CI).
10. Suite green from `.venv`; no catalog schema change (explicitly asserted in review);
    CLAUDE.md + README + AI_ROADMAP/STATUS updated per house rules with each shipped phase.

## 13. Risks & honest notes

- **Broker gemma serving**: single-shot vision calls avoid the tool-parser risk, but
  multi-image support and max image size on the RedHatAI/vLLM deployment are unverified —
  phase A includes a live probe script; fall back to per-frame calls if multi-image fails.
- **Vision quality on quantized 27B** is unproven for dense diagrams — the eval cases decide
  whether raster-diagram description is worth keeping on by default or ingest stays
  diagrams+audio only. Deterministic rungs (§4) are valuable regardless.
- **Long tool rounds**: a 1-hour recording ≈ minutes of wall clock inside one agent round.
  Acceptable for v1 (SSE keeps streaming tool_call events); background jobs à la
  `sync_manager` are a follow-up if it bites.
- **Windows**: ffmpeg on PATH, cp1252 console (already mitigated by the UTF-8 stdout fix),
  long paths in `media/<sha16>/frames/`.
- **Privacy**: transcripts of meetings can carry sensitive talk — ingest is opt-in per the
  gates, and the gaps privacy precedent (`store_queries`) is the model if per-workspace
  redaction is ever requested.

## 14. Phases

| Phase | Delivers | Effort |
|---|---|---|
| A | `media/` primitives: capabilities, probe, vision adapters, transcribe, diagrams, frames, derive + artifacts + cache; config; unit tests; live probe script for the broker | ~3d |
| B | Live tool surface: `analyze_media` + `learn_media`, path safety, prompts guidance, condensation wiring; demo-able in `qj chat` | ~2d |
| C | API + frontend: upload, artifact, media/test, settings roundtrip; composer attach, Settings Media section, ArtifactModal flow; `npm run build` | ~2–3d |
| D | Ingest surface on files/git + confidence class + graph edges + eval cases (restoring `docs/evals/`) | ~1.5d |
| E | Connector attachments (Confluence + Jira fetch), Docker `WITH_MEDIA`, README/CLAUDE/roadmap doc pass, Playwright walkthrough | ~1.5–2d |

Ship order A→B→C→D→E; each phase lands with its tests and doc updates (house rules). A+B
alone already delivers the conversational "summarize this recording" story.

## 15. Ready-to-paste implementation prompt

```
Read CLAUDE.md fully, then docs/plans/07-multimodal-media.md — it is the spec; follow its
phases in order (A→B→C→D→E) and do not redesign what it fixes. Key constraints you must not
violate: derive-to-text only (the answering LLM never becomes multimodal; llm/base.py's
neutral message format is untouched); the grounding gate and catalog schema are unchanged;
media.enabled defaults False and phase-1 behavior with it off must be byte-identical to
today; every derived document carries the provenance header and the derived-media confidence
class; deterministic rungs (skip lists, sha-cache, diagrams.py XML extraction, ffprobe,
phash frame dedupe, downscale) run BEFORE any model call; conversational derivations are
never auto-ingested (explicit learn only); all subprocess (ffmpeg) and HTTP (vision/ASR)
calls go through injectable seams and are fully unit-tested offline (MockTransport /
fixture JSON); path inputs from the model or HTTP are untrusted — use the plan's path-safety
resolver everywhere. Environment: use .venv\Scripts\python.exe (never system Python);
tests: .venv\Scripts\python.exe -m pytest -q; frontend: prepend
$env:Path = "$env:LOCALAPPDATA\nvm\v22.23.1;$env:Path" then npm run build in frontend/
(slow — do not kill); live verification only on a scratch workspace + scratch port (never
~/.quickjoiner/default, never 8787). After each phase: full suite green, CLAUDE.md +
README.md + docs (AI_ROADMAP #23, plans/STATUS.md, this plan's banner) updated in the same
change. Final report: map every §12 acceptance criterion to its covering test; name unmet
items honestly.
```
