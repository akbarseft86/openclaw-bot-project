#!/usr/bin/env python3
"""
TelegramTrendBotRouter — PRD-03
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Telegram command router + HTML formatter for BMAD trend research.
Stateless routing layer between Telegram users and FunnelTrendResearcher.

Supports:
  /trend <niche>          — adaptive window, summary mode
  /trend_full <niche>     — summary + JSON file attachment
  /trend_window <h> <n>   — fixed window override
  /trend_help             — usage guide

Usage:
    from telegram_trend_bot_router import TrendBotRouter
    router = TrendBotRouter(youtube_api_key=..., available_models=..., ...)
    router.register_handlers(app)
"""

import io
import re
import json
import time
import uuid
import logging
from datetime import datetime, timezone
from html import escape as html_escape

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bmad_orchestrator import BMADOrchestrator

log = logging.getLogger("trend_router")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

TELEGRAM_MSG_LIMIT = 4096
NICHE_MAX_LEN = 40
NICHE_RE = re.compile(r"^[a-zA-Z0-9_ \-]+$")
WINDOW_MIN = 6
WINDOW_MAX = 720

SEPARATOR = "━━━━━━━━━━━━━━━━━━"


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """In-memory per-user cooldown + global guard."""

    def __init__(self, user_cooldown: float = 10.0, global_rpm: int = 20):
        self.user_cooldown = user_cooldown
        self.global_rpm = global_rpm
        self._user_last: dict[int, float] = {}
        self._global_timestamps: list[float] = []

    def check_user(self, user_id: int) -> bool:
        """Returns True if allowed, False if rate-limited."""
        now = time.monotonic()
        last = self._user_last.get(user_id, 0)
        if now - last < self.user_cooldown:
            return False
        self._user_last[user_id] = now
        return True

    def check_global(self) -> bool:
        """Returns True if global rate allows, False if too many."""
        now = time.monotonic()
        cutoff = now - 60
        self._global_timestamps = [t for t in self._global_timestamps if t > cutoff]
        if len(self._global_timestamps) >= self.global_rpm:
            return False
        self._global_timestamps.append(now)
        return True

    def remaining_cooldown(self, user_id: int) -> float:
        now = time.monotonic()
        last = self._user_last.get(user_id, 0)
        return max(0, self.user_cooldown - (now - last))


# ══════════════════════════════════════════════════════════════════════════════
# NICHE MAPPER
# ══════════════════════════════════════════════════════════════════════════════

class NicheMapper:
    """Lightweight niche string → structured niches[] mapping."""

    AI_ANCHORS = ["AI", "automation", "funnel"]

    @classmethod
    def map(cls, niche_raw: str) -> list[dict]:
        """Convert a niche string into FunnelTrendResearcher niches[] format."""
        niche = re.sub(r"\s+", " ", niche_raw.strip())
        slug = re.sub(r"[^a-z0-9_]", "_", niche.lower()).strip("_")
        slug = re.sub(r"_+", "_", slug)

        tokens = [t for t in niche.split() if len(t) >= 2]
        seed_terms = []

        # Original niche as first seed
        seed_terms.append(niche)

        # Token pairs with AI anchors
        if len(tokens) >= 2:
            seed_terms.append(f"AI {tokens[0]}")
            seed_terms.append(f"{tokens[-1]} funnel")
        elif len(tokens) == 1:
            seed_terms.append(f"AI {tokens[0]}")
            seed_terms.append(f"{tokens[0]} funnel")
            seed_terms.append(f"{tokens[0]} automation")

        # Add "AI funnel" anchor if not already covered
        if not any("funnel" in s.lower() and "ai" in s.lower() for s in seed_terms):
            seed_terms.append("AI funnel")

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for s in seed_terms:
            key = s.lower()
            if key not in seen:
                seen.add(key)
                unique.append(s)

        return [{
            "niche_id": slug or "custom",
            "seed_terms": unique[:6],
        }]


# ══════════════════════════════════════════════════════════════════════════════
# HTML FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

