#!/usr/bin/env bash
# Sends a test spend, imitating the iPhone shortcut.
#
#   ./scripts/test_spend.sh https://casher-arx3.onrender.com <token-from-/token> [amount]
#
# Without an amount it sends a real push text and lets the bot parse the number itself.

set -euo pipefail

HOST="${1:?Pass the service URL, e.g. https://casher-arx3.onrender.com}"
TOKEN="${2:?Pass your personal token, the /token command prints it}"
AMOUNT="${3:-}"

if [[ -n "$AMOUNT" ]]; then
    BODY="{\"amount\": $AMOUNT, \"currency\": \"KGS\", \"raw\": \"Тестовая трата\"}"
else
    BODY='{"raw": "Успещная операция по QR. Сумма: 1470.00 KGS"}'
fi

curl -sS -X POST "${HOST%/}/spend" \
    -H "X-Token: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY"
echo
