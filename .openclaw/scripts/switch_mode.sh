#!/bin/bash
# switch_mode.sh — Switch OpenClaw agent model mode
# Usage: switch_mode.sh [show|fast|smart]
#
# Modes:
#   fast  -> sumopod/gpt-4.1-nano (cepat, hemat token)
#   smart -> ark-kimi/kimi-k2-250905 (lebih pintar, lebih lambat)
#   show  -> tampilkan mode saat ini

CONFIG_FILE="$HOME/.openclaw/openclaw.json"
MODE_FILE="$HOME/.openclaw/.current_mode"

# Ensure mode file exists
if [ ! -f "$MODE_FILE" ]; then
    echo "fast" > "$MODE_FILE"
fi

CURRENT_MODE=$(cat "$MODE_FILE" 2>/dev/null || echo "fast")
CMD="${1:-show}"

case "$CMD" in
    show)
        if [ "$CURRENT_MODE" = "fast" ]; then
            echo "Mode saat ini: fast (GPT-4.1 Nano)"
            echo "Ketik: /mode smart untuk model lebih pintar"
        else
            echo "Mode saat ini: smart (Kimi K2)"
            echo "Ketik: /mode fast untuk model lebih cepat"
        fi
        ;;
    fast)
        echo "fast" > "$MODE_FILE"
        echo "Mode diubah ke: fast (GPT-4.1 Nano)"
        ;;
    smart)
        echo "smart" > "$MODE_FILE"
        echo "Mode diubah ke: smart (Kimi K2)"
        ;;
    *)
        echo "Usage: /mode [fast|smart]"
        echo "  fast  = GPT-4.1 Nano (cepat)"
        echo "  smart = Kimi K2 (pintar)"
        ;;
esac
