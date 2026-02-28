#!/usr/bin/env python3
"""
VectorizeClient — PRD-05 Phase 2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Thin HTTP client wrapping Cloudflare Vectorize REST API
and Workers AI embedding endpoint.

Env vars required:
    CLOUDFLARE_ACCOUNT_ID   — Cloudflare account ID
    CLOUDFLARE_API_TOKEN    — API token with Vectorize + Workers AI permissions

Usage:
    from vectorize_client import VectorizeClient
    vc = VectorizeClient()
    vc.upsert("index_name", vectors=[{"id": "1", "values": [...], "metadata": {...}}])
    results = vc.query("index_name", vector=[...], top_k=8, filter={"mode": "trend"})
"""

import os
import json
import time
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("vectorize")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CF_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CF_BASE_URL = "https://api.cloudflare.com/client/v4"

# Default embedding model via Workers AI
EMBEDDING_MODEL = os.environ.get(
    "CF_EMBEDDING_MODEL", "@cf/baai/bge-base-en-v1.5"
)
EMBEDDING_DIMENSIONS = 768  # bge-base-en-v1.5 output size

# Index naming convention (§5.1)
DEFAULT_ENV = os.environ.get("OPENCLAW_ENV", "prod")
DEFAULT_INDEX = f"oc_mem_{DEFAULT_ENV}_global_trend_v1"

# Retrieval defaults (§5.4)
DEFAULT_TOP_K = 8
DEFAULT_THRESHOLD = 0.78

# Chunking (§5.2.2)
MAX_CHUNK_TOKENS = 350
CHUNK_OVERLAP_TOKENS = 40


# ══════════════════════════════════════════════════════════════════════════════
# CHUNKER — splits text into embeddable chunks
# ══════════════════════════════════════════════════════════════════════════════

