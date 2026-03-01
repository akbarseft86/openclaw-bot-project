#!/bin/bash
# router_prompt_command.sh — Routes prompt database commands to prompt_db_v2.py
# Called by OpenClaw agent when user sends: prompt [topik], lihat paket: [slug], pakai: [slug], List, etc.
#
# Usage: router_prompt_command.sh "<user_message>"
# Output: Text ready to send back to user via Telegram

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_SCRIPT="$SCRIPT_DIR/prompt_db_v2.py"
PYTHON=python3

MSG="$1"

if [ -z "$MSG" ]; then
    echo "(router_prompt_command.sh: no match)"
    exit 0
fi

# Normalize: trim whitespace, lowercase for matching
MSG_LOWER=$(echo "$MSG" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

# --- Route: "lihat paket: <pack_slug>" ---
if echo "$MSG_LOWER" | grep -qE '^lihat paket[: ]+'; then
    PACK_SLUG=$(echo "$MSG" | sed -E 's/^[Ll]ihat [Pp]aket[: ]+//;s/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$PACK_SLUG" ]; then
        $PYTHON "$DB_SCRIPT" pack-detail "$PACK_SLUG"
        exit 0
    fi
fi

# --- Route: "pakai: <slug>" ---
if echo "$MSG_LOWER" | grep -qE '^pakai[: ]+'; then
    SLUG=$(echo "$MSG" | sed -E 's/^[Pp]akai[: ]+//;s/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$SLUG" ]; then
        $PYTHON "$DB_SCRIPT" get "$SLUG"
        exit 0
    fi
fi

# --- Route: "prompt <topik>" ---
if echo "$MSG_LOWER" | grep -qE '^prompt[[:space:]]+'; then
    TOPIC=$(echo "$MSG" | sed -E 's/^[Pp]rompt[[:space:]]+//;s/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$TOPIC" ]; then
        $PYTHON "$DB_SCRIPT" search-packs "$TOPIC"
        exit 0
    fi
fi

# --- Route: "List <kategori>" ---
if echo "$MSG_LOWER" | grep -qE '^list[[:space:]]+'; then
    CATEGORY=$(echo "$MSG" | sed -E 's/^[Ll]ist[[:space:]]+//;s/^[[:space:]]*//;s/[[:space:]]*$//')
    if [ -n "$CATEGORY" ]; then
        $PYTHON "$DB_SCRIPT" list-category "$CATEGORY"
        exit 0
    fi
fi

# --- Route: "List" (exact) ---
if echo "$MSG_LOWER" | grep -qE '^list$'; then
    $PYTHON "$DB_SCRIPT" list-all
    exit 0
fi

# --- No match ---
echo "(router_prompt_command.sh: no match)"
exit 0
