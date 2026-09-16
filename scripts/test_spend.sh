#!/usr/bin/env bash
# Отправляет тестовую трату, имитируя шорткат с iPhone.
#
#   ./scripts/test_spend.sh https://cacher-bot.onrender.com <токен-из-/token> [сумма]
#
# Без суммы отправляется реальный текст пуша — сумму разберёт сам бот.

set -euo pipefail

HOST="${1:?Укажи адрес сервиса, например https://cacher-bot.onrender.com}"
TOKEN="${2:?Укажи личный токен, его выдаёт команда /token}"
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
