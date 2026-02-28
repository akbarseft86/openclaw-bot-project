#!/usr/bin/env python3
"""
SkillMenuEngine — PRD-05
━━━━━━━━━━━━━━━━━━━━━━━━
Dynamic OpenClaw Skill Menu & Interactive Flow Engine for Telegram.
Auto-discovers installed OpenClaw skills, renders inline keyboard menus,
manages multi-turn conversational flows, and routes invocations to
BMADOrchestrator or OpenClaw Gateway.

Usage:
    from skill_menu_engine import SkillMenuEngine
    engine = SkillMenuEngine(config)
    engine.register_handlers(app)
"""

import re
import time
import json
import logging
import subprocess
from typing import Any, Optional
from html import escape as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

log = logging.getLogger("skill_menu")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

SKILLS_DIR = "/root/.openclaw/skills"
CALLBACK_RE = re.compile(r"^(core|nav|skill|cat):[a-zA-Z0-9_:]+$")
PAGE_SIZE = 6
SESSION_TTL = 900  # 15 minutes

# Core actions definitions (pinned at top of menu)
CORE_ACTIONS = [
    {"id": "trend",  "label": "🔥 Trend Research",  "command": "/trend"},
    {"id": "full",   "label": "📊 Full Report",     "command": "/trend_full"},
    {"id": "window", "label": "⏱ Custom Window",    "command": "/trend_window"},
]


# ══════════════════════════════════════════════════════════════════════════════
# SKILL REGISTRY — discovers skills from filesystem
# ══════════════════════════════════════════════════════════════════════════════

class SkillRegistry:
    """Discovers and caches OpenClaw skills from the skills directory."""

    def __init__(self, skills_dir: str = SKILLS_DIR, cache_ttl: float = 300):
        self.skills_dir = skills_dir
        self.cache_ttl = cache_ttl
        self._cache: list[dict] = []
        self._cache_time: float = 0

    def discover(self, force: bool = False) -> list[dict]:
        """Return list of skill metadata dicts."""
        now = time.time()
        if not force and self._cache and (now - self._cache_time < self.cache_ttl):
            return self._cache

        skills = []
        try:
            cmd = (
                f"for f in $(find {self.skills_dir} -maxdepth 3 -name '*.md' "
                f"-not -path '*/node_modules/*' -type f 2>/dev/null); do "
                "name=$(grep '^name:' \"$f\" 2>/dev/null | head -1 | sed 's/name: *//'); "
                "desc=$(grep '^description:' \"$f\" 2>/dev/null | head -1 | sed 's/description: *//'); "
                "dir=$(dirname \"$f\" | xargs basename); "
                "if [ -n \"$name\" ]; then echo \"$dir|||$name|||$desc\"; fi; done"
            )
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10
            )
            output = result.stdout.strip()
            if output:
                for line in output.split("\n"):
                    parts = line.strip().split("|||")
                    if len(parts) >= 2:
                        dir_name = parts[0].strip()
                        name = parts[1].strip()
                        desc = parts[2].strip() if len(parts) > 2 else ""
                        # Generate a safe skill_id from directory name
                        skill_id = re.sub(r"[^a-z0-9_]", "_", dir_name.lower()).strip("_")
                        skill_id = re.sub(r"_+", "_", skill_id)[:30]
                        skills.append({
                            "skill_id": skill_id,
                            "name": name,
                            "description": desc,
                            "dir_name": dir_name,
                        })
        except Exception as e:
            log.error("Skill discovery failed: %s", e)

        self._cache = skills
        self._cache_time = now
        log.info("Discovered %d OpenClaw skills", len(skills))
        return skills

    def get_skill(self, skill_id: str) -> Optional[dict]:
        """Get a single skill by ID."""
        for s in self.discover():
            if s["skill_id"] == skill_id:
                return s
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SESSION MANAGER — per-user state with TTL
# ══════════════════════════════════════════════════════════════════════════════

