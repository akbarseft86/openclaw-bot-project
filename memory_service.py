#!/usr/bin/env python3
"""
MemoryService — PRD-05 Phase 1+2: SQLite + Cloudflare Vectorize
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Long-term memory for the OpenClaw multi-agent system.
Provides structured storage (SQLite), deduplication,
redaction, execution logging, memory capture hooks,
and semantic retrieval via Cloudflare Vectorize (RAG).

Usage:
    from memory_service import MemoryService
    mem = MemoryService()
    mem.capture_execution_log(trace_id, user_id, chat_id, intent, agent, status, latency)
    mem.capture_research_run(run_id, trace_id, niche, window, queries, total, quality)
    pack = mem.retrieve(intent, scope, query_text)
"""

import os
import re
import json
import time
import hashlib
import sqlite3
import logging
from typing import Any, Optional
from datetime import datetime, timedelta

log = logging.getLogger("memory_svc")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get("OPENCLAW_MEMORY_DB", "/root/openclaw_memory.db")
SYSTEM_SALT = os.environ.get("OPENCLAW_SALT", "oc_default_salt_v1")

# TTL defaults (days)
TTL_DEFAULTS = {
    "trend_short": 30,
    "trend_long": 180,
    "news_short": 14,
    "ads_long": 180,
    "ops_long": 365,
    "execution_log": 90,
    "research_run": 180,
    "content_output": 365,
}

# Token budget for context packs
MAX_MEMORY_TOKENS = 900
MAX_HITS = 8
MAX_BULLETS_PER_HIT = 2
MAX_KEYWORDS_PER_HIT = 6


# ══════════════════════════════════════════════════════════════════════════════
# REDACTOR — strips secrets from text before storage
# ══════════════════════════════════════════════════════════════════════════════

