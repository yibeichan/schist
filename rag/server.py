"""
schist-rag — Lightweight RAG companion service for the schist MCP server.

Provides cross-encoder reranking and embedding via ONNX Runtime on ARM64 CPU.
Runs as a stateless localhost HTTP service; the MCP server calls it for ML-heavy
operations (reranking candidates, computing embeddings).

Architecture:
  FastAPI (uvicorn) → onnxruntime (CPU) → quantized MiniLM cross-encoder

Endpoints:
  POST /rerank  — Rerank a list of candidate documents by relevance to a query
  POST /embed   — Compute embedding vector(s) for text
  GET  /health  — Liveness check with model load status
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fastapi
import numpy as np
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("schist-rag")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
MODEL_CACHE = Path.home() / ".cache" / "schist-rag"
PORT = int(os.environ.get("SCHIST_RAG_PORT", "8788"))
HOST = os.environ.get("SCHIST_RAG_HOST", "127.0.0.1")
MAX_CANDIDATES = 30  # hard cap on how many documents we'll rerank in one call

# ---------------------------------------------------------------------------
# ONNX Runtime helper — loaded lazily so the module can be imported without
# triggering ORT init (useful for testing / import-time introspection).
# ---------------------------------------------------------------------------

_ort = None


def _get_ort():
    global _ort
    if _ort is None:
        import onnxruntime as ort

        _ort = ort
    return _ort


# ---------------------------------------------------------------------------
# Model loader
# ---------------------------------------------------------------------------


@dataclass
class Reranker:
    """Wraps a quantized ONNX cross-encoder model loaded via onnxruntime."""

    session: object = None
    tokenizer: object = None
    model_name: str = ""
    input_names: list[str] = field(default_factory=list)
    output_name: str = ""

    @classmethod
    def load(cls, model_name: str = DEFAULT_MODEL, cache_dir: Path = MODEL_CACHE) -> "Reranker":
        """Download (if needed) and load the ONNX cross-encoder model."""

        # --- deferred imports so uv install without heavy deps is possible for testing ---
        from huggingface_hub import snapshot_download
        from onnxruntime import SessionOptions, GraphOptimizationLevel
        from transformers import AutoTokenizer

        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / model_name.replace("/", "--")

        # Download model files if not cached
        if not (model_path / "model_quantized.onnx").exists():
            log.info("Downloading model %s to %s ...", model_name, model_path)
            snapshot_download(
                repo_id=model_name,
                local_dir=model_path,
                local_dir_use_symlinks=False,
                ignore_patterns=["*.md", "*.msgpack", "*.bin", "flax_model*"],
            )
            log.info("Download complete.")

        # Find the ONNX file — try quantized ARM64 first, then fall back
        onnx_dir = model_path / "onnx"
        candidates = [
            onnx_dir / "model_qint8_arm64.onnx",   # quantized ARM64 (23MB — ideal for Pi)
            onnx_dir / "model_quint8_avx2.onnx",    # quantized generic
            onnx_dir / "model_O4.onnx",             # optimized O4
            onnx_dir / "model.onnx",                # full precision (91MB)
        ]
        onnx_path = None
        for c in candidates:
            if c.exists():
                onnx_path = c
                break
        if onnx_path is None:
            raise FileNotFoundError(
                f"No ONNX model found in {onnx_dir}. "
                f"Tried: {[c.name for c in candidates]}"
            )

        # Load tokenizer
        tokenizer = AutoTokenizer.from_pretrained(str(model_path))

        # Configure ONNX Runtime session
        opts = SessionOptions()
        opts.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2  # match Pi CPU cores
        opts.inter_op_num_threads = 1

        ort = _get_ort()
        session = ort.InferenceSession(
            str(onnx_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        input_names = [inp.name for inp in session.get_inputs()]
        output_name = session.get_outputs()[0].name

        log.info(
            "Model loaded: %s (%s, %d inputs, %d params)",
            model_name,
            onnx_path.name,
            len(input_names),
            session.get_modelmeta().custom_metadata_map.get("num_params", "?"),
        )

        return cls(
            session=session,
            tokenizer=tokenizer,
            model_name=model_name,
            input_names=input_names,
            output_name=output_name,
        )

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Score each document by relevance to the query, returns 0-1 floats."""
        if not documents:
            return []

        # Tokenize as (query, document) pairs
        features = self.tokenizer(
            [query] * len(documents),
            documents,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )

        # Build ONNX feed dict
        ort_inputs = {name: features[name].astype(np.int64) for name in self.input_names}
        logits = self.session.run([self.output_name], ort_inputs)[0]

        # Convert logits to 0-1 score via sigmoid
        scores = 1.0 / (1.0 + np.exp(-logits.flatten()))
        return scores.tolist()

    @property
    def is_loaded(self) -> bool:
        return self.session is not None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = fastapi.FastAPI(title="schist-rag", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global model instance — loaded at startup, shared across requests
_reranker: Reranker | None = None


@app.on_event("startup")
async def startup():
    global _reranker
    log.info("Starting schist-rag on %s:%d", HOST, PORT)
    try:
        _reranker = Reranker.load()
        log.info("Reranker ready.")
    except Exception as e:
        log.error("Failed to load reranker: %s", e)
        _reranker = None  # service runs but returns 503 for rerank


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down.")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Query string to rank against")
    documents: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_CANDIDATES,
        description="Candidate documents to rerank",
    )


class RerankResponse(BaseModel):
    scores: list[float]
    model: str = ""
    took_ms: float = 0.0


class EmbedRequest(BaseModel):
    texts: list[str] = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Texts to embed (max 32 per call)",
    )


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str = ""
    dimension: int = 0
    took_ms: float = 0.0


class HealthResponse(BaseModel):
    status: str = "ok"
    model_loaded: bool = False
    model_name: str = ""
    uptime_s: float = 0.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    """Rerank candidate documents by relevance to a query."""
    if _reranker is None or not _reranker.is_loaded:
        raise fastapi.HTTPException(status_code=503, detail="Reranker not loaded")

    t0 = time.time()
    scores = _reranker.rerank(req.query, req.documents)
    took_ms = (time.time() - t0) * 1000

    log.debug("Reranked %d docs in %.1fms", len(req.documents), took_ms)

    return RerankResponse(
        scores=scores,
        model=_reranker.model_name,
        took_ms=round(took_ms, 1),
    )


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    """Compute embedding vectors for input texts.

    NOTE: This is a stub until Phase 3. Currently returns random vectors.
    """
    dim = 384  # MiniLM dimension
    t0 = time.time()
    rng = np.random.default_rng(42)
    embeddings = rng.uniform(-1, 1, (len(req.texts), dim)).tolist()
    took_ms = (time.time() - t0) * 1000

    return EmbedResponse(
        embeddings=embeddings,
        model="stub",
        dimension=dim,
        took_ms=round(took_ms, 1),
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    """Liveness check."""
    return HealthResponse(
        status="ok",
        model_loaded=_reranker is not None and _reranker.is_loaded,
        model_name=_reranker.model_name if _reranker else "",
        uptime_s=round(time.time() - _start_time, 1),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_start_time = time.time()


def main():
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        log_level="info",
        reload=False,
        workers=1,  # single worker — ONNX session is not fork-safe
    )


if __name__ == "__main__":
    # Handle SIGTERM gracefully (for systemd)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