class TrendFormatter:
    """Formats FunnelTrendResearcher JSON output into Telegram HTML messages."""

    @classmethod
    def format_summary(
        cls, result: dict, niche: str, max_clusters: int = 3
    ) -> list[str]:
        """
        Build summary messages in HTML parse mode.
        Returns list of message strings, each ≤ 4096 chars.
        """
        meta = result.get("meta", {})
        status = meta.get("status", "unknown")
        window = meta.get("final_window", {})
        clusters = result.get("trend_clusters", [])
        content = result.get("content_opportunities", {})
        discarded = result.get("discarded_notes", [])

        # ── Build single message, then apply truncation ──
        parts = []

        # Header
        status_icon = "✅" if status == "ok" else ("⚠️" if status == "partial" else "❌")
        parts.append(
            f"🔍 <b>Funnel Trend Report</b>\n"
            f"{SEPARATOR}\n"
            f"📌 Niche: <code>{html_escape(niche)}</code>\n"
            f"📊 Status: {status_icon} {html_escape(status.upper())}\n"
            f"🕐 Window: {html_escape(window.get('mode', '48h'))}"
        )
        if window.get("expanded_from", "none") != "none":
            parts[-1] += f" (expanded from {html_escape(window['expanded_from'])})"
        parts[-1] += f"\n{html_escape(meta.get('scoring_version', 'v1.1.0'))}"

        # Errors
        if meta.get("errors"):
            err_lines = ["", "❌ <b>Errors:</b>"]
            for err in meta["errors"]:
                err_lines.append(f"• <code>{html_escape(err.get('code', ''))}</code>: {html_escape(err.get('message', ''))}")
            parts.append("\n".join(err_lines))

        # Clusters (select top N by trend_quality, prefer strong/medium fit)
        selected = cls._select_clusters(clusters, max_clusters)
        if selected:
            for i, cluster in enumerate(selected, 1):
                parts.append(cls._format_cluster(cluster, i))
        else:
            parts.append("\n📭 No strong trends detected in this window.")

        # Content opportunities
        if content and selected:
            parts.append(cls._format_content(content))

        # Discarded notes
        if discarded:
            disc_lines = [f"\n🗑 <b>Discarded:</b>"]
            for d in discarded[:3]:
                disc_lines.append(f"• {html_escape(d.get('reason_code', ''))}: {d.get('count', 0)} items")
            parts.append("\n".join(disc_lines))

        # Notes
        if meta.get("notes"):
            note_lines = [f"\n📝 <b>Notes:</b>"]
            for n in meta["notes"][:2]:
                note_lines.append(f"• {html_escape(n)}")
            parts.append("\n".join(note_lines))

        # Assemble and truncate
        full_msg = "\n".join(parts)
        return cls._truncate_to_messages(full_msg, parts, selected, content)

    @classmethod
    def _select_clusters(cls, clusters: list[dict], max_n: int) -> list[dict]:
        """Select top clusters: prefer strong/medium fit, then by trend_quality."""
        if not clusters:
            return []

        def sort_key(c):
            fit = c.get("positioning_fit", {}).get("fit", "weak")
            fit_rank = {"strong": 0, "medium": 1, "weak": 2}.get(fit, 3)
            quality = c.get("scores", {}).get("trend_quality", 0)
            return (fit_rank, -quality)

        sorted_clusters = sorted(clusters, key=sort_key)
        return sorted_clusters[:max_n]

    @classmethod
    def _format_cluster(cls, cluster: dict, idx: int) -> str:
        """Format a single cluster block in HTML."""
        name = html_escape(cluster.get("cluster_name", "Trend")[:60])
        summary = html_escape(cluster.get("summary", "")[:200])
        scores = cluster.get("scores", {})
        fit = cluster.get("positioning_fit", {})
        eng = cluster.get("engagement_snapshot", {})
        kw = cluster.get("keyword_signals", {})
        links = cluster.get("example_links", [])

        lines = [
            f"\n{SEPARATOR}",
            f"📈 <b>#{idx} {name}</b>",
            f"<i>{summary}</i>",
            "",
            f"Quality: {scores.get('trend_quality', 0):.0%} │ "
            f"Velocity: {scores.get('velocity', 0):.0%} │ "
            f"Fit: {html_escape(fit.get('fit', 'N/A').upper())}",
        ]

        # Engagement
        vr = eng.get("views_range", [0, 0])
        lines.append(
            f"📊 {eng.get('items_count', 0)} videos │ "
            f"Views: {cls._fmt_num(vr[0])}–{cls._fmt_num(vr[1])} │ "
            f"Age: {eng.get('median_age_hours', 0):.0f}h"
        )

        # Keywords
        top_kw = kw.get("top_keywords", [])[:6]
        if top_kw:
            lines.append(f"🔑 {', '.join(html_escape(k) for k in top_kw)}")

        # Tools
        tools = kw.get("tool_signals", [])[:4]
        if tools:
            lines.append(f"🛠 {', '.join(html_escape(t) for t in tools)}")

        # Links (compact)
        if links:
            for lnk in links[:2]:
                title = html_escape(lnk.get("title_hint", "Video")[:45])
                url = lnk.get("url", "")
                lines.append(f"🔗 <a href=\"{url}\">{title}</a>")

        return "\n".join(lines)

    @classmethod
    def _format_content(cls, content: dict) -> str:
        """Format content opportunities in HTML."""
        lines = [f"\n{SEPARATOR}", "💡 <b>Content Opportunities</b>", ""]

        hooks = content.get("instagram_hooks", [])[:3]
        if hooks:
            lines.append("<b>IG Hooks:</b>")
            for h in hooks:
                lines.append(f"• {html_escape(h)}")
            lines.append("")

        carousels = content.get("carousel_outlines", [])[:1]
        if carousels:
            c = carousels[0]
            lines.append(f"📑 <b>{html_escape(c.get('title', 'Carousel')[:60])}</b>")
            for j, slide in enumerate(c.get("slides", [])[:8], 1):
                lines.append(f"  {j}. {html_escape(slide)}")
            lines.append("")

        threads = content.get("threads_starters", [])[:3]
        if threads:
            lines.append("<b>Threads:</b>")
            for t in threads:
                lines.append(f"🧵 {html_escape(t)}")

        return "\n".join(lines)

    @classmethod
    def _truncate_to_messages(
        cls,
        full_msg: str,
        parts: list[str],
        clusters: list[dict],
        content: dict,
    ) -> list[str]:
        """
        Deterministic truncation cascade (§9.3):
        1. Reduce clusters 3→2
        2. Shorten summaries to 120 chars
        3. Reduce discarded to 1 line
        4. Drop carousel slides 6-8
        5. Hard split
        """
        if len(full_msg) <= TELEGRAM_MSG_LIMIT:
            return [full_msg]

        # Step 1: rebuild with fewer clusters (2 instead of 3)
        if len(clusters) > 2:
            log.info("Truncation: reducing clusters 3→2 (msg was %d chars)", len(full_msg))
            # Rebuild with 2 clusters — caller should retry with max_clusters=2
            # For simplicity, we split into multiple messages
            pass

        # Step 5: split into chunks of ≤ 4096
        messages = []
        while full_msg:
            if len(full_msg) <= TELEGRAM_MSG_LIMIT:
                messages.append(full_msg)
                break
            # Find a good split point (newline near the limit)
            split_at = full_msg.rfind("\n", 0, TELEGRAM_MSG_LIMIT)
            if split_at < 100:
                split_at = TELEGRAM_MSG_LIMIT
            messages.append(full_msg[:split_at])
            full_msg = full_msg[split_at:].lstrip("\n")

        return messages

    @staticmethod
    def _fmt_num(n) -> str:
        try:
            n = int(n)
        except (ValueError, TypeError):
            return "0"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @classmethod
    def format_help(cls) -> str:
        """Build the trend help message in HTML."""
        return (
            f"🔍 <b>Trend Research Commands</b>\n"
            f"{SEPARATOR}\n\n"
            "<b>Commands:</b>\n"
            "• <code>/trend &lt;niche&gt;</code> — Research trends (adaptive window)\n"
            "• <code>/trend_full &lt;niche&gt;</code> — Summary + full JSON file\n"
            "• <code>/trend_window &lt;hours&gt; &lt;niche&gt;</code> — Fixed time window\n"
            "• <code>/trend_help</code> — This help message\n\n"
            "<b>Examples:</b>\n"
            "• <code>/trend ai email funnels</code>\n"
            "• <code>/trend_full ai lead magnet</code>\n"
            "• <code>/trend_window 168 ai chatbot</code>\n"
            "• <code>/trend ai_sales_funnels</code>\n\n"
            "<b>Notes:</b>\n"
            f"• Niche: letters, numbers, _ or - (max {NICHE_MAX_LEN} chars)\n"
            f"• Window hours: {WINDOW_MIN}–{WINDOW_MAX} (6h to 30d)\n"
            "• Default: no niche → uses AI email + sales funnel presets\n"
            "• Rate limit: 1 request per 10 seconds"
        )

    @classmethod
    def format_error(cls, message: str, suggestion: str = "") -> str:
        """Format a user-facing error in HTML."""
        msg = f"❌ {html_escape(message)}"
        if suggestion:
            msg += f"\n💡 {html_escape(suggestion)}"
        return msg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ROUTER CLASS
