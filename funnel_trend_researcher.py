#!/usr/bin/env python3
"""
FunnelTrendResearcher — Callable Sub-Agent (v1.1)
─────────────────────────────────────────────────
Detects early trend clusters on YouTube related to AI-powered marketing funnels,
optimized for solopreneurs. Converts aggregated trend signals into actionable
Instagram + Threads content opportunities.

Uses YouTubeTrendSearchTool (PRD-02) as the backend data fetching layer.

Usage:
    agent = FunnelTrendResearcher(youtube_api_key="...", ai_api_url="...", ai_api_key="...", ai_model="...")
    result = await agent.research(input_json)
"""

import re
import math
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
from typing import Optional

import httpx
from youtube_trend_search_tool import YouTubeTrendSearchTool

log = logging.getLogger("funnel_trend_researcher")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS & RULE LIBRARIES
# ══════════════════════════════════════════════════════════════════════════════

SCORING_VERSION = "v1.1.0"

# Stopwords for keyword extraction
STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could of in to for on with at by from as into "
    "through during before after above below between out off over under again "
    "further then once here there when where why how all each every both few "
    "more most other some such no nor not only own same so than too very and "
    "but if or because until while about up down just also this that these those "
    "i me my myself we our ours ourselves you your yours yourself yourselves "
    "he him his himself she her hers herself it its itself they them their theirs "
    "themselves what which who whom whose am been being get got gets getting "
    "new use using used make makes making like dont im youre hes shes".split()
)

# Format pattern rules (regex -> tag)
FORMAT_PATTERNS = [
    (re.compile(r"\b\w+\s+in\s+\d+\s+minutes?\b", re.I), "X in Y minutes"),
    (re.compile(r"\bi\s+replaced\b.*\bwith\s+(ai|automation)\b", re.I), "I replaced X with AI"),
    (re.compile(r"\b(template|swipe\s*file|cheat\s*sheet)\b", re.I), "template / swipe file"),
    (re.compile(r"\b(workflow|automation|automate)\b", re.I), "workflow / automation"),
    (re.compile(r"\b(funnel\s+)?teardown\b", re.I), "funnel teardown"),
    (re.compile(r"\bstep[\s-]by[\s-]step\b", re.I), "step by step"),
    (re.compile(r"\bwalkthrough\b", re.I), "walkthrough"),
    (re.compile(r"\btutorial\b", re.I), "tutorial"),
    (re.compile(r"\bfull\s+(guide|course|breakdown)\b", re.I), "full guide"),
    (re.compile(r"\bvs\.?\b", re.I), "comparison"),
    (re.compile(r"\b(case\s+study|real\s+results?)\b", re.I), "case study"),
    (re.compile(r"\b(no[\s-]code|without\s+coding)\b", re.I), "no-code"),
]

# Tool/platform signals
TOOL_SIGNALS_PATTERNS = [
    "zapier", "make", "make.com", "n8n", "hubspot", "klaviyo", "mailchimp",
    "convertkit", "activecampaign", "ga4", "google analytics", "meta ads",
    "facebook ads", "google ads", "clickfunnels", "gohighlevel", "highlevel",
    "systeme.io", "leadpages", "unbounce", "typeform", "calendly", "stripe",
    "gumroad", "stan store", "beehiiv", "substack", "notion", "airtable",
    "chatgpt", "claude", "midjourney", "openai", "gemini", "perplexity",
    "cursor", "copilot", "jasper", "copy.ai", "writesonic",
]

# Funnel stage signals
FUNNEL_SIGNALS = [
    "opt-in", "optin", "landing page", "lead magnet", "email sequence",
    "nurture", "upsell", "webinar", "vsl", "retargeting", "sales page",
    "crm", "pipeline", "tripwire", "checkout", "conversion", "funnel",
    "squeeze page", "thank you page", "order bump", "downsell",
    "lead generation", "lead gen", "email list", "subscriber",
]

# AI automation signals
AI_SIGNALS = [
    "ai agent", "ai assistant", "ai automation", "ai workflow", "ai funnel",
    "prompt", "llm", "gpt", "automation", "automate", "automated",
    "ai-powered", "ai powered", "machine learning", "personalization",
    "segmentation", "chatbot", "ai chatbot", "ai tool",
]

# Solopreneur signals
SOLO_SIGNALS = [
    "solo", "solopreneur", "one-person", "one person", "no team",
    "small business", "creator", "consultant", "freelancer", "coach",
    "agency of one", "time-saving", "time saving", "done-for-you",
    "done for you", "side hustle", "passive income", "bootstrapped",
    "indie", "maker",
]

# Generic/evergreen terms that reduce novelty
GENERIC_TERMS = frozenset([
    "marketing", "digital marketing", "online marketing", "ai tools",
    "best tools", "top tools", "social media", "content creation",
    "make money", "make money online", "passive income", "side hustle",
    "entrepreneur", "business",
])




# ══════════════════════════════════════════════════════════════════════════════
# 1. QUERY GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """Generates 3 distinct search queries per niche."""

    @staticmethod
    def generate(niche: dict) -> list[str]:
        seeds = niche["seed_terms"]
        seed_group = " OR ".join(f'"{s}"' for s in seeds[:4])

        # Query 1: Core intent (funnel + AI automation)
        q1 = f'({seed_group}) (AI OR agent OR automation) (funnel OR "lead magnet" OR "email sequence")'

        # Query 2: Format-pattern (teardown / template / walkthrough)
        q2 = f'({seed_group}) ("template" OR "teardown" OR "step by step" OR "workflow") AI'

        # Query 3: Tool-stack (Zapier/Make/n8n/CRM/email)
        q3 = f'({seed_group}) (Zapier OR Make OR n8n OR HubSpot OR Klaviyo OR "email marketing") AI'

        queries = [q1, q2, q3]

        # Truncate if too long (YouTube max ~128 chars effectively)
        return [q[:160] for q in queries]