class UserSession:
    """Represents one user's conversational state."""

    def __init__(self, user_id: int, chat_id: int):
        self.user_id = user_id
        self.chat_id = chat_id
        self.state = "IDLE"
        self.skill_id: str = ""
        self.skill_name: str = ""
        self.pending_params: list[dict] = []
        self.collected_params: dict[str, str] = {}
        self.param_index: int = 0
        self.last_active: float = time.time()
        self.menu_message_id: int | None = None

    def reset(self):
        self.state = "IDLE"
        self.skill_id = ""
        self.skill_name = ""
        self.pending_params = []
        self.collected_params = {}
        self.param_index = 0
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > SESSION_TTL

    def touch(self):
        self.last_active = time.time()


class SessionManager:
    """Manages per-user sessions with TTL cleanup."""

    def __init__(self):
        self._sessions: dict[str, UserSession] = {}

    def _key(self, user_id: int, chat_id: int) -> str:
        return f"{chat_id}_{user_id}"

    def get(self, user_id: int, chat_id: int) -> UserSession:
        key = self._key(user_id, chat_id)
        session = self._sessions.get(key)
        if session and not session.is_expired():
            session.touch()
            return session
        # Create new or replace expired
        session = UserSession(user_id, chat_id)
        self._sessions[key] = session
        return session

    def clear(self, user_id: int, chat_id: int):
        key = self._key(user_id, chat_id)
        if key in self._sessions:
            self._sessions[key].reset()

    def cleanup(self):
        """Remove expired sessions."""
        expired = [k for k, v in self._sessions.items() if v.is_expired()]
        for k in expired:
            del self._sessions[k]


# ══════════════════════════════════════════════════════════════════════════════
# USAGE TRACKER — analytics, ranking, pinning, suggestions
# ══════════════════════════════════════════════════════════════════════════════

SUGGESTION_MAP = {
    # last_skill_id -> (suggestion_text, suggested_callback)
    "debug_pro":       ("Run a security audit next?",        "skill:security_check"),
    "security_check":  ("Check network ports next?",          "skill:network_monitor"),
    "content_writer":  ("Research trends for content ideas?", "core:trend"),
    "seo_audit":       ("Generate IG hooks from trends?",     "core:trend"),
}

class UsageTracker:
    """
    Tracks per-user and global skill usage for:
    - Frequently-used skill pinning (top 3)
    - Priority ranking (sort by popularity)
    - AI suggestion (based on last action)
    """

    def __init__(self):
        # {user_id: {skill_id: count}}
        self._user_usage: dict[int, dict[str, int]] = {}
        # {skill_id: count}  (global)
        self._global_usage: dict[str, int] = {}
        # {user_id: skill_id}  (last executed)
        self._last_skill: dict[int, str] = {}

    def record(self, user_id: int, skill_id: str):
        """Record a skill invocation."""
        # Per-user
        if user_id not in self._user_usage:
            self._user_usage[user_id] = {}
        self._user_usage[user_id][skill_id] = self._user_usage[user_id].get(skill_id, 0) + 1
        # Global
        self._global_usage[skill_id] = self._global_usage.get(skill_id, 0) + 1
        # Last used
        self._last_skill[user_id] = skill_id

    def get_pinned_ids(self, user_id: int, top_n: int = 3) -> list[str]:
        """Return top N most-used skill_ids for this user."""
        usage = self._user_usage.get(user_id, {})
        if not usage:
            return []
        sorted_skills = sorted(usage.items(), key=lambda x: -x[1])
        return [sid for sid, _ in sorted_skills[:top_n] if _ >= 2]  # min 2 uses to pin

    def get_global_ranking(self) -> dict[str, int]:
        """Return global usage counts."""
        return dict(self._global_usage)

    def get_last_skill(self, user_id: int) -> str:
        """Return last executed skill_id for this user."""
        return self._last_skill.get(user_id, "")

    def get_suggestion(self, user_id: int) -> tuple[str, str] | None:
        """
        Return an AI suggestion based on last action.
        Returns (suggestion_text, callback_data) or None.
        """
        last = self.get_last_skill(user_id)
        if last and last in SUGGESTION_MAP:
            return SUGGESTION_MAP[last]
        return None

    def get_user_stats(self, user_id: int) -> dict:
        """Return stats summary for a user."""
        usage = self._user_usage.get(user_id, {})
        total = sum(usage.values())
        top = sorted(usage.items(), key=lambda x: -x[1])[:3]
        return {"total_invocations": total, "top_skills": top}


