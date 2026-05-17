# schist-rag

Lightweight RAG companion service for the schist MCP server.
Provides cross-encoder reranking and embedding via ONNX Runtime on ARM64 CPU.

## Architecture

```
schist MCP (Node.js) → localhost:8788 → schist-rag (Python, FastAPI)
                                           ├── /rerank  (cross-encoder scoring)
                                           ├── /embed   (text embeddings)
                                           └── /health  (liveness)
```

## Setup

```bash
# Install dependencies (uv-managed)
cd rag
uv pip install -r requirements.txt

# Test: start the service
uv run python server.py

# In another terminal:
curl http://127.0.0.1:8788/health
# → {"status":"ok","model_loaded":true,"model_name":"cross-encoder/ms-marco-MiniLM-L-6-v2",...}
```

## Systemd

```bash
sudo cp schist-rag.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now schist-rag
```

## Endpoints

### POST /rerank

Rerank candidate documents by relevance to a query.

```json
{
  "query": "How does RLS work in Supabase?",
  "documents": [
    "Row Level Security in Supabase...",
    "Database migrations are handled by..."
  ]
}
```

Returns scores 0-1 for each document.

### POST /embed

Compute embeddings for text (Phase 3 — currently a stub).

### GET /health

Liveness + model status.