# ══════════════════════════════════════════════════════════════════════════════
# 2. YOUTUBE DATA LAYER (delegates to YouTubeTrendSearchTool — PRD-02)
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_tool_video(v: dict) -> dict:
    """Map YouTubeTrendSearchTool video output to internal item format."""
    return {
        "video_id": v.get("video_id", ""),
        "title": v.get("title", ""),
        "description": v.get("description", "")[:300],
        "channel_title": v.get("channel_title", ""),
        "published_at": v.get("published_at", ""),
        "views": v.get("view_count", 0),
        "likes": v.get("like_count", 0),
        "comments": v.get("comment_count", 0),
        "age_hours": v.get("hours_since_publish", 24.0),
        "vph": v.get("views_per_hour", 0),
        "tags": v.get("tags", []),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. SIGNAL EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════

class SignalExtractor:
    """Extracts keywords, n-grams, format patterns, and tool/funnel signals."""

    @staticmethod
    def tokenize(text: str) -> list[str]:
        text = re.sub(r"[^a-zA-Z0-9\s\-]", " ", text.lower())
        return [w for w in text.split() if len(w) >= 2 and w not in STOPWORDS]

    @staticmethod
    def extract_ngrams(tokens: list[str], n: int = 2) -> list[str]:
        return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]

    @staticmethod
    def detect_format_patterns(text: str) -> list[str]:
        patterns = []
        for regex, tag in FORMAT_PATTERNS:
            if regex.search(text):
                patterns.append(tag)
        return patterns

    @staticmethod
    def detect_tool_signals(text: str) -> list[str]:
        text_lower = text.lower()
        found = []
        for tool in TOOL_SIGNALS_PATTERNS:
            if tool in text_lower:
                found.append(tool)
        return found

    @staticmethod
    def detect_funnel_signals(text: str) -> list[str]:
        text_lower = text.lower()
        return [s for s in FUNNEL_SIGNALS if s in text_lower]

    @staticmethod
    def detect_ai_signals(text: str) -> list[str]:
        text_lower = text.lower()
        return [s for s in AI_SIGNALS if s in text_lower]

    @staticmethod
    def detect_solo_signals(text: str) -> list[str]:
        text_lower = text.lower()
        return [s for s in SOLO_SIGNALS if s in text_lower]

    @classmethod
    def extract(cls, item: dict) -> dict:
        """Extract all signals from a single item."""
        text = f"{item.get('title', '')} {item.get('description', '')}"
        tokens = cls.tokenize(text)
        bigrams = cls.extract_ngrams(tokens, 2)
        trigrams = cls.extract_ngrams(tokens, 3)

        return {
            "tokens": tokens,
            "bigrams": bigrams,
            "trigrams": trigrams,
            "format_patterns": cls.detect_format_patterns(text),
            "tool_signals": cls.detect_tool_signals(text),
            "funnel_signals": cls.detect_funnel_signals(text),
            "ai_signals": cls.detect_ai_signals(text),
            "solo_signals": cls.detect_solo_signals(text),
        }


# ══════════════════════════════════════════════════════════════════════════════
# 4. TREND CLUSTERER (Option A — Lightweight, No Embeddings)
# ══════════════════════════════════════════════════════════════════════════════

class TrendClusterer:
    """Jaccard-based agglomerative clustering."""

    MERGE_THRESHOLD = 0.32
    POST_MERGE_THRESHOLD = 0.28

    @staticmethod
    def jaccard(set_a: set, set_b: set) -> float:
        if not set_a and not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0

    @classmethod
    def compute_similarity(cls, sig_a: dict, sig_b: dict) -> float:
        kw_a = set(sig_a.get("tokens", []))
        kw_b = set(sig_b.get("tokens", []))
        fmt_a = set(sig_a.get("format_patterns", []))
        fmt_b = set(sig_b.get("format_patterns", []))
        return 0.55 * cls.jaccard(kw_a, kw_b) + 0.45 * cls.jaccard(fmt_a, fmt_b)

    @classmethod
    def cluster(cls, items: list[dict], signals: list[dict], max_clusters: int = 8) -> list[list[int]]:
        """Returns list of clusters, each a list of item indices."""
        n = len(items)
        if n == 0:
            return []

        # Start with each item as its own cluster
        clusters = [[i] for i in range(n)]

        # Compute pairwise similarities
        sims = {}
        for i in range(n):
            for j in range(i + 1, n):
                s = cls.compute_similarity(signals[i], signals[j])
                sims[(i, j)] = s

        # Agglomerative merging
        changed = True
        while changed and len(clusters) > max_clusters:
            changed = False
            best_sim = -1
            best_pair = None

            for ci in range(len(clusters)):
                for cj in range(ci + 1, len(clusters)):
                    # Average linkage
                    total_sim = 0
                    count = 0
                    for a in clusters[ci]:
                        for b in clusters[cj]:
                            key = (min(a, b), max(a, b))
                            total_sim += sims.get(key, 0)
                            count += 1
                    avg_sim = total_sim / count if count else 0
                    if avg_sim > best_sim:
                        best_sim = avg_sim
                        best_pair = (ci, cj)

            if best_pair and best_sim >= cls.MERGE_THRESHOLD:
                ci, cj = best_pair
                clusters[ci].extend(clusters[cj])
                clusters.pop(cj)
                changed = True

        # Post-merge small clusters (size < 2)
        small = [c for c in clusters if len(c) < 2]
        large = [c for c in clusters if len(c) >= 2]

        for sc in small:
            best_target = None
            best_sim_val = -1
            for li, lc in enumerate(large):
                total_sim = 0
                count = 0
                for a in sc:
                    for b in lc:
                        key = (min(a, b), max(a, b))
                        total_sim += sims.get(key, 0)
                        count += 1
                avg = total_sim / count if count else 0
                if avg > best_sim_val:
                    best_sim_val = avg
                    best_target = li
            if best_target is not None and best_sim_val >= cls.POST_MERGE_THRESHOLD:
                large[best_target].extend(sc)
            # Otherwise drop singleton (it becomes discarded)

        # Filter clusters with at least 2 items
        valid = [c for c in large if len(c) >= 2]

        # Sort by size desc, limit to max
        valid.sort(key=len, reverse=True)
        return valid[:max_clusters]