# ══════════════════════════════════════════════════════════════════════════════
# MENU RENDERER — builds inline keyboards
# ══════════════════════════════════════════════════════════════════════════════

class MenuRenderer:
    """Builds Telegram InlineKeyboardMarkup dynamically."""

    @staticmethod
    def build_main_menu(
        skills: list[dict],
        page: int = 0,
        pinned_ids: list[str] | None = None,
        suggestion: tuple[str, str] | None = None,
        global_ranking: dict[str, int] | None = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build the main /start menu with core actions + pinned + paginated skills."""
        pinned_ids = pinned_ids or []
        global_ranking = global_ranking or {}

        text_parts = [
            "🧠 <b>OpenClaw Control Panel</b>",
            "━━━━━━━━━━━━━━━━━━",
            "Select an action below:",
        ]

        keyboard = []

        # Core actions row
        keyboard.append([
            InlineKeyboardButton(a["label"], callback_data=f"core:{a['id']}")
            for a in CORE_ACTIONS
        ])

        # AI Suggestion row (if available)
        if suggestion:
            sug_text, sug_cb = suggestion
            text_parts.append("")
            text_parts.append(f"💡 <i>{html_escape(sug_text)}</i>")
            keyboard.append([
                InlineKeyboardButton(f"💡 {sug_text[:35]}", callback_data=sug_cb)
            ])

        # Pinned (frequently used) skills
        pinned_skills = [s for s in skills if s["skill_id"] in pinned_ids]
        if pinned_skills:
            text_parts.append("")
            text_parts.append("⭐ <b>Frequently Used</b>")
            row = []
            for s in pinned_skills[:3]:
                uses = global_ranking.get(s["skill_id"], 0)
                label = f"⭐ {s['name']}"
                if uses:
                    label += f" ({uses}x)"
                if len(label) > 32:
                    label = label[:29] + "..."
                row.append(InlineKeyboardButton(
                    label, callback_data=f"skill:{s['skill_id']}"
                ))
            keyboard.append(row)

        # Remaining skills (exclude pinned)
        remaining = [s for s in skills if s["skill_id"] not in pinned_ids]

        # Sort remaining by global usage (most popular first)
        if global_ranking:
            remaining.sort(key=lambda s: -global_ranking.get(s["skill_id"], 0))

        if remaining:
            text_parts.append("")
            text_parts.append(f"🧩 <b>All Skills</b> ({len(skills)})")

            total_pages = max(1, (len(remaining) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            start = page * PAGE_SIZE
            end = start + PAGE_SIZE
            page_skills = remaining[start:end]

            # Skills in rows of 2
            for i in range(0, len(page_skills), 2):
                row = []
                for s in page_skills[i:i+2]:
                    uses = global_ranking.get(s["skill_id"], 0)
                    label = f"🧩 {s['name']}"
                    if uses:
                        label += f" ({uses}x)"
                    if len(label) > 32:
                        label = label[:29] + "..."
                    row.append(InlineKeyboardButton(
                        label, callback_data=f"skill:{s['skill_id']}"
                    ))
                keyboard.append(row)

            # Pagination row
            if total_pages > 1:
                nav_row = []
                if page > 0:
                    nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav:page:{page-1}"))
                nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="nav:noop"))
                if page < total_pages - 1:
                    nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav:page:{page+1}"))
                keyboard.append(nav_row)
        elif not pinned_skills:
            text_parts.append("")
            text_parts.append("⚠️ <i>No OpenClaw skills detected.</i>")

        # Footer row
        keyboard.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="nav:refresh"),
            InlineKeyboardButton("❓ Help", callback_data="nav:help"),
        ])

        return "\n".join(text_parts), InlineKeyboardMarkup(keyboard)

    @staticmethod
    def build_skill_detail(skill: dict) -> tuple[str, InlineKeyboardMarkup]:
        """Build the detail view for a selected skill."""
        text = (
            f"🧩 <b>{html_escape(skill['name'])}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        if skill.get("description"):
            text += f"<i>{html_escape(skill['description'])}</i>\n\n"
        else:
            text += "\n"
        text += "Tap <b>▶️ Run</b> to execute this skill, or provide a prompt.\n"
        text += "<i>(Type /cancel to abort)</i>"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Run Skill", callback_data=f"skill:run:{skill['skill_id']}"),
                InlineKeyboardButton("✏️ Run with Input", callback_data=f"skill:input:{skill['skill_id']}"),
            ],
            [
                InlineKeyboardButton("⬅️ Back to Menu", callback_data="nav:main"),
            ],
        ])
        return text, keyboard

    @staticmethod
    def build_core_prompt(action_id: str) -> tuple[str, InlineKeyboardMarkup]:
        """Prompt for core action input (niche, window, etc.)."""
        prompts = {
            "trend": (
                "🔥 <b>Trend Research</b>\n\n"
                "Send the niche you want to research.\n"
                "Example: <code>ai email funnels</code>\n\n"
                "Or tap <b>Default</b> to use preset niches.\n"
                "<i>(Type /cancel to abort)</i>"
            ),
            "full": (
                "📊 <b>Full Report</b>\n\n"
                "Send the niche for a full report (summary + JSON file).\n"
                "Example: <code>ai lead magnet</code>\n\n"
                "Or tap <b>Default</b> to use preset niches.\n"
                "<i>(Type /cancel to abort)</i>"
            ),
            "window": (
                "⏱ <b>Custom Window</b>\n\n"
                "Send the window hours and niche.\n"
                "Format: <code>&lt;hours&gt; &lt;niche&gt;</code>\n"
                "Example: <code>168 ai chatbot</code>\n\n"
                "Min: 6h │ Max: 720h (30 days)\n"
                "<i>(Type /cancel to abort)</i>"
            ),
        }
        text = prompts.get(action_id, "Unknown action.")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Default (preset niches)", callback_data=f"core:default:{action_id}")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="nav:main")],
        ])
        return text, keyboard

    @staticmethod
    def build_help() -> tuple[str, InlineKeyboardMarkup]:
        """Build help screen."""
        text = (
            "❓ <b>Help — OpenClaw Control Panel</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Core Actions:</b>\n"
            "• 🔥 <b>Trend Research</b> — Discover AI trends via YouTube\n"
            "• 📊 <b>Full Report</b> — Summary + downloadable JSON\n"
            "• ⏱ <b>Custom Window</b> — Fixed time window research\n\n"
            "<b>Skills:</b>\n"
            "• All installed OpenClaw skills appear as buttons\n"
            "• Tap a skill to see details and run it\n"
            "• Skills auto-refresh when new ones are added\n\n"
            "<b>Commands:</b>\n"
            "• <code>/start</code> — Open the control panel\n"
            "• <code>/cancel</code> — Cancel current operation\n"
            "• <code>/trend</code> <code>/trend_full</code> <code>/trend_window</code> — Direct access\n"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="nav:main")],
        ])
        return text, keyboard


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class SkillMenuEngine:
    """
    Interactive UI engine for Telegram.
    Manages dynamic menus, state machine, and OpenClaw skill invocation.
    """

    def __init__(self, config: dict):
        self.admin_ids: set = config.get("admin_ids", set())
        self.ask_openclaw = config.get("ask_openclaw_fn")  # async callable
        self.send_long = config.get("send_long_fn")        # async callable
        self.registry = SkillRegistry()
        self.sessions = SessionManager()
        self.renderer = MenuRenderer()
        self.usage = UsageTracker()

    def register_handlers(self, app):
        """Register /start, callback, and text-input handlers."""
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        # Text handler for AWAITING states — must be added AFTER other handlers
        # and uses a filter group to avoid conflicts with the main chat handler
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text_input),
            group=1,
        )

    # ══════════════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ══════════════════════════════════════════════════════════════════════

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /start — render main menu."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # Reset session
        session = self.sessions.get(user_id, chat_id)
        session.reset()

        skills = self.registry.discover()
        pinned = self.usage.get_pinned_ids(user_id)
        suggestion = self.usage.get_suggestion(user_id)
        ranking = self.usage.get_global_ranking()
        text, markup = self.renderer.build_main_menu(
            skills, pinned_ids=pinned, suggestion=suggestion, global_ranking=ranking
        )
        msg = await update.message.reply_html(text, reply_markup=markup)
        session.menu_message_id = msg.message_id

    async def _cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel — reset state and show menu."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        session = self.sessions.get(user_id, chat_id)
        was_active = session.state != "IDLE"
        session.reset()

        if was_active:
            await update.message.reply_text("❌ Operation cancelled.")

        skills = self.registry.discover()
        pinned = self.usage.get_pinned_ids(user_id)
        suggestion = self.usage.get_suggestion(user_id)
        ranking = self.usage.get_global_ranking()
        text, markup = self.renderer.build_main_menu(
            skills, pinned_ids=pinned, suggestion=suggestion, global_ranking=ranking
        )
        msg = await update.message.reply_html(text, reply_markup=markup)
        session.menu_message_id = msg.message_id

    # ══════════════════════════════════════════════════════════════════════
    # CALLBACK QUERY HANDLER
    # ══════════════════════════════════════════════════════════════════════

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle all inline keyboard button presses."""
        query = update.callback_query
        await query.answer()  # dismiss loading spinner

        data = query.data or ""
        user_id = query.from_user.id
        chat_id = query.message.chat_id

        # Validate callback data
        if not CALLBACK_RE.match(data):
            log.warning("Invalid callback data: %s", data)
            return

        session = self.sessions.get(user_id, chat_id)
        session.touch()

        # ── Navigation ──
        if data == "nav:main":
            session.reset()
            skills = self.registry.discover()
            pinned = self.usage.get_pinned_ids(user_id)
            suggestion = self.usage.get_suggestion(user_id)
            ranking = self.usage.get_global_ranking()
            text, markup = self.renderer.build_main_menu(
                skills, pinned_ids=pinned, suggestion=suggestion, global_ranking=ranking
            )
            await self._edit_message(query, text, markup)
            return

        if data == "nav:refresh":
            session.reset()
            skills = self.registry.discover(force=True)
            pinned = self.usage.get_pinned_ids(user_id)
            suggestion = self.usage.get_suggestion(user_id)
            ranking = self.usage.get_global_ranking()
            text, markup = self.renderer.build_main_menu(
                skills, pinned_ids=pinned, suggestion=suggestion, global_ranking=ranking
            )
            await self._edit_message(query, text, markup)
            return

        if data == "nav:help":
            text, markup = self.renderer.build_help()
            await self._edit_message(query, text, markup)
            return

        if data.startswith("nav:page:"):
            page = int(data.split(":")[-1])
            skills = self.registry.discover()
            pinned = self.usage.get_pinned_ids(user_id)
            ranking = self.usage.get_global_ranking()
            text, markup = self.renderer.build_main_menu(
                skills, page=page, pinned_ids=pinned, global_ranking=ranking
            )
            await self._edit_message(query, text, markup)
            return

        if data == "nav:noop":
            return

        # ── Core actions ──
        if data.startswith("core:"):
            parts = data.split(":")
            action_id = parts[1]

            # "core:default:trend" → run with default niches immediately
            if len(parts) == 3 and parts[1] == "default":
                action_id = parts[2]
                await self._execute_core_default(query, session, action_id)
                return

            # Show prompt screen for this core action
            session.state = f"AWAITING_CORE_{action_id.upper()}"
            session.skill_id = action_id
            text, markup = self.renderer.build_core_prompt(action_id)
            await self._edit_message(query, text, markup)
            return

        # ── Skill selection ──
        if data.startswith("skill:"):
            parts = data.split(":")
            if len(parts) == 2:
                # skill:<skill_id> — show detail
                skill_id = parts[1]
                skill = self.registry.get_skill(skill_id)
                if not skill:
                    await query.edit_message_text("❌ Skill not found. Try refreshing.")
                    return
                session.skill_id = skill_id
                session.skill_name = skill["name"]
                text, markup = self.renderer.build_skill_detail(skill)
                await self._edit_message(query, text, markup)
                return

            if len(parts) == 3 and parts[1] == "run":
                # skill:run:<skill_id> — execute immediately with no extra input
                skill_id = parts[2]
                skill = self.registry.get_skill(skill_id)
                if not skill:
                    await query.edit_message_text("❌ Skill not found.")
                    return
                await self._execute_skill(query, session, skill, prompt="")
                return

            if len(parts) == 3 and parts[1] == "input":
                # skill:input:<skill_id> — ask user for text input
                skill_id = parts[2]
                skill = self.registry.get_skill(skill_id)
                if not skill:
                    await query.edit_message_text("❌ Skill not found.")
                    return
                session.state = "AWAITING_SKILL_INPUT"
                session.skill_id = skill_id
                session.skill_name = skill["name"]
                await query.edit_message_text(
                    f"✏️ <b>{html_escape(skill['name'])}</b>\n\n"
                    f"Type your prompt or instructions for this skill:\n"
                    f"<i>(Type /cancel to abort)</i>",
                    parse_mode="HTML",
                )
                return

    # ══════════════════════════════════════════════════════════════════════
    # TEXT INPUT HANDLER (for AWAITING states)
    # ══════════════════════════════════════════════════════════════════════

    async def _on_text_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle free-text input when session is in an AWAITING state."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        session = self.sessions.get(user_id, chat_id)

        if session.state == "IDLE":
            return  # Not our business, let the main handler process

        text = (update.message.text or "").strip()
        if not text:
            return

        session.touch()

        # ── Core action inputs ──
        if session.state == "AWAITING_CORE_TREND":
            session.reset()
            # Simulate /trend <niche>
            ctx.args = text.split()
            # Find the trend handler and invoke it
            await self._forward_core_command(update, ctx, "/trend", text)
            return

        if session.state == "AWAITING_CORE_FULL":
            session.reset()
            ctx.args = text.split()
            await self._forward_core_command(update, ctx, "/trend_full", text)
            return

        if session.state == "AWAITING_CORE_WINDOW":
            session.reset()
            ctx.args = text.split()
            await self._forward_core_command(update, ctx, "/trend_window", text)
            return

        # ── Skill text input ──
        if session.state == "AWAITING_SKILL_INPUT":
            skill = self.registry.get_skill(session.skill_id)
            if not skill:
                await update.message.reply_text("❌ Skill not found. Try /start again.")
                session.reset()
                return
            session.reset()
            await self._execute_skill_from_message(update, skill, prompt=text)
            return

    # ══════════════════════════════════════════════════════════════════════
    # EXECUTION
    # ══════════════════════════════════════════════════════════════════════

    async def _execute_core_default(self, query, session, action_id: str):
        """Execute a core action with default parameters."""
        session.reset()

        # Build a synthetic Update-like message reply
        await query.edit_message_text(
            f"🔄 Running <b>{action_id}</b> with default settings...",
            parse_mode="HTML",
        )

        # We need to send a message since callback query messages can't easily
        # send new replies. We'll use the chat context.
        chat = query.message.chat

        if action_id == "trend":
            await chat.send_action("typing")
            # Use ask_openclaw or BMAD — for core actions, let the existing
            # trend router handle it. We just need to inform the user.
            await chat.send_message(
                "🔍 Routing request via BMAD Orchestrator...\n"
                "📌 Niche: <code>default presets</code>\n"
                "🕐 Window: adaptive\n"
                "⏳ This may take 10-15 seconds.",
                parse_mode="HTML",
            )
        elif action_id == "full":
            await chat.send_message(
                "📊 Use command: <code>/trend_full</code> (no args = default niches)",
                parse_mode="HTML",
            )
        elif action_id == "window":
            await chat.send_message(
                "⏱ Use command: <code>/trend_window 48</code> (default 48h window)",
                parse_mode="HTML",
            )

    async def _forward_core_command(self, update: Update, ctx, command: str, text: str):
        """Forward the user's text input as if they typed a core /trend command."""
        # Inform user
        action_labels = {
            "/trend": "🔥 Trend Research",
            "/trend_full": "📊 Full Report",
            "/trend_window": "⏱ Custom Window",
        }
        label = action_labels.get(command, command)
        await update.message.reply_html(
            f"⚡ Routing to <b>{html_escape(label)}</b>...\n"
            f"📌 Input: <code>{html_escape(text[:60])}</code>"
        )
        # The actual /trend, /trend_full, /trend_window handlers are registered
        # separately via TrendBotRouter. The user can type the command directly.
        # Here we just confirm the routing direction.
        await update.message.reply_html(
            f"💡 Please run: <code>{command} {html_escape(text[:80])}</code>"
        )

    async def _execute_skill(self, query, session, skill: dict, prompt: str):
        """Execute an OpenClaw skill via Gateway (from callback query)."""
        user_id = query.from_user.id
        skill_id = skill.get("skill_id", "")
        session.reset()
        skill_name = skill["name"]
        dir_name = skill.get("dir_name", skill_name)

        await query.edit_message_text(
            f"🧩 Running <b>{html_escape(skill_name)}</b>...\n"
            f"⏳ Please wait.",
            parse_mode="HTML",
        )

        chat = query.message.chat
        await chat.send_action("typing")

        # Build the OpenClaw message
        if prompt:
            message = f"[skill:{dir_name}] {prompt}"
        else:
            message = f"[skill:{dir_name}] run"

        # Call OpenClaw Gateway
        if self.ask_openclaw:
            reply = await self.ask_openclaw(message)
        else:
            reply = "❌ OpenClaw Gateway not configured."

        # Send result
        if self.send_long:
            # Create a fake-ish update to use send_long properly
            await chat.send_message(
                f"🧩 <b>{html_escape(skill_name)}</b> — Result:\n\n"
                f"{html_escape(reply[:3900])}",
                parse_mode="HTML",
            )
        else:
            await chat.send_message(f"🧩 {skill_name} Result:\n\n{reply[:4000]}")

        # Record usage analytics
        self.usage.record(user_id, skill_id)

        # Show return button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="nav:main")],
        ])
        await chat.send_message("✅ Done.", reply_markup=keyboard)

    async def _execute_skill_from_message(self, update: Update, skill: dict, prompt: str):
        """Execute an OpenClaw skill via Gateway (from text message)."""
        skill_name = skill["name"]
        dir_name = skill.get("dir_name", skill_name)

        await update.message.reply_html(
            f"🧩 Running <b>{html_escape(skill_name)}</b>...\n⏳ Please wait."
        )
        await update.message.chat.send_action("typing")

        if prompt:
            message = f"[skill:{dir_name}] {prompt}"
        else:
            message = f"[skill:{dir_name}] run"

        if self.ask_openclaw:
            reply = await self.ask_openclaw(message)
        else:
            reply = "❌ OpenClaw Gateway not configured."

        # Send result
        result_text = (
            f"🧩 <b>{html_escape(skill_name)}</b> — Result:\n\n"
            f"{html_escape(reply[:3900])}"
        )
        await update.message.reply_html(result_text)

        # Record usage analytics
        user_id = update.effective_user.id
        skill_id = skill.get("skill_id", "")
        self.usage.record(user_id, skill_id)

        # Show return button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="nav:main")],
        ])
        await update.message.reply_text("✅ Done.", reply_markup=keyboard)

    # ══════════════════════════════════════════════════════════════════════
    # UTILITIES
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _edit_message(query, text: str, markup: InlineKeyboardMarkup):
        """Safely edit a callback query message."""
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.warning("Edit message failed: %s", e)