class TextChunker:
    """Chunks text by approximate token count with overlap."""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate (~4 chars per token for English)."""
        return max(1, len(text) // 4)

    @classmethod
    def chunk(
        cls,
        text: str,
        max_tokens: int = MAX_CHUNK_TOKENS,
        overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
    ) -> list[str]:
        """Split text into chunks with overlap."""
        if not text:
            return []
        if cls.estimate_tokens(text) <= max_tokens:
            return [text.strip()]

        # Split by paragraphs first, then sentences
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            candidate = f"{current_chunk}\n\n{para}".strip() if current_chunk else para
            if cls.estimate_tokens(candidate) <= max_tokens:
                current_chunk = candidate
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    # Overlap: keep tail of previous chunk
                    overlap_chars = overlap_tokens * 4
                    tail = current_chunk[-overlap_chars:] if len(current_chunk) > overlap_chars else ""
                    current_chunk = f"{tail}\n\n{para}".strip() if tail else para
                else:
                    # Single paragraph exceeds limit — split by sentences
                    sentences = para.replace(". ", ".\n").split("\n")
                    for sent in sentences:
                        sent = sent.strip()
                        if not sent:
                            continue
                        test = f"{current_chunk} {sent}".strip() if current_chunk else sent
                        if cls.estimate_tokens(test) <= max_tokens:
                            current_chunk = test
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = sent

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    @classmethod
    def build_embedding_input(
        cls,
        doc_type: str,
        mode: str,
        title: str,
        summary: str,
        signals: list[str] = None,
        entities: dict = None,
        observed_at: str = "",
    ) -> str:
        """Build structured embedding input per §5.2.3."""
        parts = [
            f"<type>: {mode}/{doc_type}",
            f"<title>: {title}",
            f"<summary>: {summary}",
        ]
        if signals:
            parts.append(f"<signals>: {', '.join(signals[:12])}")
        if entities:
            ent_parts = []
            for k, v in entities.items():
                if isinstance(v, list):
                    ent_parts.append(f"{k}={','.join(v[:5])}")
                else:
                    ent_parts.append(f"{k}={v}")
            parts.append(f"<entities>: {', '.join(ent_parts)}")
        if observed_at:
            parts.append(f"<time>: {observed_at}")
        return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# VECTORIZE CLIENT — Cloudflare API wrapper
# ══════════════════════════════════════════════════════════════════════════════

class VectorizeClient:
    """
    HTTP client for Cloudflare Vectorize + Workers AI embeddings.
    Gracefully degrades: if credentials are missing, all operations return
    empty results instead of crashing.
    """

    def __init__(
        self,
        account_id: str = CF_ACCOUNT_ID,
        api_token: str = CF_API_TOKEN,
        default_index: str = DEFAULT_INDEX,
    ):
        self.account_id = account_id
        self.api_token = api_token
        self.default_index = default_index
        self.enabled = bool(account_id and api_token)

        if not self.enabled:
            log.warning(
                "VectorizeClient disabled: CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN not set. "
                "Set env vars to enable semantic RAG."
            )

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{CF_BASE_URL}/accounts/{self.account_id}/{path}"

    # ═══════════════════════════════════════════════════════════════
    # EMBEDDINGS via Workers AI
    # ═══════════════════════════════════════════════════════════════

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings via Cloudflare Workers AI.
        Returns list of float vectors (one per input text).
        """
        if not self.enabled:
            return []
        if not texts:
            return []

        url = self._url(f"ai/run/{EMBEDDING_MODEL}")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    headers=self._headers,
                    json={"text": texts},
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result", {})
                # Workers AI returns {"result": {"data": [[...], [...]]}}
                vectors = result.get("data", [])
                if len(vectors) != len(texts):
                    log.warning("Embedding count mismatch: sent %d, got %d", len(texts), len(vectors))
                return vectors
        except Exception as e:
            log.error("Embedding failed: %s", e)
            return []

    # ═══════════════════════════════════════════════════════════════
    # VECTORIZE INDEX OPERATIONS
    # ═══════════════════════════════════════════════════════════════

    async def upsert(
        self,
        vectors: list[dict],
        index_name: str = None,
    ) -> dict:
        """
        Upsert vectors into a Vectorize index.
        Each vector: {"id": str, "values": [float], "metadata": dict}
        """
        if not self.enabled:
            return {"success": False, "reason": "disabled"}
        if not vectors:
            return {"success": True, "count": 0}

        idx = index_name or self.default_index
        url = self._url(f"vectorize/v2/indexes/{idx}/upsert")

        # Vectorize expects NDJSON format for upsert
        ndjson = "\n".join(json.dumps(v) for v in vectors)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/x-ndjson",
                    },
                    content=ndjson,
                )
                resp.raise_for_status()
                data = resp.json()
                count = data.get("result", {}).get("mutationId", "")
                log.info("Vectorize upsert: index=%s ids=%d mutation=%s", idx, len(vectors), count)
                return {"success": True, "count": len(vectors), "mutation_id": count}
        except Exception as e:
            log.error("Vectorize upsert failed: %s", e)
            return {"success": False, "error": str(e)}

    async def query(
        self,
        vector: list[float],
        top_k: int = DEFAULT_TOP_K,
        filter_metadata: dict = None,
        index_name: str = None,
    ) -> list[dict]:
        """
        Query Vectorize for similar vectors.
        Returns list of {"id": str, "score": float, "metadata": dict}.
        """
        if not self.enabled:
            return []

        idx = index_name or self.default_index
        url = self._url(f"vectorize/v2/indexes/{idx}/query")

        payload = {
            "vector": vector,
            "topK": top_k,
            "returnMetadata": "all",
        }
        if filter_metadata:
            payload["filter"] = filter_metadata

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                matches = data.get("result", {}).get("matches", [])
                log.info("Vectorize query: index=%s top_k=%d matches=%d", idx, top_k, len(matches))
                return matches
        except Exception as e:
            log.error("Vectorize query failed: %s", e)
            return []

    async def delete_by_ids(
        self,
        ids: list[str],
        index_name: str = None,
    ) -> dict:
        """Delete vectors by ID."""
        if not self.enabled:
            return {"success": False, "reason": "disabled"}

        idx = index_name or self.default_index
        url = self._url(f"vectorize/v2/indexes/{idx}/delete-by-ids")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    headers=self._headers,
                    json={"ids": ids},
                )
                resp.raise_for_status()
                data = resp.json()
                log.info("Vectorize delete: index=%s ids=%d", idx, len(ids))
                return {"success": True, "count": len(ids)}
        except Exception as e:
            log.error("Vectorize delete failed: %s", e)
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════════════════════════
    # HIGH-LEVEL: EMBED + STORE
    # ═══════════════════════════════════════════════════════════════

    async def embed_and_store(
        self,
        record_id: str,
        text: str,
        metadata: dict,
        index_name: str = None,
    ) -> dict:
        """
        End-to-end: chunk text → embed chunks → upsert vectors.
        Returns summary with chunk count and success status.
        """
        if not self.enabled:
            return {"success": False, "reason": "disabled", "chunks": 0}

        chunks = TextChunker.chunk(text)
        if not chunks:
            return {"success": True, "chunks": 0}

        # Embed all chunks
        embeddings = await self.embed(chunks)
        if len(embeddings) != len(chunks):
            log.warning("Partial embedding: %d/%d chunks got vectors", len(embeddings), len(chunks))
            # Use only paired ones
            paired = min(len(chunks), len(embeddings))
            chunks = chunks[:paired]
            embeddings = embeddings[:paired]

        if not embeddings:
            return {"success": False, "reason": "embedding_failed", "chunks": 0}

        # Build vector records
        vectors = []
        for i, (chunk, vec) in enumerate(zip(chunks, embeddings)):
            vec_id = f"{record_id}_c{i}"
            vec_meta = {**metadata, "chunk_index": i, "chunk_text": chunk[:200]}
            vectors.append({
                "id": vec_id,
                "values": vec,
                "metadata": vec_meta,
            })

        result = await self.upsert(vectors, index_name)
        result["chunks"] = len(vectors)
        return result

    async def semantic_search(
        self,
        query_text: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_THRESHOLD,
        filter_metadata: dict = None,
        index_name: str = None,
    ) -> list[dict]:
        """
        End-to-end: embed query → search Vectorize → filter by threshold.
        Returns list of matches above similarity threshold.
        """
        if not self.enabled:
            return []

        # Embed the query
        vectors = await self.embed([query_text])
        if not vectors:
            return []

        query_vec = vectors[0]
        matches = await self.query(query_vec, top_k=top_k, filter_metadata=filter_metadata, index_name=index_name)

        # Filter by threshold
        filtered = [m for m in matches if m.get("score", 0) >= threshold]
        log.info("Semantic search: query=%s... top_k=%d above_threshold=%d", query_text[:40], top_k, len(filtered))
        return filtered