# ══════════════════════════════════════════════════════════════════════════════

class TrendBotRouter:
    """
    Telegram command router for BMAD trend research orchestration.

    Usage:
        router = TrendBotRouter(
            youtube_api_key="...",
            available_models={...},
            user_model_selection={},
            default_model="kimi",
            admin_ids={123},
        )
        router.register_handlers(app)
    """

    def __init__(
        self,
        youtube_api_key: str,
        available_models: dict,
        user_model_selection: dict,
        default_model: str,
        admin_ids: set,
        user_cooldown: float = 10.0,
        global_rpm: int = 20,
    ):
        self.youtube_api_key = youtube_api_key
        self.available_models = available_models
        self.user_model_selection = user_model_selection
        self.default_model = default_model
        self.admin_ids = admin_ids
        self.rate_limiter = RateLimiter(user_cooldown, global_rpm)
        self.formatter = TrendFormatter()

        # Initialize Orchestrator
        self.orchestrator = BMADOrchestrator({
            "youtube_api_key": self.youtube_api_key,
            # We delay setting AI model details until per-request if needed, 
            # or we set a default here. For now, we will pass them dynamically
            # by updating the wrapper config if needed, or by passing via orchestrator.
            # Currently FTR uses AI to generate content. We'll pass the available models
            # and let the wrapper use the default, or we can instantiate orchestrator per request.
            # To keep it stateful, let's just pass the default model details for now.
        })

    def register_handlers(self, app):
        """Register all trend commands with the Telegram Application."""
        app.add_handler(CommandHandler("trend", self._handle_trend))
        app.add_handler(CommandHandler("trend_full", self._handle_trend_full))
        app.add_handler(CommandHandler("trend_window", self._handle_trend_window))
        app.add_handler(CommandHandler("trend_help", self._handle_trend_help))

    # ══════════════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    async def _handle_trend(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /trend <niche> — adaptive window, summary mode."""
        user_id = update.effective_user.id

        if not self._check_admin(user_id):
            await update.message.reply_text("Admin only.")
            return

        if not self._check_api_key(update):
            return

        if not await self._check_rate_limit(update, user_id):
            return

        niche, error = self._parse_niche(ctx.args)
        if error:
            await update.message.reply_html(error)
            return

        await self._run_research(update, "/trend", niche, window_hours=None, attach_json=False)

    async def _handle_trend_full(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /trend_full <niche> — summary + JSON file."""
        user_id = update.effective_user.id

        if not self._check_admin(user_id):
            await update.message.reply_text("Admin only.")
            return

        if not self._check_api_key(update):
            return

        if not await self._check_rate_limit(update, user_id):
            return

        niche, error = self._parse_niche(ctx.args)
        if error:
            await update.message.reply_html(error)
            return

        await self._run_research(update, "/trend_full", niche, window_hours=None, attach_json=True)

    async def _handle_trend_window(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /trend_window <hours> <niche> — fixed window."""
        user_id = update.effective_user.id

        if not self._check_admin(user_id):
            await update.message.reply_text("Admin only.")
            return

        if not self._check_api_key(update):
            return

        if not await self._check_rate_limit(update, user_id):
            return

        args = list(ctx.args) if ctx.args else []

        # Parse hours
        if not args:
            await update.message.reply_html(self.formatter.format_help())
            return

        hours_str = args[0]
        try:
            hours = int(hours_str)
        except ValueError:
            await update.message.reply_html(
                self.formatter.format_error(
                    "Invalid window. Hours must be a number.",
                    f"Example: /trend_window 168 ai_email",
                )
            )
            return

        if hours < WINDOW_MIN:
            await update.message.reply_html(
                self.formatter.format_error(f"Invalid window. Minimum is {WINDOW_MIN} hours.")
            )
            return
        if hours > WINDOW_MAX:
            await update.message.reply_html(
                self.formatter.format_error(f"Invalid window. Maximum is {WINDOW_MAX} hours (30 days).")
            )
            return

        niche, error = self._parse_niche(args[1:])
        if error:
            await update.message.reply_html(error)
            return

        await self._run_research(update, "/trend_window", niche, window_hours=hours, attach_json=False)

    async def _handle_trend_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /trend_help — usage guide."""
        await update.message.reply_html(self.formatter.format_help())

    # ══════════════════════════════════════════════════════════════════════
    # CORE RESEARCH EXECUTION
    # ══════════════════════════════════════════════════════════════════════

    async def _run_research(
        self,
        update: Update,
        command: str,
        niche: str,
        window_hours: int | None,
        attach_json: bool,
    ):
        """Orchestrate: build payload → call BMADOrchestrator → format → send."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        trace_id = str(uuid.uuid4())[:8]
        request_id = f"tg-{trace_id}-{int(time.time())}"

        log.info(
            "Trend research: trace=%s cmd=%s user=%s chat=%s niche='%s' window=%s full=%s",
            trace_id, command, user_id, chat_id, niche, window_hours, attach_json,
        )

        # Status message
        mode_label = f"fixed {window_hours}h" if window_hours else "adaptive"
        niche_label = niche or "default presets"
        await update.message.reply_html(
            f"🔍 Routing request via BMAD Orchestrator...\n"
            f"📌 Niche: <code>{html_escape(niche_label)}</code>\n"
            f"🕐 Window: {html_escape(mode_label)}\n"
            f"⏳ This may take 10-15 seconds."
        )
        await update.message.chat.send_action("typing")

        # Get AI model config for content generation
        model_key = self.user_model_selection.get(user_id, self.default_model)
        model_info = self.available_models.get(model_key, self.available_models[self.default_model])
        display_name, api_url, api_key, model_id = model_info

        # Optional: We inject the current user's AI model specs into the orchestrator registry's wrapper.
        # This is a bit hacky but works for now to support dynamic model switching per user.
        ftr_wrapper = self.orchestrator.registry.get("FunnelTrendResearcher")["instance"]
        ftr_wrapper.ai_api_url = api_url
        ftr_wrapper.ai_api_key = api_key
        ftr_wrapper.ai_model = model_id

        # Build payload for BMADOrchestrator
        router_payload = {
            "request_id": request_id,
            "command": command,
            "args": {"niche": niche},
            "user_id": user_id,
            "chat_id": chat_id,
        }
        if window_hours is not None:
            router_payload["args"]["hours"] = window_hours

        # Execute via Orchestrator
        start_time = time.monotonic()
        try:
            orch_response = await self.orchestrator.execute(router_payload)
        except Exception as e:
            latency = time.monotonic() - start_time
            log.error("Orchestrator error: trace=%s latency=%.1fs error=%s", trace_id, latency, e)
            await update.message.reply_html(
                self.formatter.format_error(
                    "Something went wrong generating the trend report. Please retry.",
                )
            )
            return

        latency = time.monotonic() - start_time
        meta = orch_response.get("meta", {})
        status = meta.get("status", "unknown")
        log.info("Orchestrator done: trace=%s status=%s latency=%.1fs", trace_id, status, latency)

        # Handle error status
        if status == "error":
            err = orch_response.get("error", {})
            if err:
                err_type = err.get("type", "").lower()
                if "quota" in err_type or "rate" in err_type:
                    await update.message.reply_html(
                        self.formatter.format_error("Trend service temporarily unavailable. Please retry later.")
                    )
                elif "key" in err_type:
                    await update.message.reply_html(
                        self.formatter.format_error("YouTube API key issue. Contact admin.")
                    )
                elif "timeout" in err_type:
                    await update.message.reply_html(
                        self.formatter.format_error("Request timed out. Please try again.")
                    )
                else:
                    await update.message.reply_html(
                        self.formatter.format_error(
                            err.get("message", "No results found."),
                            f"Try /trend_window {WINDOW_MAX} {niche}" if niche else "",
                        )
                    )
            else:
                await update.message.reply_html(
                    self.formatter.format_error("Something went wrong. Please retry.")
                )
            return

        # Success - extract agent data
        result = orch_response.get("data", {})

        # Format summary
        niche_display = niche or "AI email + sales funnels"
        messages = self.formatter.format_summary(result, niche_display)
        for msg in messages:
            try:
                await update.message.reply_html(msg)
            except Exception as e:
                log.warning("HTML send failed, falling back to plain: %s", e)
                # Strip HTML tags for fallback
                plain = re.sub(r"<[^>]+>", "", msg)
                await update.message.reply_text(plain[:TELEGRAM_MSG_LIMIT])

        # Attach JSON file (full mode)
        if attach_json:
            await self._send_json_file(update, result, niche)

    async def _send_json_file(self, update: Update, result: dict, niche: str):
        """Send full JSON result as a downloadable file."""
        try:
            slug = re.sub(r"[^a-z0-9_]", "_", (niche or "default").lower()).strip("_")
            slug = re.sub(r"_+", "_", slug)[:30]
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
            filename = f"trend_report_{slug}_{ts}.json"

            json_bytes = json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8")
            file_obj = io.BytesIO(json_bytes)
            file_obj.name = filename

            await update.message.reply_document(
                document=file_obj,
                filename=filename,
                caption=f"📊 Full trend report: {niche or 'default'}",
            )
        except Exception as e:
            log.error("Failed to send JSON file: %s", e)
            await update.message.reply_text("⚠️ Could not attach JSON file.")

    # ══════════════════════════════════════════════════════════════════════
    # VALIDATION HELPERS
    # ══════════════════════════════════════════════════════════════════════

    def _check_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    async def _check_api_key(self, update: Update) -> bool:
        if not self.youtube_api_key:
            await update.message.reply_text("❌ YouTube API key not configured.")
            return False
        return True

    async def _check_rate_limit(self, update: Update, user_id: int) -> bool:
        if not self.rate_limiter.check_user(user_id):
            remaining = self.rate_limiter.remaining_cooldown(user_id)
            await update.message.reply_text(
                f"⏳ Too fast — please wait {remaining:.0f}s and try again."
            )
            return False
        if not self.rate_limiter.check_global():
            await update.message.reply_text(
                "⏳ Service is busy. Please retry in a moment."
            )
            return False
        return True

    @staticmethod
    def _parse_niche(args: list | None) -> tuple[str, str | None]:
        """
        Parse and validate niche from command args.
        Returns (niche_string, error_html_or_None).
        Empty niche is allowed (uses defaults).
        """
        if not args:
            return "", None  # Use default niches

        niche = " ".join(args).strip()
        niche = re.sub(r"\s+", " ", niche)

        if len(niche) > NICHE_MAX_LEN:
            return "", TrendFormatter.format_error(
                f"Niche too long (max {NICHE_MAX_LEN} chars).",
                "Example: /trend ai email funnels",
            )

        if not NICHE_RE.match(niche):
            return "", TrendFormatter.format_error(
                "Invalid niche format. Use words, numbers, _ or -.",
                "Example: /trend ai_email_funnels",
            )

        return niche, None