# ══════════════════════════════════════════════════════════════════════════════
# 5. SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ScoringEngine:
    """Computes velocity, cohesion, novelty, positioning, and trend_quality."""

    @staticmethod
    def rank_percentile(values: list[float], value: float) -> float:
        if not values:
            return 0.5
        rank = sum(1 for v in values if v <= value)
        return rank / len(values)

    @classmethod
    def compute_velocity_scores(cls, items: list[dict]) -> list[float]:
        all_vph = [it.get("vph", 0) for it in items]
        return [cls.rank_percentile(all_vph, it.get("vph", 0)) for it in items]

    @staticmethod
    def compute_engagement_norm(items: list[dict]) -> list[float]:
        eng_values = []
        for it in items:
            likes = it.get("likes", 0)
            comments = it.get("comments", 0)
            eng_values.append(likes + 3 * comments)
        max_eng = max(eng_values) if eng_values else 1
        if max_eng == 0:
            max_eng = 1
        return [e / max_eng for e in eng_values]

    @staticmethod
    def compute_freshness(items: list[dict], window_hours: int) -> list[float]:
        return [
            1 - min(1, it.get("age_hours", 24) / window_hours)
            for it in items
        ]

    @staticmethod
    def compute_cohesion(cluster_signals: list[dict]) -> float:
        if len(cluster_signals) < 2:
            return 0.0

        # Keyword Jaccard across all items in cluster
        all_kw_sets = [set(s.get("tokens", [])) for s in cluster_signals]
        # Pairwise jaccard average
        total_j = 0
        count = 0
        for i in range(len(all_kw_sets)):
            for j in range(i + 1, len(all_kw_sets)):
                inter = all_kw_sets[i] & all_kw_sets[j]
                union = all_kw_sets[i] | all_kw_sets[j]
                total_j += len(inter) / len(union) if union else 0
                count += 1
        kw_jaccard = total_j / count if count else 0

        # Format overlap rate
        all_formats = [set(s.get("format_patterns", [])) for s in cluster_signals]
        if all_formats:
            # Find dominant format(s)
            fmt_counter = Counter()
            for fs in all_formats:
                fmt_counter.update(fs)
            if fmt_counter:
                dominant = fmt_counter.most_common(1)[0][0]
                matching = sum(1 for fs in all_formats if dominant in fs)
                format_overlap = matching / len(all_formats)
            else:
                format_overlap = 0
        else:
            format_overlap = 0

        return 0.6 * kw_jaccard + 0.4 * format_overlap

    @staticmethod
    def compute_novelty(cluster_signals: list[dict]) -> float:
        # Penalize clusters dominated by generic terms
        all_tokens = []
        for s in cluster_signals:
            all_tokens.extend(s.get("tokens", []))
        if not all_tokens:
            return 0.5
        token_counter = Counter(all_tokens)
        top_10 = [t for t, _ in token_counter.most_common(10)]
        generic_count = sum(1 for t in top_10 if t in GENERIC_TERMS)
        genericness = generic_count / len(top_10) if top_10 else 0
        return 1 - genericness

    @staticmethod
    def compute_positioning(cluster_signals: list[dict]) -> float:
        n = len(cluster_signals)
        if n == 0:
            return 0.0
        funnel_count = sum(1 for s in cluster_signals if s.get("funnel_signals"))
        ai_count = sum(1 for s in cluster_signals if s.get("ai_signals"))
        solo_count = sum(1 for s in cluster_signals if s.get("solo_signals"))
        funnel_score = min(1.0, funnel_count / n)
        ai_score = min(1.0, ai_count / n)
        solo_score = min(1.0, solo_count / n)
        return 0.45 * funnel_score + 0.35 * ai_score + 0.20 * solo_score

    @classmethod
    def score_cluster(
        cls,
        cluster_items: list[dict],
        cluster_signals: list[dict],
        all_items: list[dict],
        window_hours: int,
    ) -> dict:
        # Per-item velocity scores
        v_norms = cls.compute_velocity_scores(all_items)
        cluster_v = [v_norms[0]] if v_norms else [0.5]  # placeholder

        # Recompute velocity for cluster items specifically
        all_vph = [it.get("vph", 0) for it in all_items]
        cluster_v_scores = [
            cls.rank_percentile(all_vph, it.get("vph", 0)) for it in cluster_items
        ]
        velocity = _median(cluster_v_scores) if cluster_v_scores else 0.5

        cohesion = cls.compute_cohesion(cluster_signals)
        novelty = cls.compute_novelty(cluster_signals)
        positioning = cls.compute_positioning(cluster_signals)

        trend_quality = (
            0.35 * velocity + 0.20 * cohesion + 0.15 * novelty + 0.30 * positioning
        )

        # Positioning fit label
        if positioning >= 0.72:
            fit = "strong"
        elif positioning >= 0.50:
            fit = "medium"
        else:
            fit = "weak"

        # Positioning reasons
        reasons = []
        n = len(cluster_signals)
        if n > 0:
            fc = sum(1 for s in cluster_signals if s.get("funnel_signals"))
            ac = sum(1 for s in cluster_signals if s.get("ai_signals"))
            sc = sum(1 for s in cluster_signals if s.get("solo_signals"))
            if fc / n >= 0.5:
                reasons.append("Strong funnel mechanics signals detected")
            if ac / n >= 0.5:
                reasons.append("Strong AI automation signals present")
            if sc / n >= 0.3:
                reasons.append("Solopreneur/creator framing evident")
            if not reasons:
                reasons.append("Limited positioning signal overlap")

        return {
            "scores": {
                "trend_quality": round(trend_quality, 3),
                "velocity": round(velocity, 3),
                "cohesion": round(cohesion, 3),
                "novelty": round(novelty, 3),
                "positioning": round(positioning, 3),
            },
            "positioning_fit": {
                "fit": fit,
                "reasons": reasons[:5],
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
# 6. CONTENT OPPORTUNITY GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class ContentOpportunityGenerator:
    """Generates IG hooks, carousel outlines, and Threads starters using AI."""

    def __init__(self, ai_url: str, ai_key: str, ai_model: str, client: httpx.AsyncClient):
        self.ai_url = ai_url
        self.ai_key = ai_key
        self.ai_model = ai_model
        self.client = client

    async def generate(self, clusters: list[dict], positioning: dict) -> dict:
        """Generate content opportunities from cluster insights."""
        persona = positioning.get("persona", "solopreneurs")
        angle = positioning.get("angle", "AI-powered funnel advantage")

        cluster_summaries = []
        for c in clusters[:5]:
            cluster_summaries.append(
                f"- {c['cluster_name']}: {c['summary']} "
                f"(keywords: {', '.join(c['keyword_signals']['top_keywords'][:5])})"
            )

        cluster_text = "\n".join(cluster_summaries) if cluster_summaries else "No specific clusters found."

        prompt = f"""You are a content strategist for {persona} focused on {angle}.

Based on these trending topic clusters from YouTube:
{cluster_text}

Generate EXACTLY the following in valid JSON format (no markdown, no code blocks, just JSON):
{{
  "instagram_hooks": [5 attention-grabbing Instagram post hooks, each 12-160 chars],
  "carousel_outlines": [
    {{
      "title": "carousel title (8-90 chars)",
      "slides": [exactly 8 slide texts, each 5-120 chars]
    }},
    {{
      "title": "carousel title (8-90 chars)",
      "slides": [exactly 8 slide texts, each 5-120 chars]
    }}
  ],
  "threads_starters": [5 Threads conversation starters, each 12-240 chars]
}}

Rules:
- Position everything for {persona} using {angle}
- Make hooks provocative and scroll-stopping
- Carousel slides should be educational and actionable
- Threads starters should spark discussion
- All content in English
- Return ONLY valid JSON, nothing else"""

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.ai_key}",
            }
            body = {
                "model": self.ai_model,
                "messages": [
                    {"role": "system", "content": "You are a JSON-only content generator. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 2000,
            }
            resp = await self.client.post(self.ai_url, json=body, headers=headers, timeout=30)
            if resp.status_code != 200:
                log.warning("AI content gen failed: %s", resp.status_code)
                return self._fallback(clusters)

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Try to parse JSON from response
            content = content.strip()
            # Remove markdown code blocks if present
            if content.startswith("```"):
                content = re.sub(r"^```\w*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            result = json.loads(content)

            # Validate structure
            hooks = result.get("instagram_hooks", [])[:5]
            carousels = result.get("carousel_outlines", [])[:2]
            threads = result.get("threads_starters", [])[:5]

            # Pad if needed
            while len(hooks) < 5:
                hooks.append(f"AI funnels are changing the game for {persona} 🚀")
            while len(threads) < 5:
                threads.append(f"What's your biggest challenge with AI-powered funnels?")
            while len(carousels) < 2:
                carousels.append({
                    "title": f"AI Funnel Strategies for {persona.title()}",
                    "slides": [f"Slide {i+1}" for i in range(8)],
                })

            # Ensure carousel slides are exactly 8
            for c in carousels:
                slides = c.get("slides", [])[:8]
                while len(slides) < 8:
                    slides.append("Key takeaway")
                c["slides"] = slides

            return {
                "instagram_hooks": hooks[:5],
                "carousel_outlines": carousels[:2],
                "threads_starters": threads[:5],
            }

        except (json.JSONDecodeError, KeyError, Exception) as e:
            log.warning("Content generation parse error: %s", e)
            return self._fallback(clusters)

    @staticmethod
    def _fallback(clusters: list[dict]) -> dict:
        """Generate basic content when AI is unavailable."""
        topics = [c.get("cluster_name", "AI Funnels") for c in clusters[:3]]
        topic_str = topics[0] if topics else "AI Funnels"

        return {
            "instagram_hooks": [
                f"🔥 {topic_str} is trending right now — here's why solopreneurs should care",
                "Stop building funnels manually. AI can do it 10x faster ⚡",
                "The #1 AI funnel mistake solopreneurs make (and how to fix it)",
                f"I tested {topic_str} for 30 days — the results surprised me",
                "Your competitors are using AI funnels. Are you? 🤔",
            ],
            "carousel_outlines": [
                {
                    "title": f"How to Leverage {topic_str} as a Solopreneur",
                    "slides": [
                        f"How to Leverage {topic_str}",
                        "The problem: manual funnels don't scale",
                        "What AI-powered funnels can do differently",
                        "Step 1: Identify your funnel bottleneck",
                        "Step 2: Choose the right AI tool",
                        "Step 3: Automate your lead magnet delivery",
                        "Step 4: Set up AI-driven email sequences",
                        "Start today — your funnel won't build itself",
                    ],
                },
                {
                    "title": "5 AI Tools Every Solopreneur Needs for Funnels",
                    "slides": [
                        "5 AI Tools for Solopreneur Funnels",
                        "Tool 1: AI copywriting for landing pages",
                        "Tool 2: Automated email sequence builders",
                        "Tool 3: AI chatbots for lead qualification",
                        "Tool 4: Smart analytics and A/B testing",
                        "Tool 5: Workflow automation (Zapier/Make/n8n)",
                        "The ROI: 10x output with half the effort",
                        "Save this for later & follow for more",
                    ],
                },
            ],
            "threads_starters": [
                f"🧵 {topic_str} is blowing up on YouTube. Here's what solopreneurs need to know...",
                "Hot take: Most solopreneurs are overcomplicating their funnels. AI makes it simple.",
                "What AI funnel tool has saved you the most time? Drop it below 👇",
                "I analyzed the top AI funnel trends this week. The pattern is clear...",
                "Solopreneurs: are you using AI in your funnels yet, or still doing everything manually?",
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# 7. CLUSTER BUILDER (assembles cluster output objects)
# ══════════════════════════════════════════════════════════════════════════════

class ClusterBuilder:
    """Assembles final cluster output objects with names, summaries, and metrics."""

    @staticmethod
    def build_cluster_output(
        cluster_indices: list[int],
        items: list[dict],
        signals: list[dict],
        all_items: list[dict],
        window_hours: int,
    ) -> dict:
        cluster_items = [items[i] for i in cluster_indices]
        cluster_signals = [signals[i] for i in cluster_indices]

        # Aggregate keywords
        all_tokens = []
        all_bigrams = []
        all_tools = []
        all_formats = set()
        for s in cluster_signals:
            all_tokens.extend(s.get("tokens", []))
            all_bigrams.extend(s.get("bigrams", []))
            all_tools.extend(s.get("tool_signals", []))
            all_formats.update(s.get("format_patterns", []))

        token_counter = Counter(all_tokens)
        bigram_counter = Counter(all_bigrams)
        tool_counter = Counter(all_tools)

        top_keywords = [t for t, _ in token_counter.most_common(20)]
        recurring_phrases = [p for p, c in bigram_counter.most_common(12) if c >= 2]
        tool_signals = [t for t, _ in tool_counter.most_common(12)]

        # Pad recurring_phrases if needed (min 3)
        while len(recurring_phrases) < 3:
            remaining = [p for p, _ in bigram_counter.most_common(20) if p not in recurring_phrases]
            if remaining:
                recurring_phrases.append(remaining[0])
            else:
                recurring_phrases.append(" ".join(top_keywords[:2]) if len(top_keywords) >= 2 else "ai funnel")

        # Pad top_keywords if needed (min 5)
        while len(top_keywords) < 5:
            top_keywords.append("ai")

        # Cluster name
        funnel_kws = [t for t in top_keywords[:5] if t not in GENERIC_TERMS]
        fmt_tag = list(all_formats)[:1]
        name_parts = funnel_kws[:2] if funnel_kws else top_keywords[:2]
        name = " ".join(w.title() for w in name_parts)
        if fmt_tag:
            name += f" ({fmt_tag[0]})"
        if len(name) < 4:
            name = "AI Funnel Trend"

        # Engagement snapshot
        views = [it.get("views", 0) for it in cluster_items]
        likes = [it.get("likes", 0) for it in cluster_items]
        comments = [it.get("comments", 0) for it in cluster_items]
        vphs = [it.get("vph", 0) for it in cluster_items]
        ages = [it.get("age_hours", 24) for it in cluster_items]

        # Why resonating
        why = []
        if any(s.get("ai_signals") for s in cluster_signals):
            why.append("AI automation content resonates with creators seeking efficiency")
        if any(s.get("funnel_signals") for s in cluster_signals):
            why.append("Funnel mechanics content shows strong how-to intent from viewers")
        if any(s.get("tool_signals") for s in cluster_signals):
            tools_mentioned = list(set(t for s in cluster_signals for t in s.get("tool_signals", [])))[:3]
            why.append(f"Specific tool mentions ({', '.join(tools_mentioned)}) indicate actionable content")
        if all_formats:
            why.append(f"Content format patterns like {list(all_formats)[0]} drive engagement")
        if _median(vphs) > 50:
            why.append("High velocity indicates early-stage viral momentum")
        if not why:
            why = ["Recurring keyword patterns indicate emerging interest", "Multiple creators covering similar angles"]
        while len(why) < 2:
            why.append("Growing search interest in this topic area")

        # Summary
        summary_parts = []
        if funnel_kws:
            summary_parts.append(f"Cluster around {', '.join(funnel_kws[:3])}")
        if tool_signals:
            summary_parts.append(f"with tool mentions: {', '.join(tool_signals[:3])}")
        if all_formats:
            summary_parts.append(f"in formats: {', '.join(list(all_formats)[:2])}")
        summary = " ".join(summary_parts) if summary_parts else "Emerging trend cluster in AI funnels"
        summary += f". {len(cluster_items)} videos detected with median {_median(vphs):.0f} views/hour."
        if len(summary) < 20:
            summary = "Emerging AI funnel trend cluster with growing engagement signals across multiple creators."

        # Example links
        example_links = []
        sorted_items = sorted(cluster_items, key=lambda x: x.get("vph", 0), reverse=True)
        for it in sorted_items[:6]:
            vid = it.get("video_id", "")
            if vid:
                example_links.append({
                    "url": f"https://youtube.com/watch?v={vid}",
                    "title_hint": it.get("title", "")[:120] or "AI Funnel Video",
                })
        while len(example_links) < 2:
            example_links.append({"url": "https://youtube.com", "title_hint": "YouTube search result"})

        # Scoring
        scoring = ScoringEngine.score_cluster(cluster_items, cluster_signals, all_items, window_hours)

        return {
            "cluster_name": name[:80],
            "summary": summary[:360],
            "why_resonating": why[:6],
            "format_patterns": list(all_formats)[:8] if all_formats else ["general content", "informational"],
            "keyword_signals": {
                "top_keywords": top_keywords[:20],
                "recurring_phrases": recurring_phrases[:12],
                "tool_signals": tool_signals[:12],
            },
            "engagement_snapshot": {
                "items_count": len(cluster_items),
                "views_range": [min(views), max(views)] if views else [0, 0],
                "likes_range": [min(likes), max(likes)] if likes else [0, 0],
                "comments_range": [min(comments), max(comments)] if comments else [0, 0],
                "velocity_range_vph": [
                    round(_percentile(vphs, 25), 2),
                    round(_percentile(vphs, 90), 2),
                ] if vphs else [0, 0],
                "median_age_hours": round(_median(ages), 1) if ages else 0,
            },
            "example_links": example_links[:6],
            "scores": scoring["scores"],
            "positioning_fit": scoring["positioning_fit"],
        }


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _median(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (pct / 100) * (len(s) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return s[f] * (c - k) + s[c] * (k - f)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AGENT CLASS
# ══════════════════════════════════════════════════════════════════════════════

class FunnelTrendResearcher:
    """
    Callable sub-agent: detects early trend clusters on YouTube related to
    AI-powered marketing funnels for solopreneurs.

    Usage:
        agent = FunnelTrendResearcher(
            youtube_api_key="YOUR_KEY",
            ai_api_url="https://api.example.com/v1/chat/completions",
            ai_api_key="YOUR_AI_KEY",
            ai_model="model-name",
        )
        result = await agent.research(input_json_dict)
    """

    DEFAULT_NICHES = [
        {
            "niche_id": "ai-email-funnel",
            "seed_terms": ["AI email funnel", "automated email sequence", "AI lead magnet"],
        },
        {
            "niche_id": "ai-sales-funnel",
            "seed_terms": ["AI sales funnel", "marketing automation AI", "AI funnel builder"],
        },
    ]

    def __init__(
        self,
        youtube_api_key: str,
        ai_api_url: str = "",
        ai_api_key: str = "",
        ai_model: str = "",
    ):
        self.youtube_api_key = youtube_api_key
        self.ai_api_url = ai_api_url
        self.ai_api_key = ai_api_key
        self.ai_model = ai_model

    async def research(self, input_data: dict) -> dict:
        """Main entry point. Takes input JSON dict, returns output JSON dict."""
        request_id = input_data.get("request_id", "unknown")

        try:
            return await self._do_research(input_data, request_id)
        except Exception as e:
            log.exception("FunnelTrendResearcher unexpected error")
            return self._error_response(request_id, "INTERNAL_ERROR", str(e), False)

    async def _do_research(self, input_data: dict, request_id: str) -> dict:
        niches = input_data.get("niches", self.DEFAULT_NICHES)
        discovery = input_data.get("discovery", {})
        adaptive = input_data.get("adaptive_window", {})
        limits = input_data.get("limits", {})
        positioning_cfg = input_data.get("positioning", {})

        primary_hours = adaptive.get("primary_hours", 48)
        fallback_1 = adaptive.get("fallback_days_1", 7)
        fallback_2 = adaptive.get("fallback_days_2", 30)
        min_clusters = adaptive.get("min_clusters_required", 3)
        min_quality = adaptive.get("min_quality_threshold", 0.62)
        max_clusters = limits.get("max_clusters", 8)
        max_per_query = limits.get("max_results_per_query", 40)
        max_total = limits.get("max_total_items", 120)

        relevance_lang = discovery.get("relevance_language", "en")
        region_code = discovery.get("region_code", "")

        # Window cascade: 48h -> 7d -> 30d
        windows = [
            ("48h", primary_hours, "none"),
            ("7d", fallback_1 * 24, "48h_to_7d"),
            ("30d", fallback_2 * 24, "7d_to_30d"),
        ]

        # Initialize YouTubeTrendSearchTool (PRD-02)
        tool = YouTubeTrendSearchTool(api_key=self.youtube_api_key)

        # Generate queries for all niches
        all_queries = []
        queries_used = []
        for niche in niches:
            qs = QueryGenerator.generate(niche)
            all_queries.extend(qs)
            queries_used.append({
                "niche_id": niche["niche_id"],
                "query_strings": qs,
            })

        final_clusters = []
        final_window = windows[0]
        all_items = []
        all_signals = []
        discarded = defaultdict(lambda: {"count": 0, "examples": []})

        async with httpx.AsyncClient() as client:

            for mode, hours, expanded_from in windows:
                # Discover items via YouTubeTrendSearchTool (per query)
                seen_ids = set()
                all_items = []
                api_errors = []

                for query in all_queries:
                    tool_input = {
                        "search_term": query,
                        "published_after_hours": hours,
                        "videos_per_query": min(max_per_query, 50),
                        "region_code": region_code or "",
                        "relevance_language": relevance_lang,
                        "sort_order": "relevance",
                        "min_views": 0,
                    }
                    result = await tool.search(tool_input)

                    if "error" in result:
                        err = result["error"]
                        api_errors.append(err)
                        log.warning("Tool error for query '%s': %s", query[:60], err.get("message", ""))
                        if err.get("type") in ("invalid_key", "quota_exceeded"):
                            # Fatal — stop all queries
                            return self._error_response(
                                request_id,
                                err["type"].upper(),
                                err.get("message", "YouTube API error"),
                                err.get("retryable", False),
                            )
                        continue

                    for v in result.get("videos", []):
                        vid = v.get("video_id", "")
                        if vid and vid not in seen_ids:
                            seen_ids.add(vid)
                            all_items.append(_normalize_tool_video(v))
                        if len(all_items) >= max_total:
                            break
                    if len(all_items) >= max_total:
                        break

                if not all_items:
                    final_window = (mode, hours, expanded_from)
                    continue

                # Extract signals
                all_signals = [SignalExtractor.extract(it) for it in all_items]

                # Deduplicate near-identical titles from same channel
                dedup_indices = self._deduplicate(all_items, discarded)
                dedup_items = [all_items[i] for i in dedup_indices]
                dedup_signals = [all_signals[i] for i in dedup_indices]

                # Filter off-topic
                filtered_indices = self._filter_offtopic(
                    dedup_items, dedup_signals, discarded
                )
                filtered_items = [dedup_items[i] for i in filtered_indices]
                filtered_signals = [dedup_signals[i] for i in filtered_indices]

                if len(filtered_items) < 2:
                    final_window = (mode, hours, expanded_from)
                    continue

                # Cluster
                cluster_groups = TrendClusterer.cluster(
                    filtered_items, filtered_signals, max_clusters
                )

                # Build cluster outputs
                clusters_out = []
                for group in cluster_groups:
                    co = ClusterBuilder.build_cluster_output(
                        group, filtered_items, filtered_signals, filtered_items, hours
                    )
                    clusters_out.append(co)

                # Apply discard logic
                valid_clusters = []
                for co in clusters_out:
                    tq = co["scores"]["trend_quality"]
                    coh = co["scores"]["cohesion"]
                    fit = co["positioning_fit"]["fit"]

                    if coh < 0.35:
                        discarded["LOW_COHESION"]["count"] += 1
                        discarded["LOW_COHESION"]["examples"].append(co["cluster_name"])
                        continue
                    if fit == "weak" and tq < 0.70:
                        discarded["POSITIONING_MISMATCH"]["count"] += 1
                        discarded["POSITIONING_MISMATCH"]["examples"].append(co["cluster_name"])
                        continue
                    valid_clusters.append(co)

                final_clusters = valid_clusters
                final_window = (mode, hours, expanded_from)

                # Check if we have enough quality clusters
                if len(final_clusters) >= min_clusters:
                    max_quality = max(
                        (c["scores"]["trend_quality"] for c in final_clusters), default=0
                    )
                    if max_quality >= min_quality:
                        break  # Good enough, stop expanding

            # Sort clusters by trend_quality desc
            final_clusters.sort(
                key=lambda c: c["scores"]["trend_quality"], reverse=True
            )

            # Generate content opportunities
            content_gen = ContentOpportunityGenerator(
                self.ai_api_url, self.ai_api_key, self.ai_model, client
            )
            if self.ai_api_url and self.ai_api_key:
                content = await content_gen.generate(final_clusters, positioning_cfg)
            else:
                content = content_gen._fallback(final_clusters)

            # Build discarded notes
            discarded_notes = []
            for reason, data in discarded.items():
                discarded_notes.append({
                    "reason_code": reason,
                    "count": data["count"],
                    "examples": data["examples"][:5],
                })

            # Determine status
            mode_str, hours_val, expanded = final_window
            status = "ok"
            if not final_clusters:
                status = "partial" if all_items else "error"

            return {
                "meta": {
                    "request_id": request_id,
                    "status": status,
                    "generated_at_utc": _iso_now(),
                    "final_window": {
                        "mode": mode_str,
                        "hours": hours_val,
                        "expanded_from": expanded,
                    },
                    "queries_used": queries_used,
                    "scoring_version": SCORING_VERSION,
                    "notes": [
                        f"Processed {len(all_items)} items across {len(all_queries)} queries",
                        f"Formed {len(final_clusters)} valid clusters",
                    ],
                    "errors": [],
                },
                "trend_clusters": final_clusters,
                "content_opportunities": content,
                "discarded_notes": discarded_notes,
            }

    def _deduplicate(self, items: list[dict], discarded: dict) -> list[int]:
        """Remove near-duplicate titles from same channel."""
        seen = set()
        valid = []
        for i, item in enumerate(items):
            key = (item.get("channel_title", "").lower(), item.get("title", "").lower()[:60])
            if key not in seen:
                seen.add(key)
                valid.append(i)
            else:
                discarded["DUPLICATE"]["count"] += 1
                if len(discarded["DUPLICATE"]["examples"]) < 5:
                    discarded["DUPLICATE"]["examples"].append(item.get("title", "")[:80])
        return valid

    def _filter_offtopic(
        self, items: list[dict], signals: list[dict], discarded: dict
    ) -> list[int]:
        """Filter off-topic and low-intent items."""
        valid = []
        for i, (item, sig) in enumerate(zip(items, signals)):
            # Must have at least 1 of: funnel, AI, or tool signals
            has_funnel = bool(sig.get("funnel_signals"))
            has_ai = bool(sig.get("ai_signals"))
            has_tool = bool(sig.get("tool_signals"))

            if not (has_funnel or has_ai or has_tool):
                discarded["OFF_TOPIC"]["count"] += 1
                if len(discarded["OFF_TOPIC"]["examples"]) < 5:
                    discarded["OFF_TOPIC"]["examples"].append(item.get("title", "")[:80])
                continue
            valid.append(i)
        return valid

    @staticmethod
    def _error_response(request_id: str, code: str, message: str, retryable: bool) -> dict:
        return {
            "meta": {
                "request_id": request_id,
                "status": "error",
                "generated_at_utc": _iso_now(),
                "final_window": {"mode": "48h", "hours": 48, "expanded_from": "none"},
                "queries_used": [],
                "scoring_version": SCORING_VERSION,
                "notes": [],
                "errors": [{"code": code, "message": message, "retryable": retryable}],
            },
            "trend_clusters": [],
            "content_opportunities": {
                "instagram_hooks": [
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                ],
                "carousel_outlines": [
                    {"title": "Unavailable", "slides": ["Unavailable"] * 8},
                    {"title": "Unavailable", "slides": ["Unavailable"] * 8},
                ],
                "threads_starters": [
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                    "Service temporarily unavailable",
                ],
            },
            "discarded_notes": [],
        }


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def format_for_telegram(result: dict) -> list[str]:
    """Format FunnelTrendResearcher output as readable Telegram messages."""
    messages = []

    meta = result.get("meta", {})
    status = meta.get("status", "unknown")

    # Header
    header = f"🔍 *Funnel Trend Research Report*\n"
    header += f"Status: {'✅' if status == 'ok' else '⚠️'} {status.upper()}\n"
    window = meta.get("final_window", {})
    header += f"Window: {window.get('mode', '48h')}"
    if window.get("expanded_from", "none") != "none":
        header += f" (expanded from {window['expanded_from']})"
    header += f"\n📊 Scoring: {meta.get('scoring_version', 'v1.0.0')}\n"

    if meta.get("notes"):
        for note in meta["notes"]:
            header += f"📝 {note}\n"
    messages.append(header)

    # Error handling
    if meta.get("errors"):
        err_msg = "❌ *Errors:*\n"
        for err in meta["errors"]:
            err_msg += f"• `{err['code']}`: {err['message']}\n"
        messages.append(err_msg)

    # Trend clusters
    clusters = result.get("trend_clusters", [])
    if clusters:
        for i, cluster in enumerate(clusters, 1):
            msg = f"📈 *Cluster {i}: {cluster['cluster_name']}*\n"
            msg += f"_{cluster['summary']}_\n\n"

            # Scores
            scores = cluster.get("scores", {})
            fit = cluster.get("positioning_fit", {})
            msg += f"Quality: {scores.get('trend_quality', 0):.0%} | "
            msg += f"Velocity: {scores.get('velocity', 0):.0%} | "
            msg += f"Fit: {fit.get('fit', 'N/A').upper()}\n"

            # Engagement
            eng = cluster.get("engagement_snapshot", {})
            msg += f"📊 {eng.get('items_count', 0)} videos | "
            vr = eng.get("views_range", [0, 0])
            msg += f"Views: {_format_num(vr[0])}-{_format_num(vr[1])} | "
            msg += f"Median age: {eng.get('median_age_hours', 0):.0f}h\n"

            # Keywords
            kw = cluster.get("keyword_signals", {})
            if kw.get("top_keywords"):
                msg += f"🔑 {', '.join(kw['top_keywords'][:6])}\n"

            # Tools
            if kw.get("tool_signals"):
                msg += f"🛠 Tools: {', '.join(kw['tool_signals'][:4])}\n"

            # Links
            links = cluster.get("example_links", [])
            if links:
                msg += "\n🔗 Examples:\n"
                for link in links[:3]:
                    msg += f"• [{link['title_hint'][:50]}]({link['url']})\n"

            messages.append(msg)
    else:
        messages.append("📭 No trend clusters found in this window.")

    # Content opportunities
    content = result.get("content_opportunities", {})
    if content:
        content_msg = "💡 *Content Opportunities*\n\n"

        hooks = content.get("instagram_hooks", [])
        if hooks:
            content_msg += "*IG Hooks:*\n"
            for h in hooks[:5]:
                content_msg += f"• {h}\n"
            content_msg += "\n"

        carousels = content.get("carousel_outlines", [])
        if carousels:
            content_msg += "*Carousel Outlines:*\n"
            for c in carousels[:2]:
                content_msg += f"📑 *{c.get('title', 'Carousel')}*\n"
                for j, slide in enumerate(c.get("slides", []), 1):
                    content_msg += f"  {j}. {slide}\n"
                content_msg += "\n"

        threads = content.get("threads_starters", [])
        if threads:
            content_msg += "*Threads Starters:*\n"
            for t in threads[:5]:
                content_msg += f"🧵 {t}\n"

        messages.append(content_msg)

    # Discarded
    discarded = result.get("discarded_notes", [])
    if discarded:
        disc_msg = "🗑 *Discarded:*\n"
        for d in discarded:
            disc_msg += f"• {d['reason_code']}: {d['count']} items\n"
        messages.append(disc_msg)

    return messages


def _format_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
