"""Optional cross-encoder reranking stage (config: retrieval.reranker = "fastembed").

A cross-encoder reads query and candidate together, so it ranks far better than
bi-encoder cosine — at the cost of a forward pass per candidate. It therefore
runs only over the small fused candidate set, never the whole corpus.

Lazy at two levels: nothing is built unless the feature is on, and even then the
ONNX model is not imported/downloaded until the first `rank()` call — so starting
the app (or building a store you never search) costs nothing. The model downloads
once, then loads from the fastembed cache (FASTEMBED_CACHE_PATH pins it, same as
the embedder). If it can't load (e.g. offline first run), reranking degrades
permanently to the input (RRF) order instead of retrying every search.

The default is the **INT8 build** of ms-marco-MiniLM-L-6-v2 (23MB against the fp32
91MB), which fastembed's own catalogue does not carry — `add_custom_model` registers
the same HF repo pointed at its `onnx/model_quantized.onnx`, so it still downloads
and caches through the ordinary fastembed path. Measured on the live 57,659-chunk
corpus over the 32-case eval set (S4a, 2026-08-11): **-28.6% on the rerank stage**,
faster on 32 of 32 queries (paired ratio 0.63-0.80), with recall@k, grounded recall,
MRR, hop coverage and refusal accuracy all byte-identical to fp32.

That figure is paired and order-alternated for a reason. Two earlier framings of the
same experiment — in-process, then a same-session A/B — both ran fp32 first and both
reported -38%/-41%, while the UNTOUCHED embedder carried alongside as a control got
25% faster between the arms. The machine drifts under sustained ONNX load, so an
arm-at-a-time comparison hands roughly ten points of that drift to whichever arm ran
second. Interleaving the two encoders per query cancels it; the -28.6% is what
survives. Quantization does perturb individual scores — the returned
top-8 ORDER differs on 26 of 32 queries — but MRR is unchanged, i.e. the expected
document lands at the same rank in every answerable case and the reshuffling is
among candidates the eval set has no opinion about. `uint8` and `int8` builds of the
same repo were measured too and are slower (1517 / 1012 ms against this one's 918 in
a single run), so the variant is named explicitly rather than assumed — identical
23MB files are not interchangeable. That ordering survives the drift caveat above
without needing a paired re-run: drift favours whichever arm runs LATER, and `uint8`
ran last and still came last.

Falling back to fp32 matters and is deliberate: an existing install already has the
91MB fp32 model cached, so an upgrade that could only reach the new INT8 file would
turn an offline machine's working reranker into a silent degrade-to-RRF — a quality
regression caused purely by upgrading. A pinned `retrieval.reranker_model` is never
second-guessed this way; only the default gets the fallback.

Env kill-switch: set QJ_DISABLE_RERANKER=1 to force it off regardless of config
(used by the test suite to stay offline; handy for constrained deployments).
"""

from __future__ import annotations

import os

# The fp32 model fastembed ships in its own catalogue, and our alias for the INT8
# build of that same repo (registered on first use — see _register_quantized).
BASE_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
DEFAULT_RERANK_MODEL = BASE_RERANK_MODEL + "-int8"
QUANTIZED_MODEL_FILE = "onnx/model_quantized.onnx"


def _register_quantized() -> None:
    """Teach fastembed our INT8 alias. Idempotent: re-registering a name raises,
    and a process builds a reranker again on every settings change."""
    from fastembed.common.model_description import ModelSource
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    if any(m["model"] == DEFAULT_RERANK_MODEL for m in TextCrossEncoder.list_supported_models()):
        return
    TextCrossEncoder.add_custom_model(
        model=DEFAULT_RERANK_MODEL,
        sources=ModelSource(hf=BASE_RERANK_MODEL),
        model_file=QUANTIZED_MODEL_FILE,
        size_in_gb=0.023,
    )


class CrossEncoderReranker:
    _FAILED = object()  # sentinel: model load failed, don't keep retrying

    def __init__(self, model: str | None = None):
        self._model_name = model or DEFAULT_RERANK_MODEL
        self._pinned = bool(model)  # an explicit choice is never silently substituted
        self._encoder = None  # built lazily on first rank()

    def _candidates(self) -> list[str]:
        """Model names to try, in order. Only the default falls back to fp32."""
        if self._pinned or self._model_name != DEFAULT_RERANK_MODEL:
            return [self._model_name]
        return [DEFAULT_RERANK_MODEL, BASE_RERANK_MODEL]

    def _build(self, name: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        if name == DEFAULT_RERANK_MODEL:
            _register_quantized()
        cache = os.environ.get("FASTEMBED_CACHE_PATH")
        kwargs = {"cache_dir": cache} if cache else {}
        return TextCrossEncoder(model_name=name, **kwargs)

    def _ensure(self) -> None:
        if self._encoder is not None:
            return
        for name in self._candidates():
            try:
                self._encoder = self._build(name)
                self._model_name = name  # report what actually loaded
                return
            except Exception:
                continue
        self._encoder = self._FAILED  # offline / missing dep -> degrade quietly

    def rank(self, query: str, texts: list[str]) -> list[int]:
        """Return candidate indices ordered best-first (input order if unavailable)."""
        if not texts:
            return []
        self._ensure()
        if self._encoder is self._FAILED:
            return list(range(len(texts)))
        scores = list(self._encoder.rerank(query, texts))
        return sorted(range(len(texts)), key=lambda i: -float(scores[i]))


def create_reranker(retrieval) -> CrossEncoderReranker | None:
    """Build the configured reranker, or None. Construction is cheap (the model
    loads on first use); the QJ_DISABLE_RERANKER env var forces it off."""
    if os.environ.get("QJ_DISABLE_RERANKER"):
        return None
    if getattr(retrieval, "reranker", "none") != "fastembed":
        return None
    return CrossEncoderReranker(getattr(retrieval, "reranker_model", None))