class Redactor:
    """Detects and strips API keys, JWTs, long base64 strings from text."""

    # Patterns that match common secret formats
    _PATTERNS = [
        # API keys (AIza..., sk-..., Bearer ...)
        (re.compile(r"AIza[A-Za-z0-9_\-]{30,}"), "[REDACTED_API_KEY]"),
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
        (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"), "Bearer [REDACTED]"),
        # JWT tokens (xxx.xxx.xxx)
        (re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
        # Long base64 strings (>40 chars of base64 charset)
        (re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/]{40,}={0,3}(?![A-Za-z0-9])"), "[REDACTED_BASE64]"),
        # Authorization headers
        (re.compile(r"Authorization:\s*\S+", re.IGNORECASE), "Authorization: [REDACTED]"),
        # Cookie headers
        (re.compile(r"Cookie:\s*\S+", re.IGNORECASE), "Cookie: [REDACTED]"),
    ]

    @classmethod
    def redact(cls, text: str) -> str:
        """Apply all redaction patterns to text."""
        if not text:
            return text
        for pattern, replacement in cls._PATTERNS:
            text = pattern.sub(replacement, text)
        return text


# ══════════════════════════════════════════════════════════════════════════════
# DEDUP ENGINE — canonical hashing for deduplication
# ══════════════════════════════════════════════════════════════════════════════

class DedupEngine:
    """Generates deterministic dedup keys using SHA-256."""

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def youtube_key(cls, video_id: str) -> str:
        return cls._sha256(f"yt:{video_id}")

    @classmethod
    def news_key(cls, url: str) -> str:
        canonical = cls._canonicalize_url(url)
        return cls._sha256(f"news:{canonical}")

    @classmethod
    def ads_key(cls, url: str, primary_text: str) -> str:
        canonical = cls._canonicalize_url(url)
        text_hash = cls._sha256(primary_text.strip().lower())
        return cls._sha256(f"ad:{canonical}:{text_hash}")

    @classmethod
    def cluster_key(cls, cluster_name: str, top_keywords: list[str]) -> str:
        name_norm = cluster_name.strip().lower()
        kw_sorted = sorted([k.strip().lower() for k in top_keywords[:12]])
        sig = cls._sha256(f"{name_norm}:{','.join(kw_sorted)}")
        return cls._sha256(f"cluster:{sig}")

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        """Strip tracking params, normalize scheme/host."""
        import urllib.parse
        parsed = urllib.parse.urlparse(url.strip().lower())
        # Strip common tracking params
        params = urllib.parse.parse_qs(parsed.query)
        clean_params = {
            k: v for k, v in params.items()
            if not k.startswith("utm_") and k not in ("ref", "fbclid", "gclid", "mc_cid", "mc_eid")
        }
        clean_query = urllib.parse.urlencode(clean_params, doseq=True)
        canonical = urllib.parse.urlunparse((
            parsed.scheme or "https",
            parsed.netloc,
            parsed.path.rstrip("/"),
            parsed.params,
            clean_query,
            "",  # no fragment
        ))
        return canonical

    @staticmethod
    def hash_user_id(user_id: str | int) -> str:
        """Hash user ID for privacy."""
        raw = f"{user_id}:{SYSTEM_SALT}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE DATABASE LAYER
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA_DDL = """
-- execution_logs: Tracks every component execution (§6.1.1)
CREATE TABLE IF NOT EXISTS execution_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT    NOT NULL,
    user_id     TEXT    NULL,
    chat_id     TEXT    NULL,
    intent      TEXT    NOT NULL,
    agent_name  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    latency_ms  INTEGER NOT NULL,
    error_type  TEXT    NULL,
    error_msg   TEXT    NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_trace_id ON execution_logs(trace_id);
CREATE INDEX IF NOT EXISTS idx_exec_created_at ON execution_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_exec_user_intent ON execution_logs(user_id, intent, created_at);

-- research_runs: Tracks each research run lineage (§6.1.2)
CREATE TABLE IF NOT EXISTS research_runs (
    run_id       TEXT    PRIMARY KEY,
    trace_id     TEXT    NOT NULL,
    niche        TEXT    NOT NULL,
    window_hours INTEGER NOT NULL,
    queries_used TEXT    NOT NULL,
    total_videos INTEGER NOT NULL,
    quality_score REAL   NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_niche_created ON research_runs(niche, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_trace ON research_runs(trace_id);
CREATE INDEX IF NOT EXISTS idx_runs_window ON research_runs(window_hours, created_at);

-- content_outputs: Stores generated content (§6.1.3)
CREATE TABLE IF NOT EXISTS content_outputs (
    output_id    TEXT    PRIMARY KEY,
    trace_id     TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    source_run_id TEXT   NOT NULL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outputs_run_type ON content_outputs(source_run_id, type);
CREATE INDEX IF NOT EXISTS idx_outputs_created ON content_outputs(created_at);

-- tool_registry: Tracks tools and schemas (§6.1.4)
CREATE TABLE IF NOT EXISTS tool_registry (
    tool_name     TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    description   TEXT NOT NULL,
    inputs_schema TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_created ON tool_registry(created_at);

-- document_registry: Canonical URL/document registry (§6.1.5)
CREATE TABLE IF NOT EXISTS document_registry (
    doc_id     TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_hash ON document_registry(hash);
CREATE INDEX IF NOT EXISTS idx_doc_url ON document_registry(url);

-- dedup_registry: Central dedup state (§6.1.6)
CREATE TABLE IF NOT EXISTS dedup_registry (
    dedup_key     TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1,
    source_type   TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    last_trace_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedup_source ON dedup_registry(source_type, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_dedup_ref ON dedup_registry(source_ref);
"""


class MemoryDB:
    """SQLite wrapper with WAL mode and atomic transactions."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_db()

    def _ensure_db(self):
        """Create DB and tables if needed."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode for concurrency
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Create schema
        self._conn.executescript(SCHEMA_DDL)
        self._conn.commit()
        log.info("MemoryDB initialized: %s", self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._ensure_db()
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, params_list)

    def commit(self):
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute query and return list of dicts."""
        cursor = self.execute(sql, params)
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY SERVICE — main public API
# ══════════════════════════════════════════════════════════════════════════════

class MemoryService:
    """
    Central memory service for the OpenClaw multi-agent system.
    Provides:
      - capture_*() hooks for writing after agent execution
      - check_dedup() for duplicate detection
      - retrieve() for context pack retrieval
      - purge_expired() for TTL cleanup
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db = MemoryDB(db_path)
        self.dedup = DedupEngine()
        self.redactor = Redactor()
        # Phase 2: Vectorize (graceful if disabled)
        try:
            from vectorize_client import VectorizeClient, TextChunker
            self.vectorize = VectorizeClient()
            self.chunker = TextChunker()
            self._vec_enabled = self.vectorize.enabled
        except ImportError:
            self.vectorize = None
            self.chunker = None
            self._vec_enabled = False
            log.warning("VectorizeClient not available — semantic RAG disabled")

    # ═══════════════════════════════════════════════════════════════
    # CAPTURE HOOKS — called after agent execution
    # ═══════════════════════════════════════════════════════════════

    def capture_execution_log(
        self,
        trace_id: str,
        user_id: str | int,
        chat_id: str | int,
        intent: str,
        agent_name: str,
        status: str,
        latency_ms: int,
        error_type: str = None,
        error_msg: str = None,
    ):
        """Write an execution log entry (§6.1.1)."""
        now = datetime.utcnow().isoformat() + "Z"
        user_hash = DedupEngine.hash_user_id(user_id) if user_id else None
        chat_hash = DedupEngine.hash_user_id(chat_id) if chat_id else None
        safe_msg = self.redactor.redact(error_msg) if error_msg else None

        self.db.execute(
            """INSERT INTO execution_logs
               (trace_id, user_id, chat_id, intent, agent_name, status, latency_ms, error_type, error_msg, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, user_hash, chat_hash, intent, agent_name, status, latency_ms, error_type, safe_msg, now),
        )
        self.db.commit()
        log.debug("Logged execution: trace=%s agent=%s status=%s %dms", trace_id, agent_name, status, latency_ms)

    def capture_research_run(
        self,
        run_id: str,
        trace_id: str,
        niche: str,
        window_hours: int,
        queries_used: list[str],
        total_videos: int,
        quality_score: float,
    ):
        """Write a research run record (§6.1.2)."""
        now = datetime.utcnow().isoformat() + "Z"
        self.db.execute(
            """INSERT OR REPLACE INTO research_runs
               (run_id, trace_id, niche, window_hours, queries_used, total_videos, quality_score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, trace_id, niche, window_hours, json.dumps(queries_used), total_videos, quality_score, now),
        )
        self.db.commit()
        log.debug("Logged research run: %s niche=%s videos=%d", run_id, niche, total_videos)

    def capture_content_output(
        self,
        output_id: str,
        trace_id: str,
        output_type: str,
        text: str,
        source_run_id: str,
    ):
        """Write a content output record (§6.1.3)."""
        now = datetime.utcnow().isoformat() + "Z"
        safe_text = self.redactor.redact(text)
        self.db.execute(
            """INSERT OR REPLACE INTO content_outputs
               (output_id, trace_id, type, text, source_run_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (output_id, trace_id, output_type, safe_text, source_run_id, now),
        )
        self.db.commit()

    def register_tool(self, tool_name: str, version: str, description: str, inputs_schema: dict):
        """Register a tool in the tool registry (§6.1.4)."""
        now = datetime.utcnow().isoformat() + "Z"
        self.db.execute(
            """INSERT OR REPLACE INTO tool_registry
               (tool_name, version, description, inputs_schema, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tool_name, version, description, json.dumps(inputs_schema), now),
        )
        self.db.commit()

    def register_document(self, doc_id: str, url: str, title: str = None):
        """Register a document/URL in the document registry (§6.1.5)."""
        now = datetime.utcnow().isoformat() + "Z"
        canonical = DedupEngine._canonicalize_url(url)
        url_hash = DedupEngine._sha256(canonical)
        self.db.execute(
            """INSERT OR REPLACE INTO document_registry
               (doc_id, url, title, hash, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, url, title, url_hash, now),
        )
        self.db.commit()

    # ═══════════════════════════════════════════════════════════════
    # DEDUP — check and upsert
    # ═══════════════════════════════════════════════════════════════

    def check_dedup(self, dedup_key: str) -> Optional[dict]:
        """Check if a dedup key exists. Returns record or None."""
        rows = self.db.query(
            "SELECT * FROM dedup_registry WHERE dedup_key = ?", (dedup_key,)
        )
        return rows[0] if rows else None

    def upsert_dedup(
        self,
        dedup_key: str,
        source_type: str,
        source_ref: str,
        trace_id: str,
    ) -> dict:
        """
        Upsert dedup registry (§10.2).
        Returns {"is_new": bool, "count": int, "dedup_key": str}.
        """
        now = datetime.utcnow().isoformat() + "Z"
        existing = self.check_dedup(dedup_key)

        if existing:
            new_count = existing["count"] + 1
            self.db.execute(
                """UPDATE dedup_registry
                   SET last_seen_at = ?, count = ?, last_trace_id = ?
                   WHERE dedup_key = ?""",
                (now, new_count, trace_id, dedup_key),
            )
            self.db.commit()
            return {"is_new": False, "count": new_count, "dedup_key": dedup_key}
        else:
            self.db.execute(
                """INSERT INTO dedup_registry
                   (dedup_key, first_seen_at, last_seen_at, count, source_type, source_ref, last_trace_id)
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (dedup_key, now, now, source_type, source_ref, trace_id),
            )
            self.db.commit()
            return {"is_new": True, "count": 1, "dedup_key": dedup_key}

    def get_do_not_repeat(self, source_type: str = None, limit: int = 50) -> dict:
        """
        Build a do_not_repeat payload from recent dedup entries (§8.1).
        Returns {"video_ids": [], "urls": [], "cluster_signatures": []}.
        """
        result = {"video_ids": [], "urls": [], "cluster_signatures": []}
        query = "SELECT dedup_key, source_type, source_ref FROM dedup_registry"
        params = []
        if source_type:
            query += " WHERE source_type = ?"
            params.append(source_type)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.query(query, tuple(params))
        for row in rows:
            st = row["source_type"]
            ref = row["source_ref"]
            if st == "youtube_video":
                result["video_ids"].append(ref)
            elif st == "news_url":
                result["urls"].append(ref)
            elif st == "trend_cluster":
                result["cluster_signatures"].append(row["dedup_key"])
        return result

    # ═══════════════════════════════════════════════════════════════
    # RETRIEVAL — build context packs (Phase 1: SQLite-only)
    # ═══════════════════════════════════════════════════════════════

    def retrieve(
        self,
        intent: str,
        scope: str = "global",
        niche: str = "",
        user_id: str | int = None,
        time_range_days: int = 30,
    ) -> dict:
        """
        Build a context pack from SQLite data (§8).
        Phase 2 will add Vectorize semantic search here.
        """
        now = datetime.utcnow()
        cutoff = (now - timedelta(days=time_range_days)).isoformat() + "Z"
        user_hash = DedupEngine.hash_user_id(user_id) if user_id else None

        # Get recent research runs for this niche
        memory_hits = []
        if niche:
            runs = self.db.query(
                """SELECT run_id, niche, window_hours, queries_used, total_videos, quality_score, created_at
                   FROM research_runs
                   WHERE niche LIKE ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{niche}%", cutoff, MAX_HITS),
            )
            for run in runs:
                queries = json.loads(run["queries_used"]) if run["queries_used"] else []
                memory_hits.append({
                    "mode": "trend",
                    "title": f"Research: {run['niche']}",
                    "summary_bullets": [
                        f"Window: {run['window_hours']}h, {run['total_videos']} videos found",
                        f"Quality: {run['quality_score']:.2f}",
                    ][:MAX_BULLETS_PER_HIT],
                    "signals": {
                        "keywords": queries[:MAX_KEYWORDS_PER_HIT],
                    },
                    "provenance": {
                        "record_id": run["run_id"],
                        "source": "FunnelTrendResearcher",
                        "source_ref": run["run_id"],
                        "observed_at": run["created_at"],
                    },
                })

        # Build do_not_repeat
        dnr = self.get_do_not_repeat(limit=30)

        # Assemble context pack (§8.1)
        context_pack = {
            "context_pack": {
                "intent": intent,
                "scopes_used": [scope],
                "token_budget": MAX_MEMORY_TOKENS,
                "retrieval_params": {
                    "top_k": MAX_HITS,
                    "source": "sqlite_only",
                    "time_range_days": time_range_days,
                },
                "memory_hits": memory_hits[:MAX_HITS],
                "do_not_repeat": dnr,
            }
        }
        return context_pack

    # ═══════════════════════════════════════════════════════════════
    # PHASE 2: SEMANTIC RETRIEVAL (Vectorize + SQLite two-stage)
    # ═══════════════════════════════════════════════════════════════

    async def vectorize_capture(
        self,
        trace_id: str,
        record_id: str,
        mode: str,
        title: str,
        summary: str,
        signals: list[str] = None,
        entities: dict = None,
        source: str = "",
        source_ref: str = "",
        observed_at: str = "",
        tags: list[str] = None,
        ttl_class: str = "trend_short",
    ) -> dict:
        """
        Embed a memory chunk and store in Vectorize (§5.2 + §5.3).
        Returns upsert result.
        """
        if not self._vec_enabled:
            return {"success": False, "reason": "vectorize_disabled"}

        from vectorize_client import TextChunker

        # Build structured embedding input (§5.2.3)
        embed_text = TextChunker.build_embedding_input(
            doc_type="summary",
            mode=mode,
            title=title,
            summary=summary,
            signals=signals,
            entities=entities,
            observed_at=observed_at,
        )

        # Build metadata (§5.3)
        now = datetime.utcnow().isoformat() + "Z"
        metadata = {
            "record_id": record_id,
            "mode": mode,
            "scope": "global",
            "source": source,
            "source_ref": source_ref,
            "topic": title[:100],
            "tags": (tags or [])[:10],
            "trace_id": trace_id,
            "ttl_class": ttl_class,
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }

        return await self.vectorize.embed_and_store(record_id, embed_text, metadata)

    async def retrieve_semantic(
        self,
        intent: str,
        query_text: str,
        scope: str = "global",
        niche: str = "",
        user_id: str | int = None,
        time_range_days: int = 30,
        top_k: int = MAX_HITS,
        threshold: float = 0.78,
    ) -> dict:
        """
        Two-stage retrieval (§5.4.3):
        1. Vectorize semantic search with filters → candidate chunks
        2. SQLite enrichment by record_id → full context + dedup stats
        Then compress into context pack.
        """
        # Intent → mode mapping (§9.2)
        mode_map = {
            "trend_research": ["trend"],
            "trend_full_report": ["trend", "ops"],
            "trend_fixed_window": ["trend"],
            "ads_library_scan": ["ads", "ops"],
            "news_scan": ["news"],
        }
        modes = mode_map.get(intent, ["trend"])

        memory_hits = []

        # Stage 1: Vectorize semantic search (if enabled)
        if self._vec_enabled and query_text:
            filter_meta = {"mode": {"$in": modes}}
            matches = await self.vectorize.semantic_search(
                query_text=query_text,
                top_k=top_k,
                threshold=threshold,
                filter_metadata=filter_meta,
            )
            for match in matches:
                meta = match.get("metadata", {})
                score = match.get("score", 0)
                record_id = meta.get("record_id", "")

                # Stage 2: SQLite enrichment (optional)
                dedup_info = None
                if record_id:
                    runs = self.db.query(
                        "SELECT niche, quality_score FROM research_runs WHERE run_id = ?",
                        (record_id,)
                    )
                    if runs:
                        dedup_entries = self.db.query(
                            "SELECT dedup_key, count, last_seen_at FROM dedup_registry WHERE last_trace_id = ?",
                            (meta.get("trace_id", ""),)
                        )
                        if dedup_entries:
                            dedup_info = {
                                "dedup_key": dedup_entries[0]["dedup_key"],
                                "count": dedup_entries[0]["count"],
                                "last_seen_at": dedup_entries[0]["last_seen_at"],
                            }

                # Priority scoring (§9.4)
                recency_weight = self._recency_score(meta.get("created_at", ""), modes[0])
                dedup_penalty = 0.0
                if dedup_info and dedup_info["count"] > 2:
                    dedup_penalty = min(0.15, dedup_info["count"] * 0.03)
                priority = 0.50 * score + 0.35 * recency_weight - 0.15 * dedup_penalty

                hit = {
                    "mode": meta.get("mode", "trend"),
                    "title": meta.get("topic", "Unknown"),
                    "summary_bullets": [meta.get("chunk_text", "")[:200]][:MAX_BULLETS_PER_HIT],
                    "signals": {"keywords": meta.get("tags", [])[:MAX_KEYWORDS_PER_HIT]},
                    "similarity": round(score, 3),
                    "priority": round(priority, 3),
                    "provenance": {
                        "record_id": record_id,
                        "source": meta.get("source", ""),
                        "source_ref": meta.get("source_ref", ""),
                        "observed_at": meta.get("created_at", ""),
                        "trace_id": meta.get("trace_id", ""),
                    },
                }
                if dedup_info:
                    hit["dedup"] = dedup_info
                memory_hits.append(hit)

            # Sort by priority descending
            memory_hits.sort(key=lambda h: -h.get("priority", 0))

        # Fallback: add SQLite-only hits if vectorize didn't return enough
        if len(memory_hits) < MAX_HITS and niche:
            sqlite_pack = self.retrieve(intent, scope, niche, user_id, time_range_days)
            sqlite_hits = sqlite_pack.get("context_pack", {}).get("memory_hits", [])
            existing_ids = {h["provenance"]["record_id"] for h in memory_hits}
            for sh in sqlite_hits:
                if sh["provenance"]["record_id"] not in existing_ids:
                    sh["similarity"] = 0.0
                    sh["priority"] = 0.1
                    memory_hits.append(sh)
                if len(memory_hits) >= MAX_HITS:
                    break

        # Build do_not_repeat
        dnr = self.get_do_not_repeat(limit=30)

        # Compress to token budget (§9.3)
        if len(memory_hits) > MAX_HITS:
            memory_hits = memory_hits[:MAX_HITS]

        return {
            "context_pack": {
                "intent": intent,
                "scopes_used": [scope],
                "token_budget": MAX_MEMORY_TOKENS,
                "retrieval_params": {
                    "top_k": top_k,
                    "similarity_threshold": threshold,
                    "source": "vectorize+sqlite" if self._vec_enabled else "sqlite_only",
                    "time_range_days": time_range_days,
                },
                "memory_hits": memory_hits,
                "do_not_repeat": dnr,
            }
        }

    @staticmethod
    def _recency_score(created_at: str, mode: str) -> float:
        """Calculate recency weight with mode-specific decay (§9.4)."""
        if not created_at:
            return 0.0
        try:
            created = datetime.fromisoformat(created_at.rstrip("Z"))
            age_days = (datetime.utcnow() - created).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.0

        # Mode-specific half-life (days)
        half_lives = {
            "news": 7,
            "trend": 30,
            "ads": 90,
            "ops": 180,
        }
        half_life = half_lives.get(mode, 30)
        import math
        return math.exp(-0.693 * age_days / half_life)  # exponential decay

    async def capture_agent_result_async(
        self,
        trace_id: str,
        user_id: str | int,
        chat_id: str | int,
        intent: str,
        agent_name: str,
        status: str,
        latency_ms: int,
        agent_output: dict = None,
        error_type: str = None,
        error_msg: str = None,
    ) -> dict:
        """
        Full async capture hook — SQLite + Vectorize.
        Calls synchronous SQLite capture first, then async vectorize capture.
        """
        # 1. SQLite capture (synchronous)
        summary = self.capture_agent_result(
            trace_id, user_id, chat_id, intent, agent_name, status, latency_ms,
            agent_output, error_type, error_msg,
        )

        # 2. Vectorize capture (async, Phase 2)
        summary["vectorized"] = 0
        if self._vec_enabled and status == "success" and agent_output and agent_name == "FunnelTrendResearcher":
            clusters = agent_output.get("trend_clusters", [])
            meta = agent_output.get("meta", {})
            run_id = meta.get("request_id", trace_id)

            for i, cluster in enumerate(clusters):
                cluster_name = cluster.get("cluster_name", f"cluster_{i}")
                keywords = cluster.get("key_signals", [])
                summary_text = cluster.get("analysis_summary", "")
                if not summary_text:
                    # Build from available data
                    summary_text = f"Cluster: {cluster_name}. Signals: {', '.join(keywords[:8])}"

                try:
                    result = await self.vectorize_capture(
                        trace_id=trace_id,
                        record_id=f"{run_id}_cluster_{i}",
                        mode="trend",
                        title=cluster_name,
                        summary=summary_text,
                        signals=keywords,
                        entities=cluster.get("entities", {}),
                        source=agent_name,
                        source_ref=run_id,
                        observed_at=meta.get("created_at", ""),
                        tags=keywords[:6],
                        ttl_class="trend_short",
                    )
                    if result.get("success"):
                        summary["vectorized"] += 1
                except Exception as e:
                    log.warning("Vectorize capture failed for cluster %d: %s", i, e)

        return summary

    # ═══════════════════════════════════════════════════════════════
    # STATS & OBSERVABILITY
    # ═══════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        """Return observability metrics (§11.3)."""
        stats = {}
        for table in ["execution_logs", "research_runs", "content_outputs", "dedup_registry", "document_registry"]:
            rows = self.db.query(f"SELECT COUNT(*) as cnt FROM {table}")
            stats[f"{table}_count"] = rows[0]["cnt"] if rows else 0

        # Dedup rate
        total_dedup = stats.get("dedup_registry_count", 0)
        if total_dedup > 0:
            repeat_rows = self.db.query("SELECT COUNT(*) as cnt FROM dedup_registry WHERE count > 1")
            stats["dedup_repeat_rate"] = (repeat_rows[0]["cnt"] / total_dedup) if repeat_rows else 0
        else:
            stats["dedup_repeat_rate"] = 0.0

        # Recent execution summary
        recent = self.db.query(
            "SELECT status, COUNT(*) as cnt FROM execution_logs WHERE created_at >= ? GROUP BY status",
            ((datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z",),
        )
        stats["last_24h_executions"] = {row["status"]: row["cnt"] for row in recent}

        return stats

    # ═══════════════════════════════════════════════════════════════
    # TTL CLEANUP
    # ═══════════════════════════════════════════════════════════════

    def purge_expired(self) -> dict:
        """
        Remove expired records per TTL policy (§5.6).
        Returns counts of purged rows per table.
        """
        now = datetime.utcnow()
        purged = {}

        # execution_logs: 90 days
        cutoff = (now - timedelta(days=TTL_DEFAULTS["execution_log"])).isoformat() + "Z"
        cursor = self.db.execute("DELETE FROM execution_logs WHERE created_at < ?", (cutoff,))
        purged["execution_logs"] = cursor.rowcount

        # research_runs: 180 days
        cutoff = (now - timedelta(days=TTL_DEFAULTS["research_run"])).isoformat() + "Z"
        cursor = self.db.execute("DELETE FROM research_runs WHERE created_at < ?", (cutoff,))
        purged["research_runs"] = cursor.rowcount

        # content_outputs: 365 days
        cutoff = (now - timedelta(days=TTL_DEFAULTS["content_output"])).isoformat() + "Z"
        cursor = self.db.execute("DELETE FROM content_outputs WHERE created_at < ?", (cutoff,))
        purged["content_outputs"] = cursor.rowcount

        self.db.commit()
        log.info("Purge completed: %s", purged)
        return purged

    # ═══════════════════════════════════════════════════════════════
    # COMPOUND CAPTURE — full agent result processing
    # ═══════════════════════════════════════════════════════════════

    def capture_agent_result(
        self,
        trace_id: str,
        user_id: str | int,
        chat_id: str | int,
        intent: str,
        agent_name: str,
        status: str,
        latency_ms: int,
        agent_output: dict = None,
        error_type: str = None,
        error_msg: str = None,
    ) -> dict:
        """
        Full memory capture hook — called by BMADOrchestrator after execution.
        1. Logs execution
        2. Dedup-checks YouTube videos found
        3. Stores research run if applicable
        4. Stores content outputs if applicable
        Returns capture summary.
        """
        summary = {"logged": False, "dedup_results": [], "run_stored": False, "outputs_stored": 0}

        # 1. Always log execution
        self.capture_execution_log(
            trace_id, user_id, chat_id, intent, agent_name, status, latency_ms, error_type, error_msg
        )
        summary["logged"] = True

        # 2. Process agent output (if success and FTR)
        if status == "success" and agent_output and agent_name == "FunnelTrendResearcher":
            meta = agent_output.get("meta", {})
            clusters = agent_output.get("trend_clusters", [])

            # Dedup YouTube videos
            for cluster in clusters:
                for video in cluster.get("source_videos", []):
                    vid = video.get("video_id", "")
                    if vid:
                        dk = self.dedup.youtube_key(vid)
                        result = self.upsert_dedup(dk, "youtube_video", vid, trace_id)
                        summary["dedup_results"].append(result)

                # Dedup cluster itself
                cluster_name = cluster.get("cluster_name", "")
                keywords = cluster.get("key_signals", [])
                if cluster_name:
                    ck = self.dedup.cluster_key(cluster_name, keywords)
                    self.upsert_dedup(ck, "trend_cluster", cluster_name, trace_id)

            # Store research run
            run_id = meta.get("request_id", trace_id)
            niche = meta.get("niche", "unknown")
            window = meta.get("window_hours", 0)
            queries = meta.get("queries_used", [])
            total_vids = meta.get("total_videos_analyzed", 0)
            quality = meta.get("overall_quality_score", 0.0)

            self.capture_research_run(run_id, trace_id, niche, window, queries, total_vids, quality)
            summary["run_stored"] = True

            # Store content outputs (IG hooks, carousels, etc.)
            for cluster in clusters:
                content = cluster.get("content_outputs", {})
                for ctype, ctext in content.items():
                    if ctext and isinstance(ctext, str):
                        oid = f"{run_id}_{ctype}"
                        self.capture_content_output(oid, trace_id, ctype, ctext, run_id)
                        summary["outputs_stored"] += 1

        log.info(
            "Memory capture: trace=%s agent=%s logged=%s dedup=%d run=%s outputs=%d",
            trace_id, agent_name, summary["logged"], len(summary["dedup_results"]),
            summary["run_stored"], summary["outputs_stored"],
        )
        return summary
