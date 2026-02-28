#!/usr/bin/env python3
"""
BMADOrchestrator — PRD-04
━━━━━━━━━━━━━━━━━━━━━━━━━
Central Multi-Agent Decision Engine (Routing + Flow Control + Context).
Receives structured payload from TelegramTrendBotRouter, classifies intent,
routes to the correct agent (FunnelTrendResearcher), handles retries/timeouts,
and enforces strict JSON boundaries.
"""

import time
import uuid
import json
import logging
import asyncio
from typing import Any, Dict, Optional

# Assume FunnelTrendResearcher is available to be instantiated
from funnel_trend_researcher import FunnelTrendResearcher
from memory_service import MemoryService

log = logging.getLogger("bmad_orch")

# ══════════════════════════════════════════════════════════════════════════════
# INTENT CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════

class IntentClassifier:
    """Deterministic intent mapping from router payloads."""

    @staticmethod
    def classify(router_payload: Dict[str, Any]) -> str:
        cmd = router_payload.get("command", "")
        if cmd == "/help" or cmd == "/trend_help":
            return "help"
        elif cmd == "/trend_full":
            return "trend_full_report"
        elif cmd == "/trend_window":
            return "trend_fixed_window"
        elif cmd == "/trend":
            return "trend_research"
        return "error.invalid_intent"


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CONTEXT STORE
# ══════════════════════════════════════════════════════════════════════════════

class SessionStore:
    """Lightweight in-memory context store with sliding TTL (default 7 days)."""

    def __init__(self, ttl_seconds: int = 7 * 24 * 3600):
        self.ttl = ttl_seconds
        self._store: Dict[str, Dict[str, Any]] = {}

    def get_context(self, session_id: str) -> Dict[str, Any]:
        self._cleanup()
        entry = self._store.get(session_id)
        if entry:
            entry["expires_at"] = time.time() + self.ttl
            return entry["data"]
        return {}

    def set_context(self, session_id: str, data: Dict[str, Any]):
        self._cleanup()
        self._store[session_id] = {
            "expires_at": time.time() + self.ttl,
            "data": data
        }

    def update_context(self, session_id: str, updates: Dict[str, Any]):
        ctx = self.get_context(session_id)
        ctx.update(updates)
        self.set_context(session_id, ctx)

    def _cleanup(self):
        now = time.time()
        expired = [sid for sid, entry in self._store.items() if entry["expires_at"] < now]
        for sid in expired:
            del self._store[sid]


# ══════════════════════════════════════════════════════════════════════════════
# AGENT WRAPPER & REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class AgentError(Exception):
    """Exception raised for normalized agent/tool errors."""
    def __init__(self, type_: str, message: str, retryable: bool):
        self.type = type_
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class FunnelTrendResearcherWrapper:
    """Wraps FunnelTrendResearcher to match the generic Agent Interface Contract."""

    def __init__(self, config: Dict[str, Any]):
        self.youtube_api_key = config.get("youtube_api_key")
        self.ai_api_url = config.get("ai_api_url")
        self.ai_api_key = config.get("ai_api_key")
        self.ai_model = config.get("ai_model")

    async def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Payload contains:
        - input: agent-specific input
        - context: trace_id, session_id, etc.
        - constraints: deadline_ms, etc.
        """
        agent_input = payload.get("input", {})
        
        agent = FunnelTrendResearcher(
            youtube_api_key=self.youtube_api_key,
            ai_api_url=self.ai_api_url,
            ai_api_key=self.ai_api_key,
            ai_model=self.ai_model,
        )

        start_time = time.monotonic()
        try:
            # We don't enforce timeout here, Orchestrator wraps this with asyncio.wait_for
            result = await agent.research(agent_input)
            
            # Catch known agent-level schema errors immediately
            status = result.get("meta", {}).get("status", "unknown")
            if status == "error":
                errs = result.get("meta", {}).get("errors", [])
                if errs:
                    code = errs[0].get("code", "UNKNOWN_ERROR")
                    msg = errs[0].get("message", "Agent reported error")
                    retryable = errs[0].get("retryable", False)
                    # Translate FTR errors to standardized types
                    if code in ("QUOTA_EXCEEDED", "RATE_LIMITED"):
                        err_type = "quota_exceeded"
                    elif code == "INVALID_KEY":
                        err_type = "invalid_key"
                    elif code == "NO_RESULTS":
                        err_type = "no_results"
                    else:
                        err_type = "internal_error"
                    raise AgentError(err_type, msg, retryable)
                
            return {
                "agent_name": "FunnelTrendResearcher",
                "output": result,
                "status": "success",
                "execution_time_ms": int((time.monotonic() - start_time) * 1000)
            }
        except AgentError:
            raise
        except Exception as e:
            log.exception("FunnelTrendResearcher internal failure")
            raise AgentError("internal_error", str(e), retryable=False)


class AgentRegistry:
    """Stores available agents and their timeout/retry configurations."""
    def __init__(self):
        self._agents = {}

    def register(self, agent_name: str, wrapper_instance: Any, default_timeout_s: float):
        self._agents[agent_name] = {
            "instance": wrapper_instance,
            "timeout_s": default_timeout_s
        }

    def get(self, agent_name: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(agent_name)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

class BMADOrchestrator:
    """
    Central decision engine. Routes requests, handles retries/timeouts,
    and returns standardized JSON responses.
    """

    def __init__(self, config: Dict[str, Any]):
        self.session_store = SessionStore()
        self.registry = AgentRegistry()
        
        # Register available sub-agents
        ftr_wrapper = FunnelTrendResearcherWrapper(config)
        self.registry.register("FunnelTrendResearcher", ftr_wrapper, default_timeout_s=15.0)

        # Initialize Memory Service (PRD-05)
        self.memory = MemoryService()

    async def execute(self, router_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entry point for router.
        router_payload example:
        {
          "command": "/trend",
          "args": {"niche": "abc", "hours": null},
          "user_id": 123,
          "chat_id": 456
        }
        """
        start_time = time.monotonic()
        trace_id = str(uuid.uuid4())[:8]
        request_id = router_payload.get("request_id", f"req-{trace_id}")
        
        chat_id = router_payload.get("chat_id", "0")
        user_id = router_payload.get("user_id", "0")
        session_id = f"{chat_id}_{user_id}"

        # 1. Classify Intent
        intent = IntentClassifier.classify(router_payload)
        log.info(f"Orchestrator [{trace_id}]: intent='{intent}' req='{request_id}'")

        if intent.startswith("error."):
            return self._build_error_response(trace_id, intent, "invalid_input", "Unknown or invalid command", False)

        if intent == "help":
            # For now, orchestrator just tells router to handle it
            return self._build_success_response(trace_id, intent, {"action": "show_help"})

        # 2. Build Agent Input Payload (Routing Logic)
        agent_name = "FunnelTrendResearcher"
        args = router_payload.get("args", {})
        niche = args.get("niche", "")
        
        agent_input = {
            "request_id": request_id,
            "niches": self._map_niche_to_ftr(niche),
        }

        # Handle window logic based on intent
        if intent == "trend_fixed_window":
            hours = args.get("hours")
            if hours is None or hours < 6 or hours > 720:
                return self._build_error_response(trace_id, intent, "invalid_input", "Invalid window hours", False)
            agent_input["adaptive_window"] = {
                "primary_hours": hours,
                "fallback_days_1": 0,
                "fallback_days_2": 0,
                "min_clusters_required": 1,
                "min_quality_threshold": 0.0,
            }

        # Incorporate session history if stateful mode
        session_ctx = self.session_store.get_context(session_id)
        if session_ctx:
            agent_input["prior_history"] = session_ctx.get("history", [])

        # Build Standard Invocation Envelope
        invocation = {
            "agent_name": agent_name,
            "input": agent_input,
            "context": {
                "trace_id": trace_id,
                "session_id": session_id,
                "user_id": str(user_id),
                "intent": intent,
                "return_full_json": (intent == "trend_full_report")
            },
            "constraints": {
                "max_retries": 2
            }
        }

        # 2b. Retrieve memory context pack (PRD-05 §8 — Phase 2: semantic)
        try:
            context_pack = await self.memory.retrieve_semantic(
                intent=intent, query_text=niche, scope="global",
                niche=niche, user_id=user_id,
            )
            invocation["context"]["memory"] = context_pack
            hits = len(context_pack.get('context_pack',{}).get('memory_hits',[]))
            src = context_pack.get('context_pack',{}).get('retrieval_params',{}).get('source','?')
            log.info(f"Orchestrator [{trace_id}]: injected context_pack ({hits} hits, source={src})")
        except Exception as e:
            log.warning(f"Orchestrator [{trace_id}]: memory retrieval failed (non-fatal): {e}")
            # Fallback to SQLite-only sync retrieve
            try:
                context_pack = self.memory.retrieve(intent=intent, scope="global", niche=niche, user_id=user_id)
                invocation["context"]["memory"] = context_pack
            except Exception:
                pass

        # 3. Execute with Retries
        agent_def = self.registry.get(agent_name)
        if not agent_def:
            return self._build_error_response(trace_id, intent, "internal_error", f"Agent {agent_name} not found", False)

        try:
            agent_result = await self._execute_with_retry(
                agent_def["instance"],
                invocation,
                timeout_s=agent_def["timeout_s"],
                max_retries=invocation["constraints"]["max_retries"]
            )
        except AgentError as e:
            err_ms = int((time.monotonic() - start_time) * 1000)
            log.warning(f"Orchestrator [{trace_id}]: AgentError {e.type} - {e.message}")
            try:
                self.memory.capture_execution_log(
                    trace_id, user_id, chat_id, intent, agent_name, "error", err_ms, e.type, e.message
                )
            except Exception:
                pass
            return self._build_error_response(trace_id, intent, e.type, e.message, e.retryable, source_agent=agent_name)
        except asyncio.TimeoutError:
            err_ms = int((time.monotonic() - start_time) * 1000)
            log.warning(f"Orchestrator [{trace_id}]: Timeout execution {agent_name}")
            try:
                self.memory.capture_execution_log(
                    trace_id, user_id, chat_id, intent, agent_name, "error", err_ms, "timeout", "Agent timed out"
                )
            except Exception:
                pass
            return self._build_error_response(trace_id, intent, "timeout", "Agent execution timed out", retryable=True, source_agent=agent_name)
        except Exception as e:
            err_ms = int((time.monotonic() - start_time) * 1000)
            log.exception(f"Orchestrator [{trace_id}]: Unhandled exception")
            try:
                self.memory.capture_execution_log(
                    trace_id, user_id, chat_id, intent, agent_name, "error", err_ms, "internal_error", str(e)
                )
            except Exception:
                pass
            return self._build_error_response(trace_id, intent, "internal_error", str(e), False, source_agent=agent_name)

        # 4. JSON Schema Validation
        if not self._validate_json_schema(agent_result):
            return self._build_error_response(trace_id, intent, "schema_mismatch", "Invalid JSON structure from agent", False, source_agent=agent_name)

        # 5. Update Session Context
        # (Example: save the niche we just searched)
        history = session_ctx.get("history", [])
        history.append({"niche": niche, "ts": time.time()})
        self.session_store.update_context(session_id, {"history": history[-5:]}) # keep last 5

        # 6. Capture to Memory (PRD-05 — Phase 2: async SQLite + Vectorize)
        total_ms = int((time.monotonic() - start_time) * 1000)
        try:
            await self.memory.capture_agent_result_async(
                trace_id=trace_id,
                user_id=user_id,
                chat_id=chat_id,
                intent=intent,
                agent_name=agent_name,
                status="success",
                latency_ms=total_ms,
                agent_output=agent_result.get("output"),
            )
        except Exception as e:
            log.warning(f"Orchestrator [{trace_id}]: memory capture failed (non-fatal): {e}")

        # 7. Return Final Response
        log.info(f"Orchestrator [{trace_id}]: DONE. intent={intent} total_ms={total_ms}")
        
        return self._build_success_response(trace_id, intent, agent_result["output"])


    # ══════════════════════════════════════════════════════════════════════
    # INTERNAL EXECUTION & RETRY ENGINE
    # ══════════════════════════════════════════════════════════════════════

    async def _execute_with_retry(
        self, agent_instance: Any, payload: Dict[str, Any], timeout_s: float, max_retries: int
    ) -> Dict[str, Any]:
        """Executes an agent with exponential backoff for retryable errors."""
        import random
        
        attempts = 0
        while True:
            attempts += 1
            try:
                # Enforce strict timeout
                return await asyncio.wait_for(agent_instance.execute(payload), timeout=timeout_s)
            
            except asyncio.TimeoutError:
                if attempts > max_retries:
                    raise
                # Exponential backoff for timeout
                delay = 0.5 * (2 ** (attempts - 1)) + random.uniform(0, 0.2)
                log.warning(f"Timeout on attempt {attempts}/{max_retries+1}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
                
            except AgentError as e:
                if not e.retryable or attempts > max_retries:
                    raise
                # Exponential backoff for retryable agent errors
                delay = 0.5 * (2 ** (attempts - 1)) + random.uniform(0, 0.2)
                log.warning(f"Retryable error '{e.type}' on attempt {attempts}/{max_retries+1}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)


    # ══════════════════════════════════════════════════════════════════════
    # UTILITIES & NORMALIZATON
    # ══════════════════════════════════════════════════════════════════════

    def _validate_json_schema(self, agent_envelope: Dict[str, Any]) -> bool:
        """Enforce strict dictionary boundaries."""
        if not isinstance(agent_envelope, dict):
            return False
        if "output" not in agent_envelope:
            return False
        out = agent_envelope["output"]
        if not isinstance(out, dict):
            return False
        # If it's FTR, enforce expected FTR keys exist
        if agent_envelope.get("agent_name") == "FunnelTrendResearcher":
            if "meta" not in out or "trend_clusters" not in out:
                return False
        return True

    def _build_success_response(self, trace_id: str, intent: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalized success response to Router."""
        return {
            "meta": {
                "trace_id": trace_id,
                "intent": intent,
                "status": "ok"
            },
            "data": data,
            "error": None
        }

    def _build_error_response(
        self, trace_id: str, intent: str, err_type: str, msg: str, retryable: bool, source_agent: str = "Orchestrator"
    ) -> Dict[str, Any]:
        """Normalized error response to Router."""
        return {
            "meta": {
                "trace_id": trace_id,
                "intent": intent,
                "status": "error"
            },
            "data": {},
            "error": {
                "source_agent": source_agent,
                "type": err_type,
                "message": msg,
                "retryable": retryable
            }
        }

    def _map_niche_to_ftr(self, niche_raw: str) -> list[dict]:
        """Simple mapping (duplicated from router if router doesn't pass it structured).
           Here, Orchestrator expects router to pass raw niche in args, Orchestrator handles mapping. """
        if not niche_raw:
            return [
                {
                    "niche_id": "ai_email_funnels",
                    "seed_terms": ["AI email automation", "copywriting funnel", "email sequence"]
                },
                {
                    "niche_id": "ai_sales_funnels",
                    "seed_terms": ["AI sales funnel", "lead generation bot", "high ticket"]
                }
            ]
            
        import re
        niche = re.sub(r"\s+", " ", niche_raw.strip())
        slug = re.sub(r"[^a-z0-9_]", "_", niche.lower()).strip("_")
        slug = re.sub(r"_+", "_", slug)

        tokens = [t for t in niche.split() if len(t) >= 2]
        seed_terms = [niche]
        
        if len(tokens) >= 2:
            seed_terms.extend([f"AI {tokens[0]}", f"{tokens[-1]} funnel"])
        elif len(tokens) == 1:
            seed_terms.extend([f"AI {tokens[0]}", f"{tokens[0]} funnel"])
            
        if not any("funnel" in s.lower() and "ai" in s.lower() for s in seed_terms):
            seed_terms.append("AI funnel")

        # deduplicate
        unique = []
        for s in seed_terms:
            if s.lower() not in [u.lower() for u in unique]:
                unique.append(s)

        return [{"niche_id": slug or "custom", "seed_terms": unique[:6]}]
